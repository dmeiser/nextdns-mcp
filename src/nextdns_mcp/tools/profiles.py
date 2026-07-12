"""Grouped profile management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal, Optional

from ..coercion import OptionalProfileId
from ..config import get_readable_profiles_set, get_writable_profiles_set, is_read_only
from ..utils import _api_request, _validate_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
ProfileOperation = Literal["list", "create", "get", "update", "delete"]


async def _manage_profiles_impl(
    operation: ProfileOperation,
    profile_id: OptionalProfileId = None,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for NextDNS profiles."""
    if operation == "list":
        if get_readable_profiles_set() is None:
            return {"error": "Read access denied: no profiles are readable"}
        return await _api_request("GET", "/profiles")

    if operation == "create":
        if is_read_only():
            return {"error": "Write operation denied: server is in read-only mode"}
        if get_writable_profiles_set() is None:
            return {"error": "Write access denied: no profiles are writable"}
        if not name:
            return {"error": "name is required for create operation"}
        return await _api_request("POST", "/profiles", json={"name": name})

    if not profile_id:
        return {"error": "profile_id is required for this operation"}

    error = _validate_profile_id(profile_id)
    if error:
        return error

    url = f"/profiles/{profile_id}"
    if operation == "get":
        return await _api_request("GET", url)
    if operation == "update":
        if not name:
            return {"error": "name is required for update operation"}
        return await _api_request("PATCH", url, json={"name": name})
    if operation == "delete":
        return await _api_request("DELETE", url)

    return {"error": f"Unsupported operation: {operation}"}


async def manageProfiles(
    operation: ProfileOperation,
    profile_id: OptionalProfileId = None,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """Manage NextDNS profiles.

    NextDNS profiles are named configurations that contain DNS settings, blocklists,
    analytics, and logs. Most other tools require a ``profile_id`` from this tool.

    Operations:
        - ``list``: Return all profiles the API key can access.
        - ``create``: Create a new profile (requires ``name``).
        - ``get``: Retrieve a single profile (requires ``profile_id``).
        - ``update``: Rename a profile (requires ``profile_id`` and ``name``).
        - ``delete``: Remove a profile (requires ``profile_id``).

    Examples:
        - list:  ``manageProfiles(operation="list")``
        - create: ``manageProfiles(operation="create", name="Home Network")``
        - get:   ``manageProfiles(operation="get", profile_id="abc123")``
    """
    return await _manage_profiles_impl(operation, profile_id, name)
