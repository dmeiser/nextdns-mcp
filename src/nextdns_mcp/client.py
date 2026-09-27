"""HTTP client with profile access control for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import posixpath
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .config import (
    NEXTDNS_BASE_URL,
    ConfigurationError,
    ProfileAccessControl,
    get_api_key,
    get_http_timeout,
    load_profile_access_control,
)
from .errors import ErrorCode

logger = logging.getLogger(__name__)

# Safe identifier patterns to prevent path traversal and ACL bypass.
# Profile IDs must match the upstream spec exactly (nextdns-openapi.yaml ProfileId
# parameter): 6 lowercase alphanumeric characters. Anything else 404s upstream.
SAFE_PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9]{6}$")

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


def allowed_destination_hosts() -> frozenset[str]:
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
    URL, so they carry the very query string :func:`_redacted` strips from the
    request argument. Only the status is logged; the verbatim text stays in the
    error payload returned to the caller that supplied the query.
    """
    if isinstance(error, httpx.HTTPStatusError):
        return f"{error.response.status_code} {error.response.reason_phrase}"
    return str(error)


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


def is_write_operation(method: str) -> bool:
    """Check if an HTTP method is a write operation.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE)

    Returns:
        True if it's a write operation, False otherwise
    """
    return method.upper() in ("POST", "PUT", "PATCH", "DELETE")


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


def _is_unclassifiable_profiles_path(path: str | None) -> bool:
    """Return True when a /profiles/... path names no safe, spec-shaped profile id.

    Classification is case-insensitive, so ``/PROFILES/def456/settings`` is
    treated exactly like ``/profiles/def456/settings``. Case-sensitive matching
    left the case variant with no extracted profile id *and* no unclassifiable
    verdict, so it fell through to the collection checks, which enforce only
    the global any_readable/any_writable gates and never per-profile
    membership, silently bypassing the profile ACL (issue #285).

    Called from the single ``_authorize()`` decision point that both
    ``request()`` and ``stream()`` go through, so the two entry points cannot
    diverge again.
    """
    if path is None:
        return False
    return _PROFILE_ROOT.search(path.rstrip("/")) is not None


class AccessControlledClient(httpx.AsyncClient):
    """HTTP client wrapper that enforces profile access control."""

    def _check_write_access(self, profile_id: str, method: str, url: str, access: ProfileAccessControl) -> None:
        """Check write access against the request snapshot; raise AccessDeniedError if denied."""
        if access.can_write(profile_id):
            return

        if access.read_only:
            error_msg = "Write operation denied: server is in read-only mode"
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

    async def raw_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make an HTTP request WITHOUT any profile access-control check.

        This is the raw transport path: it goes straight to ``httpx`` and is
        used by server-level probes that must observe the true upstream state.
        The ``/health`` readiness check uses it because a local ACL denial (for
        example a deny-all readable-profile configuration) would otherwise be
        indistinguishable from a real NextDNS authentication failure.

        The destination allow-list is still enforced (issue #262): this path
        carries the X-Api-Key client header just like every other one, so the
        key must never leave for a non-NextDNS host. Only the profile ACL is
        skipped.

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
        self._check_destination(method, url)
        return await super().request(method, url, **kwargs)

    def _destination_host(self, url: Any) -> str:
        """Return the host a request would actually be sent to.

        Relative URLs resolve against this client's ``base_url``, exactly as
        httpx merges them, so a client built on a non-NextDNS base is caught
        here too. A relative URL on a client with no base URL yields "".
        """
        parsed = httpx.URL(str(url))
        if parsed.is_absolute_url or parsed.scheme or parsed.host:
            return _normalized_host(parsed.host)
        return _normalized_host(httpx.URL(str(self.base_url)).host)

    def _check_destination(self, method: str, url: Any) -> None:
        """Refuse a request whose destination host is not an approved NextDNS host.

        Runs before the request is handed to httpx, so nothing is sent and the
        X-Api-Key header never leaves the process. The message carries the host
        only - never the URL (which may hold query-string PII) and never any
        credential.
        """
        host = self._destination_host(url)
        if not host:
            # No host resolves: the request is relative and this client has no
            # base URL, so httpx itself rejects it before any I/O. There is no
            # destination to allow or to protect, and no key to leak.
            return
        if host in allowed_destination_hosts():
            return

        error_msg = f"Blocked request to non-NextDNS host: {host}"
        logger.warning(f"{error_msg} (method={method})")
        raise AccessDeniedError(error_msg, code=ErrorCode.ACCESS_DENIED)

    def _authorize(self, method: str, url: Any) -> None:
        """Apply the destination allow-list and the profile ACL to one request.

        Shared by request() and stream() so the two entry points cannot drift:
        no request leaves the process without both an approved destination host
        and an access-control decision.
        """
        self._check_destination(method, url)

        url_str = str(url)
        request_path = _normalized_request_path(url_str)
        is_absolute_url = request_path is None
        contains_traversal = request_path is not None and ".." in request_path
        # /profiles paths that name something after the prefix but do not carry a
        # safe, extractable profile id (e.g. /profiles/abc.def/settings).
        unclassifiable_profiles_path = _is_unclassifiable_profiles_path(request_path)
        profile_id = _extract_profile_id_from_path(request_path)

        # One snapshot per request: every check below, and the decision to send
        # the request upstream at all, is made against the same env values even
        # if the environment changes while this call is in flight (issue #179).
        access = load_profile_access_control()

        if profile_id:
            self._check_access(profile_id, method, url_str, access)
        elif is_absolute_url or contains_traversal or unclassifiable_profiles_path:
            # Fail closed: absolute/authority-bearing URLs, traversal payloads, and
            # unclassifiable /profiles paths cannot be matched against the profile
            # ACL, so deny them instead of letting them bypass the check entirely.
            logger.warning(f"Forbidden URL: {_redacted(url_str)} (method={method})")
            raise AccessDeniedError(
                f"Forbidden URL: {_redacted(url_str)}", code=ErrorCode.ACCESS_DENIED, profile_id=profile_id or ""
            )
        else:
            self._check_collection_access(method, url_str, access)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
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
        return await super().request(method, url, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: Any, **kwargs: Any) -> AsyncIterator[httpx.Response]:  # type: ignore[override]
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

        async with super().stream(method, url, **kwargs) as response:
            yield response


def create_nextdns_client() -> AccessControlledClient:
    """Create an authenticated HTTP client for NextDNS API with access control.

    Returns:
        AccessControlledClient: Configured async HTTP client with authentication
            and access control

    Raises:
        ConfigurationError: If the API key is absent or empty, or
            NEXTDNS_HTTP_TIMEOUT is not a positive number of seconds.
    """
    key = get_api_key()
    if not key or not key.strip():
        raise ConfigurationError(
            "NEXTDNS_API_KEY is required. Set the NEXTDNS_API_KEY environment "
            "variable or NEXTDNS_API_KEY_FILE pointing to a Docker secret."
        )

    headers = {
        "X-Api-Key": key.strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    return AccessControlledClient(
        base_url=NEXTDNS_BASE_URL,
        headers=headers,
        timeout=get_http_timeout(),
        follow_redirects=False,
    )


_client: AccessControlledClient | None = None


def get_api_client() -> AccessControlledClient:
    """Return the shared authenticated API client, creating it on first use.

    The client is built lazily so that importing this module has no side
    effects and configuration changes (e.g. in tests) are picked up.
    """
    global _client
    if _client is None:
        _client = create_nextdns_client()
    return _client


def __getattr__(name: str) -> Any:
    """Lazily expose the ``api_client`` singleton for backward compatibility."""
    if name == "api_client":
        return get_api_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
