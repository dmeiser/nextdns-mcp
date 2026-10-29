"""Grouped list management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Annotated, Any, Literal, get_args

from ..coercion import ProfileId, _coerce_json_arg, before_validator, operation_aliases
from ..errors import ErrorCode, error_payload
from ..utils import _api_request_payload, _optional_entry_id_error, resolve_profile_id

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

# "list the allowlist" is how callers ask for operation="get" (7 calls across
# three live runs). See coercion.operation_aliases for the rule and its limits.
ListOperationId = Annotated[
    ListOperation, before_validator(operation_aliases(get_args(ListOperation), {"list": "get", "delete": "remove"}))
]


def _list_type_alias(value: Any) -> Any:
    """Accept an unambiguous short name for a list type.

    Clients reach for ``"blocklists"`` where the parameter is
    ``"privacy_blocklists"``: two live runs produced 38 calls with exactly that
    value, every one rejected by the schema before the tool could answer. A name
    that is the trailing segment of exactly one list type is accepted. Anything
    ambiguous is passed through untouched, so the schema error can list the
    valid values rather than the server guessing wrong.
    """
    if not isinstance(value, str) or value in _LIST_PATHS:
        return value
    wanted = value.strip().lower().replace("-", "_").replace(" ", "_")
    matches = [name for name in _LIST_PATHS if wanted == name or wanted == name.split("_")[-1]]
    return matches[0] if len(matches) == 1 else value


ListTypeId = Annotated[ListType, before_validator(_list_type_alias)]

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
    return await _api_request_payload("GET", base_url)


async def _lists_add(base_url: str, entry: str | dict[str, Any] | None) -> dict[str, Any]:
    """Add a single entry to a list."""
    if entry is None:
        return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "entry is required for add operation")
    entry = _coerce_json_arg(entry)
    body = _relax_list_entry(entry) if isinstance(entry, dict) else {"id": entry}
    entry_error = _validate_list_entry(body, "entry", "add", hint=_ADD_ENTRY_HINT)
    if entry_error:
        return entry_error
    result = await _api_request_payload("POST", base_url, json_body=body)
    return _idempotent_list_result(result, "add") or result


def _validate_list_entry(entry: Any, label: str, operation: str, **extra: Any) -> dict[str, Any] | None:
    """Return an error dict if a list entry does not carry a string ``id``.

    The OpenAPI schemas for the ``add*`` and ``replace*`` list operations declare
    each entry as an object with a required string ``id`` field (e.g.
    ``{"id": "blocked.com"}``). Nested body fields are not checked by
    ``StripExtraFieldsMiddleware``, so a malformed entry used to be forwarded
    verbatim and come back as an opaque upstream HTTP 400. Validate locally and
    name the offending entry (``label``) so the caller gets a typed, actionable
    error instead (issues #189, #253).
    """
    if not isinstance(entry, dict):
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"{label} must be an object with an 'id' field for {operation} operation",
            **extra,
        )
    if not isinstance(entry.get("id"), str):
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"{label} must have a string 'id' field for {operation} operation",
            **extra,
        )
    return None


def _relax_list_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the NextDNS entry schema does not define (issue: AI clients).

    ``StripExtraFieldsMiddleware`` relaxes extra *top-level* tool arguments but
    cannot see inside ``entry``/``entries`` payloads, so an entry like
    ``{"id": "x.com", "value": "x.com"}`` used to reach the API and come back
    as an opaque ``400 {"code":"extraneous"}``. The upstream entry schema only
    defines ``id`` and ``active`` for every list type, so unknown keys are
    dropped here — the same silent-relaxation contract the middleware applies
    at the top level, applied one level down.
    """
    return {k: v for k, v in entry.items() if k in ("id", "active")}


def _coerce_replace_entry(entry: Any) -> Any:
    """Accept bare strings as list entries: ``"x.com"`` -> ``{"id": "x.com"}``.

    Models frequently pass ``entries=["x.com"]`` even though the schema asks
    for objects; the intent is unambiguous, so coerce instead of rejecting.
    """
    if isinstance(entry, str):
        return {"id": entry}
    if isinstance(entry, dict):
        return _relax_list_entry(entry)
    return entry


def _validate_replace_entries(entries: list[Any]) -> dict[str, Any] | None:
    """Return an error dict if a replace payload has a malformed entry (issue #189)."""
    for index, entry in enumerate(entries):
        error = _validate_list_entry(entry, f"entries[{index}]", "replace", index=index, hint=_REPLACE_ENTRIES_HINT)
        if error:
            return error
    return None


async def _lists_replace(base_url: str, entries: str | list[Any] | None) -> dict[str, Any]:
    """Replace the entire list with a new set of entries."""
    if entries is None:
        return error_payload(ErrorCode.MISSING_REQUIRED_ARGUMENT, "entries is required for replace operation")
    entries = _coerce_json_arg(entries)
    if not isinstance(entries, list):
        return error_payload(ErrorCode.INVALID_ARGUMENT, "entries must be a JSON array")
    entries = [_coerce_replace_entry(entry) for entry in entries]
    entry_error = _validate_replace_entries(entries)
    if entry_error:
        return entry_error
    return await _api_request_payload("PUT", base_url, json_body=entries)


async def _lists_update(
    base_url: str,
    list_type: ListTypeId,
    entry_id: str | None,
    entry: str | dict[str, Any] | None,
) -> dict[str, Any]:
    """Update a single list entry by id (only for supported list types)."""
    if list_type not in _LIST_UPDATEABLE_TYPES:
        return error_payload(
            ErrorCode.UNSUPPORTED_OPERATION,
            f"update is not supported for list_type={list_type}",
            supported_list_types=sorted(_LIST_UPDATEABLE_TYPES),
        )
    if entry_id is None:
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT, "entry_id is required for update operation", hint=_ENTRY_ID_HINT
        )
    entry = _coerce_json_arg(entry)
    if not isinstance(entry, dict):
        return error_payload(ErrorCode.INVALID_ARGUMENT, "entry must be a dict for update operation")
    result = await _api_request_payload("PATCH", f"{base_url}/{entry_id}", json_body=entry)
    if isinstance(result, dict) and result.get("upstream_code") == "notFound":
        result = {**result, "hint": _ENTRY_ID_HINT}
    return result


