"""Streaming the response body to disk under the total-size cap.

The full CSV is written straight to a temp file chunk by chunk in raw bytes,
while an incremental decoder supplies the text the row/preview tally needs at
the same chunk boundaries httpx used. The total-size cap
(``NEXTDNS_DOWNLOAD_MAX_BYTES``, default 1 GiB, see
:func:`nextdns_mcp.config.get_download_max_bytes`) is applied while
streaming, not after the body is in hand, so a single download cannot fill
the temp directory. A mid-stream HTTP failure is re-raised around a bounded,
buffered copy so the typed error payload can quote what upstream said.

The preview accounting lives in :mod:`.preview`; this module reads that
module's helpers and caps through the module object at call time, so the
caps have a single owner and a single source of truth.

SPDX-License-Identifier: MIT
"""

import asyncio
import codecs
import os
from typing import Any

import httpx

from ...config import get_download_max_bytes
from ...errors import BODY_SLICE_BYTES
from ...transport import buffered_response, read_body_capped
from . import preview as _preview


class DownloadTooLargeError(Exception):
    """Raised when a download exceeds the total-size cap mid-stream."""


# Read off a failed stream at most this much of the body, so http_error_payload
# can quote the upstream error document without reading a hostile multi-megabyte
# failure page into memory. The size is the errors package's own body slice
# (errors/body.py), so the read always covers what the payload will show.
_ERROR_BODY_READ_BYTES = BODY_SLICE_BYTES


async def _raise_for_streamed_status(response: httpx.Response) -> None:
    """``raise_for_status`` that leaves the failed response's body readable.

    A streaming response raises ``httpx.ResponseNotRead`` from ``.json()`` and
    ``.text`` until it has been read, and ``http_error_payload`` - which the
    download handlers build from the raised error - reads both. Before this, a
    mid-download 503 escaped as ``ResponseNotRead`` instead of the typed
    payload. The bounded error document is read here and re-raised around a
    buffered copy of the response (both shared with ``transport``, which owns
    the capped-read and buffered-rebuild helpers), so the payload carries what
    upstream said without buffering an unbounded body.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if not e.response.is_stream_consumed:
            body = await read_body_capped(e.response, _ERROR_BODY_READ_BYTES)
            e.response = buffered_response(e.response.status_code, e.response.headers, body, e.response.request)
        raise


async def _stream_pieces(response: httpx.Response, decoder: codecs.IncrementalDecoder):
    """Yield ``(raw_bytes, decoded_text)`` per chunk, then the final decoded tail.

    One loop over this keeps the size cap, the disk write and the
    row/preview tally in one place. Raw bytes are counted and written
    verbatim - no re-encode of every chunk just to account for its size -
    and the text comes from an incremental decoder, so a codepoint split
    across chunk boundaries still decodes whole. The final piece carries no
    bytes: it is whatever the decoder was holding at end-of-stream, already
    on disk as part of the raw chunks.
    """
    async for raw in response.aiter_bytes():
        if raw:
            yield raw, decoder.decode(raw)
    tail = decoder.decode(b"", final=True)
    if tail:
        yield b"", tail


async def _write_stream_to_tempfile(response: httpx.Response, path: str) -> dict[str, Any]:
    """Write a streaming response body to ``path`` without double-buffering.

    The full CSV is written straight to disk chunk by chunk, in bytes: the
    size cap is measured on the raw chunk it applies to (no re-encode of every
    chunk just to count its bytes), the file gets the raw bytes rather than a
    decode/re-encode round trip, and only the preview and row counts need the
    text, which an incremental decoder supplies at the same chunk boundaries
    httpx used (so a codepoint split across chunks still decodes whole). Only a
    bounded preview of the leading lines is kept in memory. The total bytes
    written are tracked and, once the running total exceeds the total-size cap
    (the ``NEXTDNS_DOWNLOAD_MAX_BYTES`` environment variable, default 1 GiB,
    see :func:`nextdns_mcp.config.get_download_max_bytes`), a
    :class:`DownloadTooLargeError`
    is raised immediately — while streaming, not after the body is in hand —
    so a single download cannot fill the temp directory.
    """
    await _raise_for_streamed_status(response)
    max_total = get_download_max_bytes()
    preview_lines: list[str] = []
    preview_bytes = 0
    row_count = 0
    written_bytes = 0
    # End-of-stream flag, not per-chunk state: it is recomputed for every
    # chunk and is only true when the last chunk left a line unterminated.
    open_line = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    preview_carry = ""
    # True while skipping the remainder of a record already dropped from the
    # preview for being longer than the whole preview cap: without it, the
    # record's tail - arriving in a later chunk, after the carry was dropped -
    # would be appended to the preview as if it were a new record.
    dropping_record = False
    # os.open, not open(): 0o600 is the requested mode only at open() time, and
    # builtin open() takes the system default, so a customer CSV would land
    # group- and world-readable.
    out = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
    try:
        async for raw, text in _stream_pieces(response, decoder):
            if written_bytes + len(raw) > max_total:
                raise DownloadTooLargeError(
                    f"Download exceeds the {max_total}-byte limit "
                    f"(NEXTDNS_DOWNLOAD_MAX_BYTES); aborted after {written_bytes} bytes."
                )
            written_bytes += len(raw)
            if raw:
                # A write issued here runs on the event loop, and one download is
                # thousands of writes: while one is in flight every other request
                # on this server waits for it. The worker thread keeps the loop
                # free; awaiting each write before pulling the next chunk is what
                # keeps the bytes in stream order.
                await asyncio.to_thread(out.write, raw)
            row_count += text.count("\n")
            open_line = not text.endswith("\n")
            piece, preview_carry = _preview._previewable(preview_carry, text)
            if len(preview_carry) > _preview.DOWNLOAD_PREVIEW_MAX_BYTES:
                # A single record longer than the whole preview can never fit
                # in it: drop the carry instead of growing it with the line.
                preview_carry = ""
                dropping_record = True
            piece, dropping_record = _preview._skip_dropped_record_tail(piece, dropping_record)
            preview_bytes = _preview._append_preview(preview_lines, preview_bytes, piece)
    finally:
        # Close belongs on the worker pool as well. A cancelled task unwinds
        # here while a write is still in flight, and close() takes the buffered
        # writer's lock that the in-flight write is holding; taken on the loop,
        # that wait is the same stall, bounded by one chunk instead of the whole
        # download. The size is read after this, so the flush still lands first.
        await asyncio.to_thread(out.close)
    extra_rows, preview_bytes = _preview._flush_tail(
        preview_lines, preview_bytes, preview_carry, open_line, dropping_record
    )
    row_count += extra_rows
    size = os.path.getsize(path)
    text_preview = "".join(preview_lines)
    return {
        "content_type": response.headers.get("content-type"),
        "file_path": path,
        "size": size,
        "row_count": row_count,
        "preview": {
            "text": text_preview,
            "line_count": len(preview_lines),
            "bytes": preview_bytes,
            "truncated": preview_bytes < size,
        },
    }
