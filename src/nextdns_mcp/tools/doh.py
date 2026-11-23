"""DNS-over-HTTPS lookup tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT

This is one of the three composite tools the server performs itself (alongside
``plotAnalytics`` and ``overviewDashboard``). It deliberately does *not* go through the
access-controlled API client: the DoH endpoint is a different host
(``dns.nextdns.io``, allow-listed in ``client.ALLOWED_DESTINATION_HOSTS``), it
answers DNS questions rather than REST resources, and its URL carries the
profile id as the path, so the ACL layer's "extract the profile id from a
``/profiles/...`` path" authorization has nothing to authorize. The read gate
is instead enforced here, twice against one environment snapshot (issue #179):
once before deciding to query and once on the function that sends. That
substitution of gate-for-transport is the one place the server sends an
authorized request without ``AccessControlledClient`` holding the door, and it
is the reason this module builds its own denial payload through the shared
transport helper rather than depending on the ACL's ``AccessDeniedError``.

Three names, one operation, in the order a call passes through them. One of
the three is the published tool, the other two are internal: the server
registers a composite tool under the wrapper function's own ``__name__``
(``server._COMPOSITE_TOOLS``), so ``dohLookup`` is what callers see and it
keeps the camelCase of the generated operation tools, while the wrapper itself
exists to hold the MCP-facing signature and docstring apart from the request
logic (AGENT.md). ``_dohLookup_impl`` is the private half that wrapper
forwards to by keyword: profile resolution, the one access snapshot the whole
lookup is authorized by, record-type validation, and the resolver URL.
``doh_lookup`` is the function that sends - snake_case because it is a helper
with no tool name of its own - and the read gate is checked again on it, so
the decision to query and the query itself are authorized by the same
environment snapshot (issue #179). Renaming any of the three is not a
tidy-up: ``dohLookup`` is the name in every caller's tool list, and
``doh_lookup`` is the send-time gate the access-control tests call directly.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

from ..client import _log_safe_error
from ..coercion import OptionalProfileId
from ..config import (
    ProfileAccessControl,
    get_http_timeout,
    load_profile_access_control,
)
from ..constants import DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES
from ..errors import ErrorCode, StateOwnershipError, error_payload, http_error_payload
from ..state import request_resources
from ..transport import access_denied_payload
from ..utils import resolve_default_profile

logger = logging.getLogger(__name__)


# The DoH client persists for keep-alive connection reuse, owned explicitly
# (mirrors client.ApiClientOwner): a server lifespan owns one DohClientOwner
# per server and publishes it with ``doh_client_owned``; standalone callers
# build their own.
class DohClientOwner:
    """Owns one persistent DoH HTTP client for one explicit lifetime.

    Explicit owner for the client state that used to live in the module-global
    ``_doh_client`` singleton. The client is created lazily on first use and
    reused until :meth:`aclose`, and two owners in one process never share a
    client, so one owner's shutdown cannot close another owner's connections.
    An existing client may be adopted (``DohClientOwner(client)``) - the owner
    owns it from then on, including closing it.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def get(self) -> httpx.AsyncClient:
        """Return this owner's DoH client, creating it on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=get_http_timeout())
        return self._client

    async def aclose(self) -> None:
        """Close this owner's DoH client if one exists and reset it.

        The next :meth:`get` constructs a fresh client, picking up any updated
        configuration (such as ``NEXTDNS_HTTP_TIMEOUT``).
        """
        if self._client is not None:
            client = self._client
            self._client = None
            await client.aclose()


# The owner whose DoH client the current context uses; published by a server
# lifespan or by standalone callers for same-task work, and resolved from the
# FastMCP request's lifespan context for served requests (see
# nextdns_mcp.state). No module-level default: an absent owner fails loudly
# instead of silently sharing one process-global client between independent
# servers.
doh_client_owner: ContextVar[DohClientOwner | None] = ContextVar("nextdns_doh_client_owner", default=None)


def current_doh_client_owner() -> DohClientOwner:
    """Return the owner of the DoH client state for the current context.

    Resolution order: an owner published with :func:`doh_client_owned`, then
    the resources of the FastMCP server serving this request.

    Raises:
        StateOwnershipError: When no owner is active. Code reached outside a
            server lifespan must publish one with :func:`doh_client_owned`.
    """
    owner = doh_client_owner.get()
    if owner is None:
        resources = request_resources()
        if resources is not None:
            owner = resources.doh_client
    if owner is None:
        raise StateOwnershipError(
            "No DoH client owner is active in this context. Server code runs inside a server "
            "lifespan, which publishes one; standalone callers must wrap the work in "
            "`with doh_module.doh_client_owned(doh_module.DohClientOwner()):`."
        )
    return owner


@contextmanager
def doh_client_owned(owner: DohClientOwner) -> Iterator[DohClientOwner]:
    """Publish ``owner`` as the current context's DoH client owner for the block."""
    token = doh_client_owner.set(owner)
    try:
        yield owner
    finally:
        doh_client_owner.reset(token)


async def close_doh_client() -> None:
    """Close the current owner's DoH client if one exists and reset it.

    Subsequent calls to the owner's ``get`` will construct a fresh client,
    picking up any updated configuration (such as ``NEXTDNS_HTTP_TIMEOUT``).
    """
    await current_doh_client_owner().aclose()