# What an entry_id looks like for each list type: the id IS the entry value,
# which is not obvious from the parameter name, and a guessed id comes back as
# an opaque upstream 404.
_ENTRY_ID_HINT = (
    "entry_id is the entry's own id as returned by operation='get': the hostname for "
    "allowlist/denylist, the blocklist name for privacy_blocklists, the TLD for security_tlds, "
    "and the category/service name for parental_categories/parental_services"
)

# Seen live: an add sent entry={"active": true} with the value in entry_id, and a
# replace sent entry_id="alexa". Both names exist on the tool, so the caller had
# no way to tell they belong to the other operations.
_ADD_ENTRY_HINT = (
    'entry is the value to add: a plain string ("ads.example.com") or an object with a '
    'string id ({"id": "ads.example.com"}). entry_id names an existing entry and is '
    "not how a new one is added"
)

_REPLACE_ENTRIES_HINT = (
    'entries is the whole replacement list: ["zip"] or [{"id": "zip"}]. '
    "entry_id names one existing entry and is not used by replace"
)


async def _lists_remove(base_url: str, entry_id: str | None) -> dict[str, Any]:
    """Remove a single list entry by id."""
    if entry_id is None:
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT, "entry_id is required for remove operation", hint=_ENTRY_ID_HINT
        )
    result = await _api_request_payload("DELETE", f"{base_url}/{entry_id}")
    return _idempotent_list_result(result, "remove") or result


# Upstream error codes meaning the list is already in the requested state.
_IDEMPOTENT_UPSTREAM: dict[str, frozenset[str]] = {
    "add": frozenset({"duplicate"}),
    "remove": frozenset({"notFound"}),
}

_IDEMPOTENT_NOTE: dict[str, str] = {
    "add": "entry already present; nothing to add",
    "remove": "entry already absent; nothing to remove",
}


