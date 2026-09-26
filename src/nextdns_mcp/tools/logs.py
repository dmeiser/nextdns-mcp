"""Grouped query log management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import os
import tempfile
from typing import Any, Literal

import httpx

from .. import client
from ..coercion import ProfileId
from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import _api_request, _build_query_params, _cap_limit, _validate_profile_id

logger = logging.getLogger(__name__)


def _unlink_temp_file(path: str) -> None:
    """Best-effort removal of a temp file; never masks the original error."""
    try:
        os.unlink(path)
    except OSError:
        logger.warning(f"Failed to remove temp log file: {path}")


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


async def _write_stream_to_tempfile(response: httpx.Response, path: str) -> dict[str, Any]:
    """Write a streaming response body to ``path`` without double-buffering.

    The full CSV is written straight to disk chunk by chunk; only a bounded
    preview of the leading lines is kept in memory.
    """
    response.raise_for_status()
    preview: list[str] = []
    preview_bytes = 0
    row_count = 0
    total_chars = 0
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
            total_chars += len(text)
            newlines = text.count("\n")
            row_count += newlines
            open_line = not text.endswith("\n")
            if len(preview) < DOWNLOAD_PREVIEW_MAX_LINES and preview_bytes < DOWNLOAD_PREVIEW_MAX_BYTES:
                for line in text.splitlines(keepends=True):
                    if len(preview) >= DOWNLOAD_PREVIEW_MAX_LINES:
                        break
                    if preview_bytes + len(line) > DOWNLOAD_PREVIEW_MAX_BYTES:
                        break
                    preview.append(line)
                    preview_bytes += len(line)
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
            "truncated": len(text_preview) < total_chars,
        },
    }


async def _follow_redirects_to_tempfile(next_url: httpx.URL, path: str) -> dict[str, Any]:
    """Follow a redirect chain to the final CSV, streaming it to ``path``.

    The ``Location`` chain is fetched with an unauthenticated client so the
    ``X-Api-Key`` header never crosses to a redirect target. Relative
    ``Location`` headers are resolved against the origin of the current URL.
    Redirects are bounded so a loop cannot hang the call.
    """
    redirects = 0
    unauthenticated = httpx.AsyncClient(timeout=client.get_http_timeout(), follow_redirects=False)
    try:
        while True:
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
    redirects (S3-style object storage), the ``Location`` is fetched with an
    unauthenticated client so the ``X-Api-Key`` header is never sent to a
    host outside the NextDNS API origin.
    """
    path = os.path.join(tempfile.mkdtemp(prefix="nextdns_logs_"), "download.csv")
    try:
        async with client.api_client.stream("GET", f"/profiles/{profile_id}/logs/download") as initial:
            # A non-redirect response (including the synthetic 403 from the
            # access-controlled client, which carries no real redirect target)
            # is written straight to disk; _write_stream_to_tempfile surfaces
            # any HTTP error via raise_for_status.
            if not initial.has_redirect_location or initial.is_error:
                return await _write_stream_to_tempfile(initial, path)
            redirect_url = initial.request.url.join(initial.headers["location"])
        return await _follow_redirects_to_tempfile(redirect_url, path)
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
    error = _validate_profile_id(profile_id)
    if error:
        return error

    base_url = f"/profiles/{profile_id}/logs"

    if operation == "get":
        capped_limit, _ = _cap_limit(limit, LOGS_LIMIT_MAX)
        params = _build_query_params(
            **{"from": from_time, "to": to_time, "limit": capped_limit, "device": device, "search": user, "raw": raw}
        )
        return await _api_request("GET", base_url, params=params)

    if operation == "clear":
        return await _api_request("DELETE", base_url)

    if operation == "download":
        return await _download_logs_to_tempfile(profile_id)

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
