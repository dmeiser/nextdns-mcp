"""HTTP client with profile access control for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import posixpath
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Self

import httpx

from .config import (
    NEXTDNS_BASE_URL,
    ConfigurationError,
    ProfileAccessControl,
    get_api_key,
    get_http_timeout,
    load_profile_access_control,
)
from .constants import SAFE_PROFILE_ID_PATTERN, is_write_operation
from .errors import ErrorCode, StateOwnershipError
from .state import request_resources

logger = logging.getLogger(__name__)

# Hosts this client is allowed to send a request to. Every request carries the
# X-Api-Key client header, so without a destination allow-list the account's API
# key follows the request to whatever host the URL names (issue #262). These are
# the two NextDNS hosts the code actually calls: the REST API base and the DoH
# endpoint built by tools/doh.py. The host of NEXTDNS_BASE_URL is added as well
# so pointing that existing constant at a self-hosted/proxy base keeps working
# without inventing a second configuration mechanism.
ALLOWED_DESTINATION_HOSTS = frozenset({"api.nextdns.io", "dns.nextdns.io"})

# Loopback destinations are allowed so local test doubles and loopback mock
# servers stay usable; no shipped tool ever targets them.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _normalized_host(host: str | None) -> str:
    """Return a comparable form of a host: lowercased, without a trailing dot."""
    return (host or "").strip().rstrip(".").lower()


def _allowed_destination_hosts() -> frozenset[str]:
    """Return every host this client may send a request to.

    See :data:`ALLOWED_DESTINATION_HOSTS` for the NextDNS endpoints and
    :data:`LOOPBACK_HOSTS` for the local test-double allowance.
    """
    hosts = set(ALLOWED_DESTINATION_HOSTS) | set(LOOPBACK_HOSTS)
    hosts.add(_normalized_host(httpx.URL(NEXTDNS_BASE_URL).host))
    return frozenset(hosts)


def _redacted(url: str) -> str:
    """Return the URL with any query string removed (issue #139).

    Query strings can carry sensitive data (DNS search terms, device IDs, cursor
    tokens), so every log site that runs at INFO or above must use this helper
    instead of the raw URL. Use the full URL only at DEBUG.
    """
    return str(url).split("?", 1)[0]


def _log_safe_error(error: Exception) -> str:
    """Return exception text that is safe to log at INFO or above (issue #139).

    httpx builds ``HTTPStatusError`` messages from the fully merged request
    URL, so only the status is logged for those. Every other message keeps its
    wording but goes through :func:`_redacted`, because an exception raised by
    the ACL layer names the URL the caller asked for verbatim. The exception
    itself is never rewritten: the caller still receives the full text.
    """
    if isinstance(error, httpx.HTTPStatusError):
        return f"{error.response.status_code} {error.response.reason_phrase}"
    return _redacted(str(error))


# Match the first path segment case-insensitively; anything under a
# /profiles segment that does not yield a safe id is unclassifiable (issue #285).
_PROFILE_PREFIX = re.compile(r"^/profiles/([^/]+)(?:/|$)", re.IGNORECASE)
# The bare collection root ("/profiles", with or without a trailing slash) names
# no profile and keeps its collection-endpoint handling, so only paths with a
# segment after the prefix are unclassifiable.
_PROFILE_ROOT = re.compile(r"^/profiles/", re.IGNORECASE)


def _normalized_request_path(url: str) -> str | None:
    """Return the request path with a guaranteed leading slash.

    Relative paths are resolved the same way httpx joins them against the API
    base URL: a path without a leading "/" is treated as rooted at the base
    (e.g. "profiles/abc123" -> "/profiles/abc123"). Returns None for absolute
    or authority-bearing URLs (e.g. "https://host/..." or "//host/..."), which
    target a different host and must not be routed through the profile ACL.
    """
    parsed = httpx.URL(str(url))
    if parsed.is_absolute_url or parsed.scheme or parsed.host:
        return None
    path = parsed.path
    if not path.startswith("/"):
        path = "/" + path
    return path


def _extract_profile_id_from_path(path: str | None) -> str | None:
    """Extract a safe profile_id from a normalized request path."""
    if path is None:
        return None

    # Reject any path containing parent-directory references before matching.
    # httpx does not normalize ".." segments, so /profiles/allowed123/../../profiles/denied456
    # would otherwise be normalized downstream into the denied profile while the
    # ACL check is skipped for the "safe" id we extracted here.
    if ".." in path:
        return None

    # Normalize the path so that equivalent paths are treated consistently.
    normalized = posixpath.normpath(path)
    # Match /profiles/{profile_id}/... pattern
    match = _PROFILE_PREFIX.match(normalized)
    if match:
        profile_id = match.group(1)
        if SAFE_PROFILE_ID_PATTERN.match(profile_id):
            return profile_id
    return None


def extract_profile_id_from_url(url: str) -> str | None:
    """Extract profile_id from a URL path.

    Args:
        url: The URL path (e.g., "/profiles/abc123/settings")

    Returns:
        The profile_id if found and safe, None otherwise. Traversal payloads
        and absolute URLs are rejected (returns None) so callers can fail
        closed instead of silently skipping the access check.
    """
    return _extract_profile_id_from_path(_normalized_request_path(url))


class AccessDeniedError(Exception):
    """Raised when the profile access-control layer denies a request (issue #178).

    Carries the ACL denial reason as its message plus the typed error ``code``
    (``read_access_denied`` / ``write_access_denied`` / ``access_denied``) and
    the denied ``profile_id`` ("" for collection endpoints). This replaces the
    synthetic 403 ``httpx.Response`` the ACL used to return, which had no
    transport or stream and made an ACL denial indistinguishable from a real
    upstream 403.
    """

    def __init__(self, message: str, *, code: str = ErrorCode.ACCESS_DENIED, profile_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.profile_id = profile_id
        self.message = message


def _is_unclassifiable_profiles_path(path: str) -> bool:
    """Return True when a /profiles/... path names no safe, spec-shaped profile id.

    Classification is case-insensitive, so ``/PROFILES/def456/settings`` is
    treated exactly like ``/profiles/def456/settings``. Case-sensitive matching
    left the case variant with no extracted profile id *and* no unclassifiable
    verdict, so it fell through to the collection checks, which enforce only
    the global any_readable/any_writable gates and never per-profile
    membership, silently bypassing the profile ACL (issue #285).

    Called from ``_unmatchable_profile_url``, itself part of the single
    ``_authorize()`` decision point that both ``request()`` and ``stream()``
    go through, so the two entry points cannot diverge again. A missing path is
    not classified here: ``_unmatchable_profile_url`` denies it before this is
    reached, so ``path`` is always a real normalized path.
    """
    return _PROFILE_ROOT.search(path.rstrip("/")) is not None


def _unmatchable_profile_url(request_path: str | None) -> bool:
    """True when a request cannot be classified against the profile ACL.

    An absolute/authority-bearing URL (no normalized path), a traversal
    payload, or a /profiles path that names something after the prefix but
    carries no safe, extractable profile id (e.g. /profiles/abc.def/settings)
    cannot be matched to a profile id, so _authorize denies it outright.
    """
    if request_path is None:
        return True
    if ".." in request_path:
        return True
    return _is_unclassifiable_profiles_path(request_path)


def _destination_host(base_url: Any, url: Any) -> str:
    """Return the host a request would actually be sent to.

    Relative URLs resolve against the client's ``base_url``, exactly as
    httpx merges them, so a client built on a non-NextDNS base is caught
    here too. A relative URL on a client with no base URL yields "".
    """
    parsed = httpx.URL(str(url))
    if parsed.is_absolute_url or parsed.scheme or parsed.host:
        return _normalized_host(parsed.host)
    return _normalized_host(httpx.URL(str(base_url)).host)


def _check_destination(base_url: Any, method: str, url: Any) -> None:
    """Refuse a request whose destination host is not an approved NextDNS host.

    Runs before the request is handed to httpx, so nothing is sent and the
    X-Api-Key header never leaves the process. The message carries the host
    only - never the URL (which may hold query-string PII) and never any
    credential.
    """
    host = _destination_host(base_url, url)
    if not host:
        # No host resolves: the request is relative and this client has no
        # base URL, so httpx itself rejects it before any I/O. There is no
        # destination to allow or to protect, and no key to leak.
        return
    if host in _allowed_destination_hosts():
        return

    error_msg = f"Blocked request to non-NextDNS host: {host}"
    logger.warning(f"{error_msg} (method={method})")
    raise AccessDeniedError(error_msg, code=ErrorCode.ACCESS_DENIED)


class AccessControlledClient:
    """HTTP client that enforces profile access control (issue #639).

    Composition wrapper around a private ``httpx.AsyncClient`` — the ACL
    client used to subclass the concrete httpx class, which leaked the
    base's whole public transport surface (``send``, ``build_request``, ...)
    past the ACL door. Here the wrapped client is private, and the only ways
    to move a request through this class are :meth:`request` and
    :meth:`stream` plus the method conveniences built on ``request()``;
    every one of them runs the single :meth:`_authorize` decision, so the
    ACL is the one doorway rather than an inheritance-narrowed one.
    """

    def __init__(
        self,
        *,
        base_url: str | httpx.URL | None = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        transport: Any = None,
        follow_redirects: bool = False,
    ) -> None:
        kwargs: dict[str, Any] = {"headers": headers, "follow_redirects": follow_redirects}
        if base_url is not None:
            kwargs["base_url"] = base_url
        if timeout is not None:
            kwargs["timeout"] = timeout
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        """Close the wrapped httpx client."""
        await self._client.aclose()

    @property
    def base_url(self) -> httpx.URL:
        """The base URL the wrapped client resolves relative requests against."""
        return self._client.base_url

    @property
    def headers(self) -> httpx.Headers:
        """The client-level default headers, including the X-Api-Key credential."""
        return self._client.headers

    @property
    def timeout(self) -> httpx.Timeout:
        """The configured request timeout."""
        return self._client.timeout

    @property
    def follow_redirects(self) -> bool:
        """Whether the wrapped client follows redirects (always False here)."""
        return self._client.follow_redirects

    @property
    def is_closed(self) -> bool:
        """Whether the wrapped client has been closed."""
        return self._client.is_closed

    def _check_write_access(self, profile_id: str, method: str, url: str, access: ProfileAccessControl) -> None:
        """Check write access against the request snapshot; raise AccessDeniedError if denied."""
        if access.can_write(profile_id):
            return

        if access.read_only:
            error_msg = "Write operation denied: server is in read-only mode"
        elif profile_id.lower() in access.protected:
            error_msg = f"Write access denied: profile {profile_id} is protected (NEXTDNS_PROTECTED_PROFILES)"
        else:
            error_msg = f"Write access denied for profile: {profile_id}"

        logger.warning(f"{error_msg} (method={method}, url={_redacted(url)})")
        raise AccessDeniedError(error_msg, code=ErrorCode.WRITE_ACCESS_DENIED, profile_id=profile_id)

    def _check_read_access(self, profile_id: str, method: str, url: str, access: ProfileAccessControl) -> None:
        """Check read access against the request snapshot; raise AccessDeniedError if denied."""
        if access.can_read(profile_id):
            return

        error_msg = f"Read access denied for profile: {profile_id}"
        logger.warning(f"{error_msg} (method={method}, url={_redacted(url)})")
        raise AccessDeniedError(error_msg, code=ErrorCode.READ_ACCESS_DENIED, profile_id=profile_id)

    def _check_collection_write_access(self, method: str, url: str, access: ProfileAccessControl) -> None:
        """Enforce global write denials for collection endpoints (no profile_id in URL).

        Collection endpoints such as POST /profiles create resources that belong to a
        profile, so they must respect read-only mode and the writable-profile set even
        though the URL carries no profile_id.
        """
        if access.read_only:
            error_msg = "Write operation denied: server is in read-only mode"
            logger.warning(f"{error_msg} (method={method}, url={_redacted(url)})")
            raise AccessDeniedError(error_msg, code=ErrorCode.WRITE_ACCESS_DENIED)

        if not access.any_writable:
            error_msg = "Write access denied: no profiles are writable"
            logger.warning(f"{error_msg} (method={method}, url={_redacted(url)})")
            raise AccessDeniedError(error_msg, code=ErrorCode.WRITE_ACCESS_DENIED)

    def _check_collection_read_access(self, method: str, url: str, access: ProfileAccessControl) -> None:
        """Enforce global read denials for collection endpoints (no profile_id in URL).

        Collection endpoints such as GET /profiles list profile-scoped resources, so
        they must respect the readable-profile deny-all default even though the URL
        carries no profile_id.
        """
        if not access.any_readable:
            error_msg = "Read access denied: no profiles are readable"
            logger.warning(f"{error_msg} (method={method}, url={_redacted(url)})")
            raise AccessDeniedError(error_msg, code=ErrorCode.READ_ACCESS_DENIED)

    def _check_access(self, profile_id: str, method: str, url: str, access: ProfileAccessControl) -> None:
        """Check access control for profile operations; raise AccessDeniedError if denied."""
        if is_write_operation(method):
            self._check_write_access(profile_id, method, url, access)
        else:
            self._check_read_access(profile_id, method, url, access)

    def _check_collection_access(self, method: str, url: str, access: ProfileAccessControl) -> None:
        """Check access control for collection operations; raise AccessDeniedError if denied."""
        if is_write_operation(method):
            self._check_collection_write_access(method, url, access)
        else:
            self._check_collection_read_access(method, url, access)

    def _authorize(self, method: str, url: Any) -> None:
        """Apply the destination allow-list and the profile ACL to one request.

        Shared by request() and stream() so the two entry points cannot drift:
        no request leaves the process without both an approved destination host
        and an access-control decision.
        """
        _check_destination(self._client.base_url, method, url)

        url_str = str(url)
        request_path = _normalized_request_path(url_str)
        profile_id = _extract_profile_id_from_path(request_path)

        # One snapshot per request: every check below, and the decision to send
        # the request upstream at all, is made against the same env values even
        # if the environment changes while this call is in flight (issue #179).
        access = load_profile_access_control()

        if profile_id:
            self._check_access(profile_id, method, url_str, access)
        elif _unmatchable_profile_url(request_path):
            # Fail closed: a URL whose profile cannot be classified is denied
            # instead of being allowed to bypass the profile ACL check.
            logger.warning(f"Forbidden URL: {_redacted(url_str)} (method={method})")
            raise AccessDeniedError(f"Forbidden URL: {url_str}", code=ErrorCode.ACCESS_DENIED, profile_id="")
        else:
            self._check_collection_access(method, url_str, access)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make an HTTP request with access control checks.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Raises:
            AccessDeniedError: If the destination host is not an approved
                NextDNS host, or if the request is denied by the profile access
                control layer (read/write ACL, read-only mode, or fail-closed
                URL classification). No network request is made.

        Returns:
            Response from the API
        """
        # Query strings can carry sensitive data (search terms, device IDs, cursor
        # tokens). Log only the path at INFO; log the full URL at DEBUG. (issue #139)
        logged_path = _redacted(url)
        logger.info(f"HTTP Request: {method} {logged_path}")
        logger.debug(f"HTTP Request: {method} {url}")

        self._authorize(method, url)

        # No body coercion here: string values in JSON bodies are passed through
        # unchanged. Schema-aware coercion of tool arguments already happens in
        # StripExtraFieldsMiddleware, so blindly coercing body values would corrupt
        # string fields such as profile names ("12345") or passwords ("0012").
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make a GET request with access control checks (httpx convenience)."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make a POST request with access control checks (httpx convenience)."""
        return await self.request("POST", url, **kwargs)

    async def put(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make a PUT request with access control checks (httpx convenience)."""
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make a PATCH request with access control checks (httpx convenience)."""
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make a DELETE request with access control checks (httpx convenience)."""
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make a HEAD request with access control checks (httpx convenience)."""
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: Any, **kwargs: Any) -> httpx.Response:
        """Make an OPTIONS request with access control checks (httpx convenience)."""
        return await self.request("OPTIONS", url, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: Any, **kwargs: Any) -> AsyncIterator[httpx.Response]:
        """Stream a response with the destination allow-list and access control checks.

        httpx's ``stream()`` does not call ``request()``, so the guards are
        applied here as well. When the destination host is not approved or
        access is denied, AccessDeniedError is raised (propagating on
        ``__aenter__``) without any network request, exactly as ``request()``
        does (issue #262).
        """
        # Query strings can carry sensitive data (search terms, device IDs, cursor
        # tokens). Log only the path. (issue #249)
        logged_path = _redacted(url)
        logger.info(f"HTTP Stream: {method} {logged_path}")

        # _authorize() applies the destination allow-list and the profile ACL
        # (including the case-insensitive fail-closed /profiles classification
        # from _is_unclassifiable_profiles_path), so both entry points share one
        # decision and cannot drift apart again (issues #262, #285).
        self._authorize(method, url)

        async with self._client.stream(method, url, **kwargs) as response:
            yield response


def _authenticated_client_kwargs() -> dict[str, Any]:
    """Validate the configuration and return the shared client constructor kwargs.

    Both the ACL client and the raw probe client are built from these kwargs,
    so they always carry the same credentials, base URL, and timeout.
    """
    key = get_api_key()
    if not key or not key.strip():
        raise ConfigurationError(
            "NEXTDNS_API_KEY is required. Set the NEXTDNS_API_KEY environment "
            "variable or NEXTDNS_API_KEY_FILE pointing to a Docker secret."
        )

    return {
        "base_url": NEXTDNS_BASE_URL,
        "headers": {
            "X-Api-Key": key.strip(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        "timeout": get_http_timeout(),
        "follow_redirects": False,
    }


def create_nextdns_client() -> AccessControlledClient:
    """Create an authenticated HTTP client for NextDNS API with access control.

    Returns:
        AccessControlledClient: Configured async HTTP client with authentication
            and access control

    Raises:
        ConfigurationError: If the API key is absent or empty, or
            NEXTDNS_HTTP_TIMEOUT is not a positive number of seconds.
    """
    return AccessControlledClient(**_authenticated_client_kwargs())


class RawProbeTransport:
    """ACL-free transport for server-level probes (the /health readiness check).

    Composition wrapper around a private ``httpx.AsyncClient``, kept separate
    from :class:`AccessControlledClient` so the profile ACL has no bypass
    doorway on the class that enforces it: a probe here deliberately skips the
    profile ACL because a local ACL denial (for example a deny-all
    readable-profile configuration) would otherwise be indistinguishable from a
    real NextDNS authentication failure.

    The destination allow-list is still enforced on every request (issue #262):
    this path carries the X-Api-Key client header just like every other one, so
    the key must never leave for a non-NextDNS host. Only the profile ACL is
    skipped.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._client = httpx.AsyncClient(**kwargs)

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        """Close the wrapped httpx client."""
        await self._client.aclose()

    @property
    def base_url(self) -> httpx.URL:
        """The base URL the wrapped client resolves relative requests against."""
        return self._client.base_url

    @property
    def headers(self) -> httpx.Headers:
        """The client-level default headers, including the X-Api-Key credential."""
        return self._client.headers

    @property
    def is_closed(self) -> bool:
        """Whether the wrapped client has been closed."""
        return self._client.is_closed

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request with NO profile ACL check; the destination allow-list still applies.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Returns:
            The unmodified response from the API.

        Raises:
            AccessDeniedError: If the destination host is not an approved
                NextDNS host. No network request is made.
        """
        _check_destination(self._client.base_url, method, url)
        return await self._client.request(method, url, **kwargs)


def create_raw_probe_client() -> RawProbeTransport:
    """Create the authenticated, ACL-free transport used by server-level probes.

    Raises:
        ConfigurationError: If the API key is absent or empty, or
            NEXTDNS_HTTP_TIMEOUT is not a positive number of seconds.
    """
    return RawProbeTransport(**_authenticated_client_kwargs())


class ApiClientOwner:
    """Owns one authenticated NextDNS API client for one explicit lifetime.

    Explicit owner for the client state that used to live in the module-global
    ``_client`` singleton: a server lifespan builds one owner per server and
    publishes it with :func:`api_client_owned`; standalone callers (tests,
    scripts) build their own. The client is created lazily on first use and
    reused until :meth:`aclose`, so lazy creation, configuration pickup, and
    keep-alive connection reuse are preserved per owner - and two owners in
    one process never share a client, so one owner's shutdown cannot close
    another owner's connections.
    """

    def __init__(self) -> None:
        self._client: AccessControlledClient | None = None

    def get(self) -> AccessControlledClient:
        """Return this owner's client, creating it on first use.

        The client is built lazily so that importing this module has no side
        effects and configuration changes (e.g. in tests) are picked up.
        """
        if self._client is None:
            self._client = create_nextdns_client()
        return self._client

    async def aclose(self) -> None:
        """Close this owner's client if one exists and reset it.

        The next :meth:`get` constructs a fresh client, picking up any updated
        configuration (such as ``NEXTDNS_HTTP_TIMEOUT``).
        """
        if self._client is not None:
            client = self._client
            self._client = None
            await client.aclose()


# The owner whose client the current context uses. A server lifespan sets this
# to the owner its ServerResources holds (see nextdns_mcp.openapi), as may
# standalone callers (tests, scripts) with ``api_client_owned`` for work that
# runs in the same task. Requests served by FastMCP resolve the owner from the
# request's lifespan context instead (see nextdns_mcp.state): the lifespan
# task does not propagate context variables into request tasks, so this var
# alone cannot serve per-request traffic. There is deliberately NO
# module-level default: an absent owner fails loudly instead of silently
# serving one process-global client to independent servers.
api_client_owner: ContextVar[ApiClientOwner | None] = ContextVar("nextdns_api_client_owner", default=None)


def current_api_client_owner() -> ApiClientOwner:
    """Return the owner of the API client state for the current context.

    Resolution order: an owner published with :func:`api_client_owned`, then
    the resources of the FastMCP server serving this request.

    Raises:
        StateOwnershipError: When no owner is active. Code reached outside a
            server lifespan must publish one with :func:`api_client_owned`.
    """
    owner = api_client_owner.get()
    if owner is None:
        resources = request_resources()
        if resources is not None:
            owner = resources.api_client
    if owner is None:
        raise StateOwnershipError(
            "No API client owner is active in this context. Server code runs inside a server "
            "lifespan, which publishes one; standalone callers must wrap the work in "
            "`with client_module.api_client_owned(client_module.ApiClientOwner()):`."
        )
    return owner


@contextmanager
def api_client_owned(owner: ApiClientOwner) -> Iterator[ApiClientOwner]:
    """Publish ``owner`` as the current context's API client owner for the block."""
    token = api_client_owner.set(owner)
    try:
        yield owner
    finally:
        api_client_owner.reset(token)


_raw_probe_client: RawProbeTransport | None = None


def get_raw_probe_client() -> RawProbeTransport:
    """Return the shared ACL-free probe transport, creating it on first use.

    The /health readiness check uses this instead of :func:`get_api_client` so
    the class that enforces the profile ACL exposes no bypass at all, while the
    probe still observes the true upstream state.
    """
    global _raw_probe_client
    if _raw_probe_client is None:
        _raw_probe_client = create_raw_probe_client()
    return _raw_probe_client


async def close_raw_probe_client() -> None:
    """Close the shared probe transport if one exists and reset the singleton."""
    global _raw_probe_client
    if _raw_probe_client is not None:
        transport = _raw_probe_client
        _raw_probe_client = None
        await transport.aclose()


def get_api_client() -> AccessControlledClient:
    """Return the current owner's shared authenticated API client, creating it on first use.

    The client is built lazily so that importing this module has no side
    effects and configuration changes (e.g. in tests) are picked up.
    """
    return current_api_client_owner().get()


async def close_api_client() -> None:
    """Close the current owner's API client if one exists and reset it.

    Subsequent calls to :func:`get_api_client` will construct a fresh client,
    picking up any updated configuration (such as ``NEXTDNS_HTTP_TIMEOUT``).
    """
    await current_api_client_owner().aclose()


def __getattr__(name: str) -> Any:
    """Lazily expose the ``api_client`` client for backward compatibility."""
    if name == "api_client":
        return get_api_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
