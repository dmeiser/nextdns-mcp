"""Grouped query log management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
from typing import Any, Literal, Optional

import httpx

from .. import client
from ..coercion import ProfileId
from ..utils import _api_request, _build_query_params, _validate_profile_id

logger = logging.getLogger(__name__)

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
LogOperation = Literal["get", "clear", "download"]


async def _manage_logs_impl(
    operation: LogOperation,
    profile_id: ProfileId,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    limit: Optional[int] = None,
    user: Optional[str] = None,
    device: Optional[str] = None,
    raw: Optional[bool] = None,
) -> dict[str, Any]:
    """Grouped implementation for query logs (get, clear, download)."""
    error = _validate_profile_id(profile_id)
    if error:
        return error

    base_url = f"/profiles/{profile_id}/logs"

    if operation == "get":
        params = _build_query_params(
            **{"from": from_time, "to": to_time, "limit": limit, "device": device, "search": user, "raw": raw}
        )
        return await _api_request("GET", base_url, params=params)

    if operation == "clear":
        return await _api_request("DELETE", base_url)

    if operation == "download":
        try:
            response = await client.api_client.get(f"{base_url}/download", follow_redirects=True)
            response.raise_for_status()
            return {
                "content_type": response.headers.get("content-type"),
                "size": len(response.content),
                "data": response.text,
            }
        except httpx.HTTPError as e:
            logger.error(f"HTTP error downloading logs: {e}")
            error_response = getattr(e, "response", None)
            status_code = error_response.status_code if error_response is not None else None
            body = error_response.text if error_response is not None else None
            return {
                "error": f"HTTP error {status_code} while downloading logs: {e}",
                "response_body": body,
                "status_code": status_code,
            }
        except Exception as e:
            logger.error(f"Unexpected error downloading logs: {e}")
            raise RuntimeError(f"Unexpected error while downloading logs: {e}") from e

    return {"error": f"Unsupported operation: {operation}"}


async def manageLogs(
    operation: LogOperation,
    profile_id: ProfileId,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    limit: Optional[int] = None,
    user: Optional[str] = None,
    device: Optional[str] = None,
    raw: Optional[bool] = None,
) -> dict[str, Any]:
    """Manage query logs for a NextDNS profile.

    Operations:
        - ``get``: Return recent query log entries (use ``limit`` to cap results).
          Set ``raw=true`` to bypass deduplication/noise filtering.
        - ``clear``: Delete all stored logs for the profile.
        - ``download``: Download retained logs as CSV. ``from_time`` and ``to_time`` are
          ignored by the NextDNS download endpoint.

    Time values can be Unix timestamps or relative strings like ``-1d`` or ``-7d``.
    They are only used by ``get``.

    Examples:
        - get recent: ``manageLogs(operation="get", profile_id="abc123", limit=10)``
        - get raw logs: ``manageLogs(operation="get", profile_id="abc123", raw=true)``
        - download: ``manageLogs(operation="download", profile_id="abc123", from_time="-1d")``
        - clear: ``manageLogs(operation="clear", profile_id="abc123")``
    """
    return await _manage_logs_impl(operation, profile_id, from_time, to_time, limit, user, device, raw)
