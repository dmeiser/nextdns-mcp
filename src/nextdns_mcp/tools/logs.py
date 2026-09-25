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


async def _download_logs_to_tempfile(profile_id: ProfileId) -> dict[str, Any]:
    """Stream a log CSV download to a temp file without double-buffering.

    The response body is streamed directly to disk (bounded by the OS temp
    file), a bounded preview of the first lines is kept in memory, and the
    tool payload returns only the file path, size, row count, and preview —
    never the full CSV text.
    """
    fd: int | None
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="nextdns_logs_")
    try:
        async with client.api_client.stream(
            "GET", f"/profiles/{profile_id}/logs/download", follow_redirects=True
        ) as response:
            response.raise_for_status()
            preview: list[str] = []
            preview_bytes = 0
            row_count = 0
            total_chars = 0
            open_line = False
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as out:
                fd = None  # ownership transferred to the file object
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
    except httpx.HTTPError as e:
        if fd is not None:
            os.close(fd)
        _unlink_temp_file(path)
        logger.error(f"HTTP error downloading logs: {e}")
        error_response = getattr(e, "response", None)
        status_code = error_response.status_code if error_response is not None else None
        return {
            "error": f"HTTP error {status_code} while downloading logs: {e}",
            "status_code": status_code,
        }
    except Exception as e:
        if fd is not None:
            os.close(fd)
        _unlink_temp_file(path)
        logger.error(f"Unexpected error downloading logs: {e}")
        raise RuntimeError(f"Unexpected error while downloading logs: {e}") from e


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

    return {"error": f"Unsupported operation: {operation}"}


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