def _validate_record_type(record_type: str) -> tuple[bool, str]:
    """Validate DNS record type.

    Returns:
        Tuple of (is_valid, record_type_upper)
    """
    record_type_upper = record_type.upper()
    is_valid = record_type_upper in VALID_DNS_RECORD_TYPES
    return is_valid, record_type_upper


def _build_doh_metadata(
    profile_id: str, domain: str, record_type: str, doh_url: str, status: int | None
) -> dict[str, Any]:
    """Build metadata for DoH response."""
    metadata: dict[str, Any] = {
        "profile_id": profile_id,
        "query_domain": domain,
        "query_type": record_type,
        "doh_endpoint": f"{doh_url}?name={domain}&type={record_type}",
    }

    if status is not None:
        status_desc = DNS_STATUS_CODES.get(status, f"Unknown status code: {status}")
        metadata["status_description"] = status_desc

    return metadata


def _read_denied(target_profile: str) -> dict[str, Any]:
    """Build the read-access-denied payload for a profile.

    Goes through the transport's shared denial builder, so a DoH-gate denial
    has the same shape (typed code, reason, profile id, nominal 403) as a
    denial from the ACL layer - the caller branches on one contract, not two.
    """
    logger.warning(f"Read access denied for profile: {target_profile}")
    return access_denied_payload(
        ErrorCode.READ_ACCESS_DENIED,
        f"Read access denied for profile: {target_profile}",
        target_profile,
    )


async def doh_lookup(
    doh_url: str, domain: str, record_type: str, target_profile: str, access: ProfileAccessControl
) -> dict[str, Any]:
    """Execute DoH query and return result with metadata.

    The read gate is enforced here, on the function that actually sends the
    query, against the caller's request snapshot. The decision to query
    upstream and the query itself are therefore always authorized by the same
    environment values, even if the environment changes while the request is
    in flight (issue #179).
    """
    if not access.can_read(target_profile):
        return _read_denied(target_profile)

    params = {"name": domain, "type": record_type}
    headers = {"accept": "application/dns-json"}

    client = current_doh_client_owner().get()
    try:
        response = await client.get(doh_url, params=params, headers=headers)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        metadata = _build_doh_metadata(target_profile, domain, record_type, doh_url, result.get("Status"))
        if result.get("Status") is not None:
            logger.debug(f"DoH lookup result: {domain} -> {metadata['status_description']}")
        return {"data": result, "_metadata": metadata}
    except httpx.HTTPError as e:
        # The domain names what was being looked up; it stays at DEBUG. The
        # payload returned to the caller still carries it for diagnosis.
        logger.error(f"HTTP error during DoH lookup: {_log_safe_error(e)}")
        logger.debug("HTTP error during DoH lookup for %s", domain)
        payload = http_error_payload(f"HTTP error during DoH lookup: {e!s}", e, fallback_code=ErrorCode.HTTP_ERROR)
        payload.update(profile_id=target_profile, domain=domain, type=record_type)
        return payload
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error during DoH lookup: {_log_safe_error(e)}")
        logger.debug("Unexpected error during DoH lookup for %s", domain)
        return error_payload(
            ErrorCode.INTERNAL_ERROR,
            f"Unexpected error during DoH lookup: {e!s}",
            profile_id=target_profile,
            domain=domain,
            type=record_type,
        )


async def _dohLookup_impl(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> dict[str, Any]:
    """Implementation of DoH lookup functionality.

    See dohLookup() for full documentation.
    """
    resolved = resolve_default_profile(profile_id)
    if resolved[1] is not None:
        return resolved[1]
    target_profile = resolved[0]

    # One snapshot for the whole lookup: this decision, the send-time gate in
    # doh_lookup() and the query they authorize all use these values.
    access = load_profile_access_control()
    if not access.can_read(target_profile):
        return _read_denied(target_profile)

    is_valid, record_type_upper = _validate_record_type(record_type)
    if not is_valid:
        logger.warning(f"Invalid DNS record type requested: {record_type}")
        return error_payload(
            ErrorCode.INVALID_RECORD_TYPE,
            f"Invalid record type: {record_type}",
            valid_types=VALID_DNS_RECORD_TYPES,
        )

    doh_url = f"https://dns.nextdns.io/{target_profile}/dns-query"
    # The queried domain is what the caller is asking *about* - query names are
    # browsing-shaped sensitive data - so it belongs at DEBUG, while the
    # routine "a lookup ran" fact stays at INFO without it.
    logger.info("DoH lookup (%s) via profile %s", record_type_upper, target_profile)
    logger.debug("DoH lookup: %s (%s) via profile %s", domain, record_type_upper, target_profile)
    return await doh_lookup(doh_url, domain, record_type_upper, target_profile, access)


async def dohLookup(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> dict[str, Any]:
    """Perform a DNS-over-HTTPS lookup using a NextDNS profile.

    Args:
        domain: The domain name to look up (e.g., "adwords.google.com")
        profile_id: NextDNS profile ID. If not provided, uses NEXTDNS_DEFAULT_PROFILE.
        record_type: DNS record type to query (default "A").

    Returns:
        dict: Wrapper with the raw DNS response under ``data`` and lookup
        context under ``_metadata``, so the metadata can never collide with
        a DNS answer whose name is literally ``_metadata``.
    """
    return await _dohLookup_impl(domain=domain, profile_id=profile_id, record_type=record_type)
