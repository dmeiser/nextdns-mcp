"""Streaming the response body to disk under the total-size cap.

The full CSV is written straight to a temp file chunk by chunk in raw bytes,
while an incremental decoder supplies the text the preview needs until that
preview is full. Past that point rows are counted as raw newline bytes and the
bytes are coalesced into larger writes, so neither decoding nor one offloaded
write per small chunk is paid for text nobody will see. The total-size cap
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
from collections.abc import Callable
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

# Once the preview is full, small stream chunks are gathered into writes of at
# least this size. A 1 GiB CSV otherwise issues one thread offload per chunk to
# write text that has already stopped contributing to the payload.
_STREAM_WRITE_COALESCE_BYTES = 64 * 1024


def _decode_piece(decoder: codecs.IncrementalDecoder, raw: bytes, needs_text: Callable[[], bool]) -> str:
    """Decode a raw chunk only while its text can still enter the preview."""
    return decoder.decode(raw) if needs_text() else ""


def _account_preview(
    preview_lines: list[str], preview_bytes: int, preview_carry: str, dropping_record: bool, text: str
) -> tuple[int, str, bool]:
    """Fold one decoded piece into the preview window and its record state."""
    piece, preview_carry = _preview._previewable(preview_carry, text)
    if len(preview_carry) > _preview.DOWNLOAD_PREVIEW_MAX_BYTES:
        # A single record longer than the whole preview can never fit in it.
        preview_carry = ""
        dropping_record = True
    piece, dropping_record = _preview._skip_dropped_record_tail(piece, dropping_record)
    return _preview._append_preview(preview_lines, preview_bytes, piece), preview_carry, dropping_record


async def _flush_body_chunks(out: Any, body_chunks: bytearray) -> None:
    """Land the buffered body bytes in stream order, then release the buffer."""
    if body_chunks:
        await asyncio.to_thread(out.write, bytes(body_chunks))
        body_chunks.clear()


async def _write_raw_chunk(out: Any, raw: bytes, wants_text: bool, body_chunks: bytearray) -> None:
    """Write one raw chunk directly, or gather it behind the preview into a larger write."""
    if wants_text:
        await asyncio.to_thread(out.write, raw)
        return
    body_chunks.extend(raw)
    if len(body_chunks) >= _STREAM_WRITE_COALESCE_BYTES:
        await _flush_body_chunks(out, body_chunks)


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


async def _stream_pieces(response: httpx.Response, decoder: codecs.IncrementalDecoder, needs_text: Callable[[], bool]):
    """Yield ``(raw_bytes, decoded_text)`` per chunk, then the final decoded tail.

    One loop over this keeps the size cap, the disk write and the
    row/preview tally in one place. Raw bytes are counted and written
    verbatim - no re-encode of every chunk just to account for its size -
    and the text comes from an incremental decoder, so a codepoint split
    across chunk boundaries still decodes whole. Once the preview is full no
    later chunk needs text: row counting reads its raw newline bytes and the
    decoder is left alone. The final piece carries no bytes: it is whatever
    the decoder was holding at end-of-stream, already on disk as part of the
    raw chunks, and is only requested while the preview can still use it.
    """
    async for raw in response.aiter_bytes():
        if raw:
            yield raw, _decode_piece(decoder, raw, needs_text)
    if needs_text():
        tail = decoder.decode(b"", final=True)
        if tail:
            yield b"", tail


async def _write_stream_to_tempfile(response: httpx.Response, path: str) -> dict[str, Any]:
    """Write a streaming response body to ``path`` without double-buffering.

    The full CSV is written straight to disk chunk by chunk, in bytes: the
    size cap is measured on the raw chunk it applies to (no re-encode of every
    chunk just to count its bytes), the file gets the raw bytes rather than a
    decode/re-encode round trip, and only the leading preview needs decoded
    text, which an incremental decoder supplies at the same chunk boundaries
    httpx used (so a codepoint split across chunks still decodes whole). Once
    the preview is full, rows are counted with ``bytes.count(b"\n")`` and
    later small chunks are coalesced before each write is offloaded. Only a
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

    def preview_needs_text() -> bool:
        return not _preview._preview_full(preview_lines, preview_bytes)

    # os.open, not open(): 0o600 is the requested mode only at open() time, and
    # builtin open() takes the system default, so a customer CSV would land
    # group- and world-readable.
    out = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
    body_chunks = bytearray()
    try:
        async for raw, text in _stream_pieces(response, decoder, preview_needs_text):
            wants_text = preview_needs_text()
            if written_bytes + len(raw) > max_total:
                raise DownloadTooLargeError(
                    f"Download exceeds the {max_total}-byte limit "
                    f"(NEXTDNS_DOWNLOAD_MAX_BYTES); aborted after {written_bytes} bytes."
                )
            written_bytes += len(raw)
            if raw:
                # Raw newline bytes are the same count the decoder's text gave,
                # and reading them costs nothing once the preview is closed.
                row_count += raw.count(b"\n")
                open_line = raw[-1:] != b"\n"
                # A write issued here runs on the event loop, and one download
                # is thousands of writes: the worker thread keeps the loop free,
                # and awaiting each write before pulling the next chunk keeps
                # the bytes in stream order. Before the preview is full there
                # are at most its 20 lines; after it, small chunks are gathered
                # so one offload covers many of them.
                await _write_raw_chunk(out, raw, wants_text, body_chunks)
            if wants_text:
                preview_bytes, preview_carry, dropping_record = _account_preview(
                    preview_lines, preview_bytes, preview_carry, dropping_record, text
                )
        await _flush_body_chunks(out, body_chunks)
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
