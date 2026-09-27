"""Grouped query log management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
from typing import Any, Literal

import httpx

from .. import client
from ..client import AccessDeniedError
from ..coercion import ProfileId
from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import _api_request, _build_query_params, _cap_limit, access_denied_payload, resolve_profile_id

logger = logging.getLogger(__name__)


def _unlink_temp_file(path: str) -> None:
    """Best-effort removal of the temp file AND its mkdtemp parent; never masks the original error."""
    try:
        os.unlink(path)
    except OSError:
        logger.warning(f"Failed to remove temp log file: {path}")
    try:
        os.rmdir(os.path.dirname(path))  # the mkdtemp() parent is empty once the CSV is gone
    except OSError:
        pass


# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
LogOperation = Literal["get", "clear", "download"]

# Server-side cap for the ``limit`` parameter on ``get`` (maximum accepted by
# the NextDNS API).
LOGS_LIMIT_MAX = 1000

# Caps for log downloads: the CSV is streamed to a temp file (never buffered
# twice or inlined into the tool payload) and only a bounded preview is
# returned so large multi-MB/GB downloads cannot blow up the LLM context.
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


async def _write_stream_to_tempfile(response: httpx.Response, path: str) -> dict[str, Any]:
    """Write a streaming response body to ``path`` without double-buffering.

    The full CSV is written straight to disk chunk by chunk; only a bounded
    preview of the leading lines is kept in memory.
    """
    response.raise_for_status()
    preview: list[str] = []
    preview_bytes = 0
    row_count = 0
    total_bytes = 0
    # End-of-stream flag, not per-chunk state: it is recomputed for every
    # chunk and is only true when the last chunk left a line unterminated.
    open_line = False
    # fdopen (not open) so the file handle stays usable from the event loop
    # without tripping ASYNC230; per-chunk writes are small and cheap.
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", errors="replace"
    ) as out:
        async for text in response.aiter_text():
            out.write(text)
            if not text:
                continue
            total_bytes += len(text.encode("utf-8"))
            newlines = text.count("\n")
            row_count += newlines
            open_line = not text.endswith("\n")
            if len(preview) < DOWNLOAD_PREVIEW_MAX_LINES and preview_bytes < DOWNLOAD_PREVIEW_MAX_BYTES:
                for line in text.splitlines(keepends=True):
                    if len(preview) >= DOWNLOAD_PREVIEW_MAX_LINES:
                        break
                    line_bytes = len(line.encode("utf-8"))
                    if preview_bytes + line_bytes > DOWNLOAD_PREVIEW_MAX_BYTES:
                        break
                    preview.append(line)
                    preview_bytes += line_bytes
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
            "truncated": preview_bytes < total_bytes,
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


async def _download_logs_to_tempfile(profile_id: ProfileId) -> dict[str, Any]:
    """Stream a log CSV download to a temp file without double-buffering.

    The response body is streamed directly to disk (bounded by the OS temp
    file), a bounded preview of the first lines is kept in memory, and the
    tool payload returns only the file path, size, row count, and preview —
    never the full CSV text.

    The authenticated client is used only for the initial request to the
    NextDNS API, and never follows redirects: if the download endpoint
    redirects (for example to object storage), the ``Location`` is fetched with
    an unauthenticated client so the ``X-Api-Key`` header is never sent to
    another host. That unauthenticated fetch only visits https destinations on
    globally routable hosts, so a hostile ``Location`` cannot turn the download
    into a probe of an internal service.
    """
    path = os.path.join(tempfile.mkdtemp(prefix="nextdns_logs_"), "download.csv")
    try:
        async with client.api_client.stream("GET", f"/profiles/{profile_id}/logs/download") as initial:
            # A non-redirect response is written straight to disk;
            # _write_stream_to_tempfile surfaces any HTTP error via
            # raise_for_status.
            if not initial.has_redirect_location or initial.is_error:
                return await _write_stream_to_tempfile(initial, path)
            redirect_url = initial.request.url.join(initial.headers["location"])
        return await _follow_redirects_to_tempfile(redirect_url, path)
    except DownloadRedirectRefusedError as e:
        # A redirect to a non-public or plaintext destination is refused
        # before any request is made to the target, so no foreign body is ever
        # written or returned. The message carries only scheme and host: the
        # signed part of the URL never reaches the log or the payload.
        _unlink_temp_file(path)
        logger.warning(f"Refusing log download redirect: {e}")
        return error_payload(
            ErrorCode.HTTP_ERROR, "Refusing log download redirect to a non-public or non-https destination"
        )
    except AccessDeniedError as e:
        # Raised by the ACL layer (via stream()) before any network request;
        # kept out of the httpx.HTTPError branch so a real upstream 403 keeps
        # its existing http_error path.
        _unlink_temp_file(path)
        logger.warning(f"Access denied downloading logs: {e}")
        return access_denied_payload(e)
    except httpx.HTTPError as e:
        _unlink_temp_file(path)
        logger.error(f"HTTP error downloading logs: {e}")
        return http_error_payload(f"HTTP error while downloading logs: {e}", e, fallback_code=ErrorCode.HTTP_ERROR)
    except Exception as e:  # noqa: BLE001
        _unlink_temp_file(path)
        logger.error(f"Unexpected error downloading logs: {e}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Unexpected error while downloading logs: {e}")


async def _manage_logs_impl(
    operation: LogOperation,
    profile_id: ProfileId,
    from_time: str | int | None = None,
    to_time: str | int | None = None,
    limit: int | None = None,
    user: str | None = None,
    device: str | None = None,
    raw: bool | None = None,
) -> dict[str, Any]:
    """Grouped implementation for query logs (get, clear, download)."""
    target_profile, error = resolve_profile_id(profile_id, allow_default=False)
    if error:
        return error
    assert target_profile is not None

    base_url = f"/profiles/{target_profile}/logs"

    if operation == "get":
        capped_limit, _ = _cap_limit(limit, LOGS_LIMIT_MAX)
        params = _build_query_params(
            **{"from": from_time, "to": to_time, "limit": capped_limit, "device": device, "search": user, "raw": raw}
        )
        return await _api_request("GET", base_url, params=params)

    if operation == "clear":
        return await _api_request("DELETE", base_url)

    if operation == "download":
        return await _download_logs_to_tempfile(target_profile)

    return error_payload(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {operation}")


async def manageLogs(
    operation: LogOperation,
    profile_id: ProfileId,
    from_time: str | int | None = None,
    to_time: str | int | None = None,
    limit: int | None = None,
    user: str | None = None,
    device: str | None = None,
    raw: bool | None = None,
) -> dict[str, Any]:
    """Manage query logs for a NextDNS profile.

    Operations:
        - ``get``: Return recent query log entries (use ``limit`` to cap results).
          Set ``raw=true`` to bypass deduplication/noise filtering.
        - ``clear``: Delete all stored logs for the profile.
        - ``download``: Download retained logs as CSV. ``from_time`` and ``to_time`` are
          ignored by the NextDNS download endpoint. The CSV is streamed to a
          temporary file; only the file path, size, row count, and a small
          capped preview are returned (the full text is never inlined).
          On success the temp file is *not* cleaned up: the CSV stays in the
          OS temp directory (mode 0600) and the returned path is the only
          record of it, so delete it when it is no longer needed. Failed
          downloads do remove the temp file and its parent directory.

    Time values can be Unix timestamps or relative strings like ``-1d`` or ``-7d``.
    They are only used by ``get``.

    ``limit`` is capped server-side at 1000 entries for ``get`` (the maximum
    accepted by the NextDNS API).

    Examples:
        - get recent: ``manageLogs(operation="get", profile_id="abc123", limit=10)``
        - get raw logs: ``manageLogs(operation="get", profile_id="abc123", raw=true)``
        - download: ``manageLogs(operation="download", profile_id="abc123", from_time="-1d")``
        - clear: ``manageLogs(operation="clear", profile_id="abc123")``
    """
    return await _manage_logs_impl(operation, profile_id, from_time, to_time, limit, user, device, raw)
