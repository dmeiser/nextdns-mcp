"""Grouped DNS rewrite management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal, Optional

from ..coercion import ProfileId
from ..utils import _api_request, _validate_entry_id, _validate_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
RewriteOperation = Literal["list", "add", "delete"]


async def _manage_rewrites_impl(
    operation: RewriteOperation,
    profile_id: ProfileId,
    name: Optional[str] = None,
    content: Optional[str] = None,
    entry_id: Optional[str] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for DNS rewrite entries."""
    error = _validate_profile_id(profile_id)
    if error:
        return error

    if entry_id is not None:
        error = _validate_entry_id(entry_id)
        if error:
            return error

    base_url = f"/profiles/{profile_id}/rewrites"

    if operation == "list":
        return await _api_request("GET", base_url)

    if operation == "add":
        if not name or not content:
            return {"error": "name and content are required for add operation"}
        return await _api_request("POST", base_url, json={"name": name, "content": content})

    if operation == "delete":
        if not entry_id:
            return {"error": "entry_id is required for delete operation"}
        return await _api_request("DELETE", f"{base_url}/{entry_id}")

    return {"error": f"Unsupported operation: {operation}"}


async def manageRewrites(
    operation: RewriteOperation,
    profile_id: ProfileId,
    name: Optional[str] = None,
    content: Optional[str] = None,
    entry_id: Optional[str] = None,
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
        - delete: ``manageRewrites(operation="delete", profile_id="abc123", entry_id="router.home")``
    """
    return await _manage_rewrites_impl(operation, profile_id, name, content, entry_id)
