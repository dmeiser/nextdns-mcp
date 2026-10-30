"""Grouped profile management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Annotated, Any, Literal, get_args

from ..coercion import OptionalProfileId, before_validator, operation_aliases
from ..config import load_profile_access_control
from ..errors import (
    ErrorCode,
    error_payload,
)
from ..utils import _api_request_payload, _build_query_params, resolve_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
ProfileOperation = Literal["list", "create", "get", "update", "delete"]

# Callers name the intent rather than the enum: live runs show manageProfiles
# asked for rename, set, replace and add. Each means exactly one thing for this
# tool, so it is resolved; anything else still gets the schema error.
#
# `remove` is deliberately NOT an alias for `delete` here, even though a live
# run showed it. On this tool the verb is ambiguous - a caller doing "remove X
# from the denylist" reaches for manageProfiles(operation="remove", profile_id=…)
# and the alias would turn that wrong-tool call into a deleted profile. An
# alias is only safe when the operation it selects is what the caller most
# likely meant *and* being wrong is recoverable; deleting a profile is neither.
# `delete` is in the published enum, so a caller that means it says it, and the
# schema error lists it for one that does not.
ProfileOperationId = Annotated[
    ProfileOperation,
    before_validator(
        operation_aliases(
            get_args(ProfileOperation),
            {"rename": "update", "set": "update", "replace": "update", "add": "create"},
            tool="manageProfiles",
        )
    ),
]


async def _profiles_list(cursor: str | None = None) -> dict[str, Any]:
    # One snapshot for the whole operation (issue #257), like the client
    # request and dohLookup paths.
    access = load_profile_access_control()
    if not access.any_readable:
        return error_payload(ErrorCode.READ_ACCESS_DENIED, "Read access denied: no profiles are readable")
    params = _build_query_params(cursor=cursor)
    result = await _api_request_payload("GET", "/profiles", params=params or None)
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
        # The caller has a profile_id in hand from an earlier call and reaches
        # for it; `name` is the new profile's own display name, which no
        # earlier call returned.
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT,
            "name is required for create operation",
            hint="name is the new profile's own display label - any label you choose, "
            "not a profile_id and not a name taken from an example",
        )
    return await _api_request_payload("POST", "/profiles", json_body={"name": name})


async def _profiles_update(url: str, name: str | None) -> dict[str, Any]:
    if not name:
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT,
            "name is required for update operation",
            hint="operation='update' renames the profile and takes no other fields; "
            "settings, lists, rewrites and logs are separate tools",
        )
    return await _api_request_payload("PATCH", url, json_body={"name": name})


async def _manage_profiles_impl(
    operation: ProfileOperationId,
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
    if target_profile is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution failed")

    url = f"/profiles/{target_profile}"
    if operation == "get":
        return await _api_request_payload("GET", url)
    if operation == "update":
        return await _profiles_update(url, name)
    if operation == "delete":
        return await _api_request_payload("DELETE", url)

    return error_payload(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {operation}")


async def manageProfiles(
    operation: ProfileOperationId,
    profile_id: OptionalProfileId = None,
    name: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Manage NextDNS profiles.

    A profile is the account-level object that the other tools act on; this tool
    manages the profile record itself — list, create, get, update, delete. Most
    other tools require a ``profile_id`` from this tool. Taking an entry out of
    something *inside* a profile is that tool's job, not this one: a list entry
    is ``manageLists``, a DNS rewrite is ``manageRewrites``.

    Operations:
        - ``list``: Return profiles the API key can access (supports ``cursor`` for pagination).
        - ``create``: Create a new profile (requires ``name``).
        - ``get``: Retrieve a single profile (requires ``profile_id``).
        - ``update``: Rename a profile (requires ``profile_id`` and ``name``).
        - ``delete``: Delete a profile permanently (requires ``profile_id``). This
          deletes the profile and everything configured in it; there is no undo.

    Examples:
        - list:        ``manageProfiles(operation="list")``
        - list (page): ``manageProfiles(operation="list", cursor="j2k3zl3b4v")``
        - create:      ``manageProfiles(operation="create", name="Home Network")``
        - get:         ``manageProfiles(operation="get", profile_id="<profile id>")``
    """
    return await _manage_profiles_impl(
        operation=operation,
        profile_id=profile_id,
        name=name,
        cursor=cursor,
    )
