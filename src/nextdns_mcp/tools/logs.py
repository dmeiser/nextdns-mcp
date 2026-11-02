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
import ipaddress
import logging
import os
import socket
import tempfile
from typing import Any

import httpx

from .. import client
from ..client import AccessDeniedError, _log_safe_error
from ..config import get_download_max_bytes
from ..errors import (
    ErrorCode,
    error_payload,
    http_error_payload,
)
from ..utils import access_denied_payload

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
# Known limitations of this reachability check, not bugs to fix here:
# - The host is resolved here and resolved again by httpx when it connects, so
#   a name whose answer changes in between (DNS rebinding) is not closed by it.
# - A resolution failure (offline, unresolvable name) allows the hop through:
#   such a host cannot be connected to anyway, and httpx reports the failure
#   through the normal HTTP-error path.
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


async def _host_resolves_to_public_address(host: str) -> bool:
    """Return whether every address ``host`` resolves to is globally routable.

    Resolution runs off the event loop because it can block on DNS. A name that
    does not resolve is not treated as a refusal: such a host cannot be
    connected to anyway, and httpx surfaces the connection error through the
    normal HTTP-error path. The address vetted here is not the address httpx
    dials, since httpx resolves the name again when it connects.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError:
        return True
    return all(_is_public_address(info[4][0]) for info in infos)


async def _check_download_redirect_target(next_url: httpx.URL) -> None:
    """Raise :class:`DownloadRedirectRefusedError` unless ``next_url`` is a public https destination.

    Args:
        next_url: The resolved URL about to be fetched (the first hop's
            ``Location`` or a later hop's).

    Raises:
        DownloadRedirectRefusedError: If the scheme is not in
            :data:`ALLOWED_DOWNLOAD_SCHEMES`, or the host is not pinned in
            :data:`ALLOWED_DOWNLOAD_HOSTS` and does not resolve to a globally
            routable address.
    """
    if next_url.scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise DownloadRedirectRefusedError(
            f"Refusing log download redirect to {next_url.scheme}://{next_url.host}: scheme not allowed"
        )
    if next_url.host in ALLOWED_DOWNLOAD_HOSTS:
        return
    if not await _host_resolves_to_public_address(next_url.host):
        raise DownloadRedirectRefusedError(
            f"Refusing log download redirect to {next_url.scheme}://{next_url.host}: host is not publicly routable"
        )


class DownloadTooLargeError(Exception):
    """Raised when a download exceeds the total-size cap mid-stream."""


def _append_preview(preview: list[str], preview_bytes: int, text: str) -> int:
    """Add this chunk's leading lines to the preview, within the line and byte caps.

    Returns the new preview byte count. A preview that already hit either cap
    collects nothing: the first line trips the same break the per-chunk guard
    used before the extraction.
    """
    for line in text.splitlines(keepends=True):
        if len(preview) >= DOWNLOAD_PREVIEW_MAX_LINES:
            break
        line_bytes = len(line.encode("utf-8"))
        if preview_bytes + line_bytes > DOWNLOAD_PREVIEW_MAX_BYTES:
            break
        preview.append(line)
        preview_bytes += line_bytes
    return preview_bytes


async def _write_stream_to_tempfile(response: httpx.Response, path: str) -> dict[str, Any]:
    """Write a streaming response body to ``path`` without double-buffering.

    The full CSV is written straight to disk chunk by chunk; only a bounded
    preview of the leading lines is kept in memory. The total bytes written
    are tracked and, once the running total exceeds the total-size cap
    (the ``NEXTDNS_DOWNLOAD_MAX_BYTES`` environment variable, default 1 GiB,
    see :func:`nextdns_mcp.config.get_download_max_bytes`), a
    :class:`DownloadTooLargeError`
    is raised immediately — while streaming, not after the body is in hand —
    so a single download cannot fill the temp directory.
    """
    response.raise_for_status()
    max_total = get_download_max_bytes()
    preview: list[str] = []
    preview_bytes = 0
    row_count = 0
    written_bytes = 0
    # End-of-stream flag, not per-chunk state: it is recomputed for every
    # chunk and is only true when the last chunk left a line unterminated.
    open_line = False
    # fdopen (not open) so the file handle stays usable from the event loop
    # without tripping ASYNC230; per-chunk writes are small and cheap.
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", errors="replace"
    ) as out:
        async for text in response.aiter_text():
            if not text:
                continue
            chunk_bytes = len(text.encode("utf-8"))
            if written_bytes + chunk_bytes > max_total:
                raise DownloadTooLargeError(
                    f"Download exceeds the {max_total}-byte limit "
                    f"(NEXTDNS_DOWNLOAD_MAX_BYTES); aborted after {written_bytes} bytes."
                )
            written_bytes += chunk_bytes
            out.write(text)
            newlines = text.count("\n")
            row_count += newlines
            open_line = not text.endswith("\n")
            preview_bytes = _append_preview(preview, preview_bytes, text)
    if open_line:
        row_count += 1
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
    """
    redirects = 0
    unauthenticated = httpx.AsyncClient(timeout=client.get_http_timeout(), follow_redirects=False)
    try:
        while True:
            # Checked before every fetch, so the first ``Location`` is covered
            # by the same rule as the rest of the chain.
            await _check_download_redirect_target(next_url)
            async with unauthenticated.stream("GET", next_url) as response:
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
    async with client.api_client.stream("GET", f"/profiles/{profile_id}/logs/download") as initial:
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
        return access_denied_payload(e), False
    except DownloadTooLargeError as e:
        # Raised mid-stream when the total-size cap is exceeded; the partially
        # written CSV and its parent are removed by the caller's guard.
        logger.warning(f"Download aborted, size cap exceeded: {e}")
        return error_payload(ErrorCode.DOWNLOAD_TOO_LARGE, str(e)), False


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
        return http_error_payload(f"HTTP error while downloading logs: {e}", e, fallback_code=ErrorCode.HTTP_ERROR)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error downloading logs: {_log_safe_error(e)}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Unexpected error while downloading logs: {e}")
    finally:
        # Every exit that is not a successful download — including a cancelled
        # task, which reaches neither handler — removes the temp file and its
        # mkdtemp parent, so an abandoned download leaves nothing behind.
        if not downloaded:
            _unlink_temp_file(path)
