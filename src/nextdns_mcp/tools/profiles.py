"""Grouped profile management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal

from ..coercion import OptionalProfileId
<<<<<<< HEAD
=======
from ..config import get_readable_profiles_set, get_writable_profiles_set, is_read_only
from ..errors import ErrorCode, error_payload, http_error_payload, NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError
from ..utils import _api_request, _build_query_params, resolve_profile_id, _handle_api_error
>>>>>>> 554f833 (Fix review gate r2: fix json parameter shadowing, remove 2xx API-level error sniffing, fix error subclass __init__ overrides, update _api_request callers and tests)
import httpx

from ..config import load_profile_access_control
from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import (
    NextDNSAuthError,
    NextDNSError,
    NextDNSRateLimitError,
    NextDNSServerError,
    _api_request,
    _build_query_params,
    resolve_profile_id,
)

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
ProfileOperation = Literal["list", "create", "get", "update", "delete"]


async def _profiles_list(cursor: str | None = None) -> dict[str, Any]:
    # One snapshot for the whole operation (issue #257), like the client
    # request and dohLookup paths.
    access = load_profile_access_control()
    if not access.any_readable:
        return error_payload(ErrorCode.READ_ACCESS_DENIED, "Read access denied: no profiles are readable")
    params = _build_query_params(cursor=cursor)
    try:
            result = await _api_request("GET", "/profiles", params=params or None)
    except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
        result = _handle_api_error(e)
    if isinstance(result, dict) and "meta" in result and isinstance(result["meta"], dict):
        pagination = result["meta"].get("pagination")
        if isinstance(pagination, dict) and pagination.get("cursor"):
            result.setdefault("cursor", pagination["cursor"])
    return result


async def _profiles_create(name: str | None) -> dict[str, Any]:
    # One snapshot for the whole operation (issue #257), so the read-only
    # check and the writable-set check cannot disagree with each other.
    access = load_profile_access_control()
    if access.read_only:
        return error_payload(ErrorCode.WRITE_ACCESS_DENIED, "Write operation denied: server is in read-only mode")
    if not access.any_writable:
        return error_payload(ErrorCode.WRITE_ACCESS_DENIED, "Write access denied: no profiles are writable")
    if not name:
        return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "name is required for create operation")
    try:
            return await _api_request("POST", "/profiles", json_body={"name": name})
    except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
        return _handle_api_error(e)


async def _profiles_update(url: str, name: str | None) -> dict[str, Any]:
    if not name:
        return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "name is required for update operation")
    try:
            return await _api_request("PATCH", url, json_body={"name": name})
    except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
        return _handle_api_error(e)


async def _manage_profiles_impl(
    operation: ProfileOperation,
    profile_id: OptionalProfileId = None,
    name: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for NextDNS profiles."""
    if operation == "list":
        return await _profiles_list(cursor=cursor)

    if operation == "create":
        return await _profiles_create(name)

    target_profile, error = resolve_profile_id(profile_id, allow_default=False)
    if error:
        return error
    assert target_profile is not None

    url = f"/profiles/{target_profile}"
    if operation == "get":
        try:
                    return await _api_request("GET", url)
        except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
            return _handle_api_error(e)
    if operation == "update":
        return await _profiles_update(url, name)
    if operation == "delete":
        try:
                    return await _api_request("DELETE", url)
        except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
            return _handle_api_error(e)

    return error_payload(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {operation}")


async def manageProfiles(
    operation: ProfileOperation,
    profile_id: OptionalProfileId = None,
    name: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Manage NextDNS profiles.

    NextDNS profiles are named configurations that contain DNS settings, blocklists,
    analytics, and logs. Most other tools require a ``profile_id`` from this tool.

    Operations:
        - ``list``: Return profiles the API key can access (supports ``cursor`` for pagination).
        - ``create``: Create a new profile (requires ``name``).
        - ``get``: Retrieve a single profile (requires ``profile_id``).
        - ``update``: Rename a profile (requires ``profile_id`` and ``name``).
        - ``delete``: Remove a profile (requires ``profile_id``).

    Examples:
        - list:        ``manageProfiles(operation="list")``
        - list (page): ``manageProfiles(operation="list", cursor="j2k3zl3b4v")``
        - create:      ``manageProfiles(operation="create", name="Home Network")``
        - get:         ``manageProfiles(operation="get", profile_id="abc123")``
    """
    return await _manage_profiles_impl(
        operation=operation,
        profile_id=profile_id,
        name=name,
        cursor=cursor,
    )
