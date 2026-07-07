"""Grouped list management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal, Optional, Union

from ..coercion import ProfileId, _coerce_json_arg
from ..utils import _api_request, _validate_entry_id, _validate_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
ListType = Literal[
    "allowlist",
    "denylist",
    "privacy_blocklists",
    "privacy_natives",
    "security_tlds",
    "parental_categories",
    "parental_services",
]
ListOperation = Literal["get", "add", "replace", "update", "remove"]

# List types that support per-entry PATCH updates.
_LIST_UPDATEABLE_TYPES: set[ListType] = {
    "allowlist",
    "denylist",
    "parental_categories",
    "parental_services",
}

# Path-segment mappings for grouped CRUD tools.
_LIST_PATHS: dict[ListType, str] = {
    "allowlist": "allowlist",
    "denylist": "denylist",
    "privacy_blocklists": "privacy/blocklists",
    "privacy_natives": "privacy/natives",
    "security_tlds": "security/tlds",
    "parental_categories": "parentalControl/categories",
    "parental_services": "parentalControl/services",
}


async def _lists_get(base_url: str) -> dict[str, Any]:
    """Fetch the current list entries."""
    return await _api_request("GET", base_url)


async def _lists_add(base_url: str, entry: Optional[Union[str, dict[str, Any]]]) -> dict[str, Any]:
    """Add a single entry to a list."""
    if entry is None:
        return {"error": "entry is required for add operation"}
    entry = _coerce_json_arg(entry)
    body = entry if isinstance(entry, dict) else {"id": entry}
    return await _api_request("POST", base_url, json=body)


async def _lists_replace(base_url: str, entries: Optional[Union[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """Replace the entire list with a new set of entries."""
    if entries is None:
        return {"error": "entries is required for replace operation"}
    entries = _coerce_json_arg(entries)
    if not isinstance(entries, list):
        return {"error": "entries must be a JSON array"}
    return await _api_request("PUT", base_url, json=entries)


async def _lists_update(
    base_url: str,
    list_type: ListType,
    entry_id: Optional[str],
    entry: Optional[Union[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Update a single list entry by id (only for supported list types)."""
    if list_type not in _LIST_UPDATEABLE_TYPES:
        return {
            "error": f"update is not supported for list_type={list_type}",
            "supported_list_types": sorted(_LIST_UPDATEABLE_TYPES),
        }
    if entry_id is None:
        return {"error": "entry_id is required for update operation"}
    entry = _coerce_json_arg(entry)
    if not isinstance(entry, dict):
        return {"error": "entry must be a dict for update operation"}
    return await _api_request("PATCH", f"{base_url}/{entry_id}", json=entry)


async def _lists_remove(base_url: str, entry_id: Optional[str]) -> dict[str, Any]:
    """Remove a single list entry by id."""
    if entry_id is None:
        return {"error": "entry_id is required for remove operation"}
    return await _api_request("DELETE", f"{base_url}/{entry_id}")


async def _manage_lists_impl(
    list_type: ListType,
    operation: ListOperation,
    profile_id: ProfileId,
    entry_id: Optional[str] = None,
    entry: Optional[Union[str, dict[str, Any]]] = None,
    entries: Optional[Union[str, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for content/privacy/security/parental lists."""
    error = _validate_profile_id(profile_id)
    if error:
        return error

    if entry_id is not None:
        error = _validate_entry_id(entry_id)
        if error:
            return error

    path = _LIST_PATHS[list_type]
    base_url = f"/profiles/{profile_id}/{path}"

    if operation == "get":
        return await _lists_get(base_url)
    if operation == "add":
        return await _lists_add(base_url, entry)
    if operation == "replace":
        return await _lists_replace(base_url, entries)
    if operation == "update":
        return await _lists_update(base_url, list_type, entry_id, entry)
    if operation == "remove":
        return await _lists_remove(base_url, entry_id)

    return {"error": f"Unsupported operation: {operation}"}


async def manageLists(
    list_type: ListType,
    operation: ListOperation,
    profile_id: ProfileId,
    entry_id: Optional[str] = None,
    entry: Optional[Union[str, dict[str, Any]]] = None,
    entries: Optional[Union[str, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Manage allow/deny/block lists for a NextDNS profile.

    List types:
        - ``allowlist`` / ``denylist``: Always-allow or always-block specific domains.
        - ``privacy_blocklists``: Subscribed blocklists (e.g., ``nextdns-recommended``).
        - ``privacy_natives``: Native tracking blockers (e.g., ``apple``, ``facebook``).
        - ``security_tlds``: Dangerous top-level domains to block (e.g., ``zip``).
        - ``parental_categories``: Content categories (e.g., ``gambling``, ``porn``).
        - ``parental_services``: Specific apps/services (e.g., ``tiktok``, ``youtube``).

    Operations:
        - ``get``: Return the current list.
        - ``add``: Append one entry (pass ``entry`` as ``{"id": "value"}`` or as a plain id string).
        - ``remove``: Delete one entry by ``entry_id``.
        - ``update``: Toggle an existing entry by ``entry_id`` (pass ``entry={"active": True|False}``).
          Only supported for ``allowlist``, ``denylist``, ``parental_categories``, and
          ``parental_services``.
        - ``replace``: Replace the entire list with ``entries`` (list of dicts).

    Examples:
        - get: ``manageLists(list_type="denylist", operation="get", profile_id="abc123")``
        - add: ``manageLists(list_type="denylist", operation="add", profile_id="abc123", entry={"id": "example.com"})``
        - remove: ``manageLists(list_type="denylist", operation="remove", profile_id="abc123", entry_id="example.com")``
        - replace: ``manageLists(list_type="privacy_blocklists", operation="replace", profile_id="abc123", entries=[{"id": "nextdns-recommended"}])``
    """
    return await _manage_lists_impl(list_type, operation, profile_id, entry_id, entry, entries)
