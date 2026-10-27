"""Grouped DNS rewrite management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal

from ..coercion import ProfileId
from ..errors import ErrorCode, error_payload
from ..utils import _api_request_payload, _optional_entry_id_error, resolve_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
RewriteOperation = Literal["list", "add", "delete"]


async def _resolve_rewrite_id(base_url: str, name: str) -> tuple[str | None, dict[str, Any] | None]:
    """Find a rewrite's server-assigned id from its name (the natural key).

    ``delete`` is keyed by the opaque id the API assigns, but callers think in
    terms of the hostname they added. Resolving the name here saves the caller
    a list-then-delete round trip it would otherwise skip, inventing an id
    instead and failing with a 404.
    """
    listed = await _api_request_payload("GET", base_url)
    if listed.get("error"):
        return None, listed
    for entry in listed.get("data") or []:
        if isinstance(entry, dict) and entry.get("name") == name and entry.get("id"):
            return str(entry["id"]), None
    return None, error_payload(
        ErrorCode.INVALID_ENTRY_ID,
        f"no rewrite named {name!r} on this profile",
        hint="call operation='list' to see each rewrite's name and id",
    )


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
    if target_profile is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution failed")

    error = _optional_entry_id_error(entry_id)
    if error:
        return error

    base_url = f"/profiles/{target_profile}/rewrites"

    if operation == "list":
        return await _api_request_payload("GET", base_url)

    if operation == "add":
        if not name or not content:
            return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "name and content are required for add operation")
        return await _api_request_payload("POST", base_url, json_body={"name": name, "content": content})

    if operation == "delete":
        if not entry_id and name:
            resolved, resolve_error = await _resolve_rewrite_id(base_url, name)
            if resolve_error:
                return resolve_error
            entry_id = resolved
        if not entry_id:
            return error_payload(
                ErrorCode.MISSING_REQUIRED_ARGUMENT,
                "entry_id is required for delete operation",
                hint="pass entry_id from operation='list', or pass the rewrite's name and it will be resolved",
            )
        return await _api_request_payload("DELETE", f"{base_url}/{entry_id}")

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
        - ``delete``: Remove a rewrite. Pass ``entry_id`` (from ``list``) or just the ``name``
          that was added; the id is resolved from the name when it is missing.

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
