"""Grouped DNS rewrite management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal

from ..coercion import ProfileId
from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import _api_request, _validate_entry_id, resolve_profile_id, NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError
import httpx

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
RewriteOperation = Literal["list", "add", "delete"]


async def _manage_rewrites_impl(
    operation: RewriteOperation,
    profile_id: ProfileId,
    name: str | None = None,
    content: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for DNS rewrite entries."""
    target_profile, error = resolve_profile_id(profile_id, allow_default=False)
    if error:
        return error

    if entry_id is not None:
        error = _validate_entry_id(entry_id)
        if error:
            return error

    base_url = f"/profiles/{target_profile}/rewrites"

    if operation == "list":
        try:
                    return await _api_request("GET", base_url)
        except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
            cause = e.__cause__
            if cause is not None and isinstance(cause, httpx.HTTPError):
                return http_error_payload(str(e), cause)
            else:
                return error_payload(ErrorCode.INTERNAL_ERROR, str(e))

    if operation == "add":
        if not name or not content:
            return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "name and content are required for add operation")
        try:
                    return await _api_request("POST", base_url, json={"name": name, "content": content})
        except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
            cause = e.__cause__
            if cause is not None and isinstance(cause, httpx.HTTPError):
                return http_error_payload(str(e), cause)
            else:
                return error_payload(ErrorCode.INTERNAL_ERROR, str(e))

    if operation == "delete":
        if not entry_id:
            return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "entry_id is required for delete operation")
        try:
                    return await _api_request("DELETE", f"{base_url}/{entry_id}")
        except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
            cause = e.__cause__
            if cause is not None and isinstance(cause, httpx.HTTPError):
                return http_error_payload(str(e), cause)
            else:
                return error_payload(ErrorCode.INTERNAL_ERROR, str(e))

    return error_payload(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {operation}")


async def manageRewrites(
    operation: RewriteOperation,
    profile_id: ProfileId,
    name: str | None = None,
    content: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Manage DNS rewrite entries for a NextDNS profile.

    Rewrites let you return a custom answer for a hostname. Typical uses:
    - Point an internal hostname to a private IP.
    - Block a domain by rewriting it to ``0.0.0.0``.

    Operations:
        - ``list``: Show existing rewrites.
        - ``add``: Create a rewrite (requires ``name`` and ``content``).
        - ``delete``: Remove a rewrite (requires ``entry_id`` from ``list``).

    Examples:
        - list: ``manageRewrites(operation="list", profile_id="abc123")``
        - add: ``manageRewrites(operation="add", profile_id="abc123", name="router.home", content="192.168.1.1")``
        - delete: ``manageRewrites(operation="delete", profile_id="abc123", entry_id="<id-from-list>")``
    """
    return await _manage_rewrites_impl(
        operation=operation,
        profile_id=profile_id,
        name=name,
        content=content,
        entry_id=entry_id,
    )
