"""The guarded log-download path behind the ``downloadLogs`` tool.

SPDX-License-Identifier: MIT

The endpoint answers with a redirect to a signed URL, so the download is not a
plain API call the generated tool can make: it needs the access-controlled client
for the first request, an *unauthenticated* client for the redirect chain so the
API key never crosses to another host, a check that every hop is https on a
globally routable host, a total-size cap applied while streaming, and a bounded
preview so a CSV export cannot blow up the caller's context.

That is why ``spec_tools`` delegates this one operation here instead of issuing
the request the way it issues every other one.
"""

import asyncio
import codecs
import contextlib
import ipaddress
import logging
import os
import socket
import tempfile
from collections.abc import Iterator
from typing import Any

import httpx

from .. import client
from ..client import AccessDeniedError, _log_safe_error
from ..config import get_download_max_bytes
from ..errors import (
    BODY_SLICE_BYTES,
    ErrorCode,
    error_payload,
    http_error_payload,
)
from ..transport import access_denied_payload_for

logger = logging.getLogger(__name__)


def _unlink_temp_file(path: str) -> None:
    """Best-effort removal of the temp file AND its mkdtemp parent; never masks the original error."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        # Nothing was ever written (e.g. the ACL refused before the file was
        # opened), so there is nothing to remove and nothing to report.
        pass
    except OSError:
        logger.warning(f"Failed to remove temp log file: {path}")
    try:
        os.rmdir(os.path.dirname(path))  # the mkdtemp() parent is empty once the CSV is gone
    except OSError:
        pass


# Caps for log downloads: the CSV is streamed to a temp file (never buffered
# twice or inlined into the tool payload) and only a bounded preview is
# returned so large downloads cannot blow up the LLM context. The total
# download size itself is capped separately and configured in config.py
# (NEXTDNS_DOWNLOAD_MAX_BYTES).
DOWNLOAD_PREVIEW_MAX_LINES = 20
DOWNLOAD_PREVIEW_MAX_BYTES = 256 * 1024

# Maximum number of redirects followed when downloading logs.
_MAX_DOWNLOAD_REDIRECTS = 5

# SSRF guard for the download redirect chain (issue #266). The follow-up fetch
# is unauthenticated, so a ``Location`` from the API response is checked before
# any request is made to it: a hop is refused when its scheme is not https, or
# when its host resolves to a non-globally-routable address (loopback, private,
# link-local and unique-local ranges, which is what covers 127.0.0.1,
# 10/8-style ranges and the 169.254.169.254 metadata endpoint). Any other
# public https destination is followed, including a host other than the API
# origin. The scheme check applies to pinned hosts too, so a pin can never
# allow a plaintext fetch.
#
# Known properties of this reachability check:
# - The vetted address IS the dialled address (issue #266 review): while the
#   hop is fetched, ``socket.getaddrinfo`` is pinned to the answer that was
#   vetted (``_pinned_dns``), so httpcore connects to the vetted IP instead of
#   resolving the name again — the DNS-rebinding / TOCTOU window between
#   "vetted" and "dialled" is closed rather than accepted. The request URL,
#   TLS SNI, certificate verification and the Host header all stay hostname-
#   based; only the dialled address is pinned. The patch is process-global for
#   the duration of one hop's fetch (``socket`` is the only resolver httpcore
#   reaches through); other names resolve normally during the window, and the
#   pinned name resolves to addresses already vetted as public.
# - A resolution failure REFUSES the hop (fail closed, see
#   ``_resolve_public_addresses``): the guard does not degrade to
#   allow when DNS misbehaves.
ALLOWED_DOWNLOAD_SCHEMES = {"https"}

# Verified public hosts allowed to serve log downloads even though they do not
# resolve to a globally routable address. Intentionally empty: the real NextDNS
# download host is not yet verified, so nothing is pinned here. To pin one, add
# its hostname to this set with a comment saying why it is trusted.
ALLOWED_DOWNLOAD_HOSTS: frozenset[str] = frozenset()


class DownloadRedirectRefusedError(Exception):
    """Raised when a log-download ``Location`` is not a public https destination.

    SSRF guard for the redirect chain: the unauthenticated follow-up fetch is
    never aimed at a non-https or non-globally-routable destination, so a
    compromised or malicious API response cannot use it to reach an internal
    service such as a cloud metadata endpoint (issue #266). Public destinations
    on any host remain reachable.
    """


def _is_public_address(address: str | int) -> bool:
    """Return whether a resolved address is a globally routable unicast address."""
    parsed = ipaddress.ip_address(address)
    return parsed.is_global and not parsed.is_multicast and not parsed.is_reserved


async def _resolve_public_addresses(host: str) -> list[Any] | None:
    """The vetted addrinfo entries for ``host``, or None when the hop must be refused.

    Resolution runs off the event loop because it can block on DNS.

    Fails closed: a name that does not resolve - resolver down, NXDOMAIN,
    empty answer - is refused, not waved through. The old fail-open reading
    argued an unresolvable host cannot be connected to anyway; that argument
    holds only while this guard's resolver and httpx's answer the same
    question with the same result, and a guard that disables itself whenever
    DNS misbehaves is not a guard. The redirect can wait for a working
    resolver; the metadata endpoint cannot wait for anything.

    Returning the vetted entries (not just a bool) is what lets the fetch
    connect to *these* addresses via ``_pinned_dns`` instead of resolving the
    name a second time and hoping the answer held still.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos or not all(_is_public_address(info[4][0]) for info in infos):
        return None
    return list(infos)


async def _host_resolves_to_public_address(host: str) -> bool:
    """Whether ``host`` resolves only to globally routable addresses."""
    return await _resolve_public_addresses(host) is not None


# The pinned-resolver swap is process-global (httpcore reaches the resolver
# only through the ``socket`` module attribute), so two downloads running
# their check/fetch windows at the same moment could restore each other's
# wrapper out of order and leave a stale pin installed, or continue one hop
# unpinned. ``_PIN_FETCH_LOCK`` serializes the complete vet-and-fetch
# critical section across all concurrent downloadLogs calls: for one
# download at a time, every hop's resolution, connect, header read and body
# stream run with its own pin installed and nothing else's. A
# per-connection resolver would avoid the serialization, but httpx/httpcore
# expose no hook for one short of replacing the transport stack wholesale;
# serializing is the honest trade, and downloadLogs is a rare call (issue
# #266 review). httpx ties body streaming to the open stream context, so the
# body is inside the section rather than re-resolving outside it.
_PIN_FETCH_LOCK = asyncio.Lock()


@contextlib.contextmanager
def _pinned_dns(host: str | None, infos: list[Any] | None) -> Iterator[None]:
    """Make ``socket.getaddrinfo`` return the vetted ``infos`` for ``host``.

    Installed for the duration of one hop's fetch, so the connection httpcore
    opens dials the address that was vetted, closing the rebinding window
    (module note above). httpcore resolves through the event loop, which
    reaches ``socket.getaddrinfo`` by module attribute at call time - the only
    hook available without replacing the transport stack. The swap is
    process-global while active, so the caller must hold
    :data:`_PIN_FETCH_LOCK` across check-and-fetch: with the lock, no other
    download's swap can interleave, restore out of order, or leave this
    window unpinned. Other name lookups (any host but ``host``) pass through
    to the real resolver while the pin is installed. A no-op for pinned
    allow hosts (``infos is None``), which skip resolution entirely.
    """
    if host is None or infos is None:
        yield
        return
    real = socket.getaddrinfo

    def pinned(name: Any, *args: Any, **kwargs: Any) -> Any:
        resolved_name = name.decode() if isinstance(name, (bytes, bytearray)) else str(name)
        if resolved_name == host:
            return infos
        return real(name, *args, **kwargs)

    socket.getaddrinfo = pinned  # type: ignore[assignment]  # typeshed overloads are narrower than the swap
    try:
        yield
    finally:
        socket.getaddrinfo = real


async def _check_download_redirect_target(next_url: httpx.URL) -> list[Any] | None:
    """Vet one redirect hop; return its vetted addresses, or None for a pinned host.

    Raises:
        DownloadRedirectRefusedError: If the scheme is not in
        :data:`ALLOWED_DOWNLOAD_SCHEMES`, or the host is not pinned in
        :data:`ALLOWED_DOWNLOAD_HOSTS` and does not resolve to only globally
        routable addresses.
    """
    if next_url.scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise DownloadRedirectRefusedError(
            f"Refusing log download redirect to {next_url.scheme}://{next_url.host}: scheme not allowed"
        )
    if next_url.host in ALLOWED_DOWNLOAD_HOSTS:
        return None
    infos = await _resolve_public_addresses(next_url.host)
    if infos is None:
        raise DownloadRedirectRefusedError(
            f"Refusing log download redirect to {next_url.scheme}://{next_url.host}: host is not publicly routable"
        )
    return infos


class DownloadTooLargeError(Exception):
    """Raised when a download exceeds the total-size cap mid-stream."""


def _preview_full(preview: list[str], preview_bytes: int) -> bool:
    return len(preview) >= DOWNLOAD_PREVIEW_MAX_LINES or preview_bytes >= DOWNLOAD_PREVIEW_MAX_BYTES


def _append_preview(preview: list[str], preview_bytes: int, text: str) -> int:
    """Add this chunk's leading lines to the preview, within the line and byte caps.

    Returns the new preview byte count. A preview that already hit either cap
    returns before the chunk is split into lines at all: past 20 lines or
    256 KB there is nothing any later chunk can add, and ``splitlines()`` on
    every remaining chunk of a 1 GiB CSV is work that buys nothing. A first
    line that would cross the byte cap still trips the same break the
    per-chunk guard used before the extraction.
    """
    if _preview_full(preview, preview_bytes):
        return preview_bytes
    for line in text.splitlines(keepends=True):
        if _preview_full(preview, preview_bytes):
            break
        line_bytes = len(line.encode("utf-8"))
        if preview_bytes + line_bytes > DOWNLOAD_PREVIEW_MAX_BYTES:
            break
        preview.append(line)
        preview_bytes += line_bytes
    return preview_bytes


# Read off a failed stream at most this much of the body, so http_error_payload
# can quote the upstream error document without reading a hostile multi-megabyte
# failure page into memory. The size is errors.py's own body slice, so the read
# always covers what the payload will show.
_ERROR_BODY_READ_BYTES = BODY_SLICE_BYTES


async def _bounded_error_body(response: httpx.Response) -> bytes:
    parts: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        parts.append(chunk)
        total += len(chunk)
        if total >= _ERROR_BODY_READ_BYTES:
            break
    return b"".join(parts)


async def _raise_for_streamed_status(response: httpx.Response) -> None:
    """``raise_for_status`` that leaves the failed response's body readable.

    A streaming response raises ``httpx.ResponseNotRead`` from ``.json()`` and
    ``.text`` until it has been read, and ``http_error_payload`` - which the
    download handlers build from the raised error - reads both. Before this, a
    mid-download 503 escaped as ``ResponseNotRead`` instead of the typed
    payload. The bounded error document is read here and re-raised around a
    buffered copy of the response, so the payload carries what upstream said
    without buffering an unbounded body.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if not e.response.is_stream_consumed:
            body = await _bounded_error_body(e.response)
            e.response = httpx.Response(
                status_code=e.response.status_code,
                headers=e.response.headers,
                content=body,
                request=e.response.request,
            )
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


def _previewable(carry: str, text: str) -> tuple[str, str]:
    """Split a stream piece into whole leading lines and the trailing fragment.

    A chunk boundary is not a record boundary: only whole lines reach the
    preview, and the unterminated tail rides over to the next chunk, so a CSV
    record is never previewed as two fragments (or dropped and counted).
    """
    piece = carry + text
    if piece.endswith("\n"):
        return piece, ""
    cut = piece.rfind("\n")
    if cut == -1:
        return "", piece
    return piece[: cut + 1], piece[cut + 1 :]


def _skip_dropped_record_tail(piece: str, dropping: bool) -> tuple[str, bool]:
    """Cut the leading fragment of a record already dropped from the preview.

    ``piece`` is whole lines, so a non-empty piece always ends with a newline:
    its first line is the dropped record's tail (or several lines of it), and
    only what follows the first newline is a record of its own and previewable.
    Without this, a record longer than the preview cap would re-surface in the
    preview as a fresh record every time its tail crossed a chunk boundary.
    """
    if not dropping or not piece:
        return piece, dropping
    return piece[piece.find("\n") + 1 :], False


def _flush_tail(
    preview: list[str], preview_bytes: int, carry: str, open_line: bool, dropping_record: bool
) -> tuple[int, int]:
    """The stream's final unterminated record: extra rows and the new preview size.

    ``dropping_record`` means the record still being read was already dropped
    from the preview for crossing the byte cap; its leftover tail at EOF
    belongs to that same record, so previewing it would resurrect the dropped
    record as a phantom line. The row itself still counts - it exists in the
    file whether or not the preview shows it.
    """
    if carry and not dropping_record:
        preview_bytes = _append_preview(preview, preview_bytes, carry)
    return (1 if open_line else 0), preview_bytes


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
    preview: list[str] = []
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
    # fdopen (not open) so the file handle stays usable from the event loop
    # without tripping ASYNC230; per-chunk writes are small and cheap.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
        async for raw, text in _stream_pieces(response, decoder):
            if written_bytes + len(raw) > max_total:
                raise DownloadTooLargeError(
                    f"Download exceeds the {max_total}-byte limit "
                    f"(NEXTDNS_DOWNLOAD_MAX_BYTES); aborted after {written_bytes} bytes."
                )
            written_bytes += len(raw)
            if raw:
                out.write(raw)
            row_count += text.count("\n")
            open_line = not text.endswith("\n")
            piece, preview_carry = _previewable(preview_carry, text)
            if len(preview_carry) > DOWNLOAD_PREVIEW_MAX_BYTES:
                # A single record longer than the whole preview can never fit
                # in it: drop the carry instead of growing it with the line.
                preview_carry = ""
                dropping_record = True
            piece, dropping_record = _skip_dropped_record_tail(piece, dropping_record)
            preview_bytes = _append_preview(preview, preview_bytes, piece)
    extra_rows, preview_bytes = _flush_tail(preview, preview_bytes, preview_carry, open_line, dropping_record)
    row_count += extra_rows
    size = os.path.getsize(path)
    text_preview = "".join(preview)
    return {
        "content_type": response.headers.get("content-type"),
        "file_path": path,
        "size": size,
        "row_count": row_count,
        "preview": {
            "text": text_preview,
            "line_count": len(preview),
            "bytes": preview_bytes,
            "truncated": preview_bytes < size,
        },
    }


async def _follow_redirects_to_tempfile(next_url: httpx.URL, path: str) -> dict[str, Any]:
    """Follow a redirect chain to the final CSV, streaming it to ``path``.

    The ``Location`` chain is fetched with an unauthenticated client so the
    ``X-Api-Key`` header never crosses to a redirect target. Relative
    ``Location`` headers are resolved against the origin of the current URL.
    Redirects are bounded so a loop cannot hang the call, and every hop (the
    first included) is checked to be https on a globally routable host, so a
    redirect cannot aim the fetch at an internal or metadata service.

    The whole chain runs under ``_PIN_FETCH_LOCK``: vetting and fetching must
    be one indivisible section process-wide, or a concurrent download's
    resolver swap interleaves with this one (see the module note).
    """
    redirects = 0
    unauthenticated = httpx.AsyncClient(timeout=client.get_http_timeout(), follow_redirects=False)
    try:
        async with _PIN_FETCH_LOCK:
            while True:
                # Checked before every fetch, so the first ``Location`` is covered
                # by the same rule as the rest of the chain; the fetch then dials
                # the vetted addresses rather than re-resolving the name.
                infos = await _check_download_redirect_target(next_url)
                with _pinned_dns(next_url.host, infos):
                    async with unauthenticated.stream("GET", next_url, headers={"Accept-Encoding": "identity"}) as response:
                        if response.has_redirect_location:
                            redirects += 1
                            if redirects > _MAX_DOWNLOAD_REDIRECTS:
                                raise httpx.TooManyRedirects(
                                    f"Exceeded {_MAX_DOWNLOAD_REDIRECTS} redirects", request=response.request
                                )
                            next_url = response.request.url.join(response.headers["location"])
                            continue
                        return await _write_stream_to_tempfile(response, path)
    finally:
        await unauthenticated.aclose()


async def _stream_download_to_path(profile_id: str, path: str) -> dict[str, Any]:
    """Stream the log download for ``profile_id`` into ``path`` and return the payload."""
    async with client.api_client.stream("GET", f"/profiles/{profile_id}/logs/download", headers={"Accept-Encoding": "identity"}) as initial:
        # A non-redirect response is written straight to disk;
        # _write_stream_to_tempfile surfaces any HTTP error via
        # raise_for_status.
        if not initial.has_redirect_location or initial.is_error:
            return await _write_stream_to_tempfile(initial, path)
        redirect_url = initial.request.url.join(initial.headers["location"])
    return await _follow_redirects_to_tempfile(redirect_url, path)


async def _download_logs_controlled(profile_id: str, path: str) -> tuple[dict[str, Any], bool]:
    """Stream the CSV to ``path``, mapping typed failures to their payloads.

    Returns a ``(payload, downloaded)`` pair; ``downloaded`` is True only when
    the whole body was written, which is what tells the caller's cleanup guard
    to keep the file. HTTP and unexpected errors are re-raised for the caller's
    handlers, in the same order the single-function version caught them.
    """
    try:
        payload = await _stream_download_to_path(profile_id, path)
        return payload, True
    except DownloadRedirectRefusedError as e:
        # A redirect to a non-public or plaintext destination is refused
        # before any request is made to the target, so no foreign body is ever
        # written or returned. The message carries only scheme and host: the
        # signed part of the URL never reaches the log or the payload. The
        # temp file and its parent are removed by the caller's finally guard,
        # which covers every non-successful exit.
        logger.warning(f"Refusing log download redirect: {e}")
        return (
            error_payload(
                ErrorCode.HTTP_ERROR, "Refusing log download redirect to a non-public or non-https destination"
            ),
            False,
        )
    except AccessDeniedError as e:
        # Raised by the ACL layer (via stream()) before any network request;
        # kept out of the httpx.HTTPError branch so a real upstream 403 keeps
        # its existing http_error path.
        logger.warning(f"Access denied downloading logs: {_log_safe_error(e)}")
        return access_denied_payload_for(e), False
    except DownloadTooLargeError as e:
        # Raised mid-stream when the total-size cap is exceeded; the partially
        # written CSV and its parent are removed by the caller's guard.
        logger.warning(f"Download aborted, size cap exceeded: {e}")
        return error_payload(ErrorCode.DOWNLOAD_TOO_LARGE, str(e)), False




def _download_error_details(error: Exception) -> tuple[Any, Any, Any]:
    """Best-effort (status, scheme, host) from an httpx error, without raising."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    url = None
    try:
        request = getattr(error, "request", None)
    except RuntimeError:
        request = None
    if request is not None:
        try:
            url = getattr(request, "url", None)
        except Exception:  # noqa: BLE001
            url = None
    scheme = getattr(url, "scheme", None)
    host = getattr(url, "host", None)
    return status, scheme, host


async def _download_logs_to_tempfile(profile_id: str) -> dict[str, Any]:
    """Stream a log CSV download to a temp file without double-buffering.

    The response body is streamed directly to disk (bounded by the OS temp
    file), a bounded preview of the first lines is kept in memory, and the
    tool payload returns only the file path, size, row count, and preview —
    never the full CSV text.

    Every exit that is not a successful download removes the temp file and its
    mkdtemp parent, including cancellation of the awaiting task, so an
    abandoned download leaves neither an empty directory nor a partial CSV
    behind.

    The authenticated client is used only for the initial request to the
    NextDNS API, and never follows redirects: if the download endpoint
    redirects (for example to object storage), the ``Location`` is fetched with
    an unauthenticated client so the ``X-Api-Key`` header is never sent to
    another host. That unauthenticated fetch only visits https destinations on
    globally routable hosts, so a hostile ``Location`` cannot turn the download
    into a probe of an internal service.
    """
    path = os.path.join(tempfile.mkdtemp(prefix="nextdns_logs_"), "download.csv")
    downloaded = False
    try:
        payload, downloaded = await _download_logs_controlled(profile_id, path)
        return payload
    except httpx.HTTPError as e:
        logger.error(f"HTTP error downloading logs: {_log_safe_error(e)}")
        status, scheme, host = _download_error_details(e)
        return http_error_payload(
            f"HTTP error while downloading logs: {status} {scheme}://{host}".strip(),
            e,
            fallback_code=ErrorCode.HTTP_ERROR,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error downloading logs: {_log_safe_error(e)}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Unexpected error while downloading logs: {e}")
    finally:
        # Every exit that is not a successful download — including a cancelled
        # task, which reaches neither handler — removes the temp file and its
        # mkdtemp parent, so an abandoned download leaves nothing behind.
        if not downloaded:
            _unlink_temp_file(path)