def _idempotent_list_result(result: dict[str, Any], operation: str) -> dict[str, Any] | None:
    """Report an already-satisfied add/remove as a success, not a failure.

    ``add`` of an entry the list already holds comes back upstream as
    ``duplicate`` (HTTP 400 for blocklists/TLDs, HTTP 200 with an error
    document for allow/deny), and ``remove`` of an absent entry comes back as
    ``notFound``. In every one of those cases the caller's intended end state
    is already true, so answering with a failure only sends the caller into a
    retry loop against a list that is already correct. The success payload
    stays honest about what it absorbed by naming the upstream code.

    Returns None when the result is not an "already in that state" error, so
    the caller returns the original payload unchanged.
    """
    if not isinstance(result, dict):
        return None
    codes = result.get("upstream_codes")
    if not isinstance(codes, list):
        single = result.get("upstream_code")
        codes = [single] if isinstance(single, str) else []
    matched = sorted({str(c) for c in codes} & _IDEMPOTENT_UPSTREAM[operation])
    if not matched:
        return None
    return {"success": True, "note": _IDEMPOTENT_NOTE[operation], "upstream_code": matched[0]}


async def _manage_lists_impl(
    list_type: ListTypeId,
    operation: ListOperationId,
    profile_id: ProfileId,
    entry_id: str | None = None,
    entry: str | dict[str, Any] | None = None,
    entries: str | list[Any] | None = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for content/privacy/security/parental lists."""
    target_profile, error = resolve_profile_id(profile_id, allow_default=False)
    if error:
        return error
    if target_profile is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution failed")

    error = _optional_entry_id_error(entry_id)
    if error:
        return error

    # `entry` is the singular of both `entries` (replace) and, for these list
    # types, `entry_id` (remove: the id IS the value, see _ENTRY_ID_HINT). Live
    # runs show callers reaching for the singular in exactly those two
    # operations, so accept it rather than rejecting a call that names the
    # right thing. Only the singular: a malformed value under the plural
    # `entries` stays the shape error it has always been.
    if operation == "replace" and entries is None and entry is not None:
        coerced = _coerce_json_arg(entry)
        entries = [coerced] if isinstance(coerced, (str, dict)) else coerced
    if operation == "remove" and entry_id is None and isinstance(entry, str):
        entry_id = entry

    path = _LIST_PATHS[list_type]
    base_url = f"/profiles/{target_profile}/{path}"

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

    return error_payload(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {operation}")


async def manageLists(
    list_type: ListTypeId,
    operation: ListOperationId,
    profile_id: ProfileId,
    entry_id: str | None = None,
    entry: str | dict[str, Any] | None = None,
    entries: str | list[Any] | None = None,
) -> dict[str, Any]:
    """Get, add, replace, update or remove entries in a NextDNS profile's lists:
    allowlist, denylist, privacy blocklists, privacy natives, security TLDs, and
    parental-control categories and services.

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
          The entry must carry a string ``id``; an entry without one is rejected locally with
          ``invalid_argument`` rather than sent upstream.
        - ``remove``: Delete one entry by ``entry_id``. The ``entry_id`` IS the entry's value:
          the hostname for allowlist/denylist, the TLD for security_tlds, the category or
          service name for parental lists. Removing an absent entry is idempotent success.
        - ``update``: Toggle an existing entry by ``entry_id`` (pass ``entry={"active": True|False}``).
          Only supported for ``allowlist``, ``denylist``, ``parental_categories``, and
          ``parental_services``.
        - ``replace``: Replace the entire list with ``entries``. Each entry is an object with a
          string ``id``, or a bare string, which is read as ``{"id": "..."}``; a malformed entry
          is rejected locally with ``invalid_argument`` naming its index, rather than sent upstream.

    Examples:
        - get: ``manageLists(list_type="denylist", operation="get", profile_id="abc123")``
        - add: ``manageLists(list_type="denylist", operation="add", profile_id="abc123", entry={"id": "example.com"})``
        - remove: ``manageLists(list_type="denylist", operation="remove", profile_id="abc123", entry_id="example.com")``
        - replace: ``manageLists(list_type="privacy_blocklists", operation="replace", profile_id="abc123", entries=[{"id": "nextdns-recommended"}])``
        - add parental category: ``manageLists(list_type="parental_categories", operation="add", profile_id="abc123", entry={"id": "gambling"})``
        - update parental service: ``manageLists(list_type="parental_services", operation="update", profile_id="abc123", entry_id="tiktok", entry={"active": False})``
    """
    return await _manage_lists_impl(
        list_type=list_type,
        operation=operation,
        profile_id=profile_id,
        entry_id=entry_id,
        entry=entry,
        entries=entries,
    )
