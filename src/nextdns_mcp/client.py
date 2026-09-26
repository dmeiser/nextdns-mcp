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
    can_read_profile,
    can_write_profile,
    get_api_key,
    get_http_timeout,
    get_readable_profiles_set,
    get_writable_profiles_set,
    is_read_only,
)
from .errors import ErrorCode

logger = logging.getLogger(__name__)

# Safe identifier patterns to prevent path traversal and ACL bypass.
# Profile IDs must match the upstream spec exactly (nextdns-openapi.yaml ProfileId
# parameter): 6 lowercase alphanumeric characters. Anything else 404s upstream.
SAFE_PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9]{6}$")


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
    match = re.match(r"^/profiles/([^/]+)(?:/|$)", normalized)
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


class AccessControlledClient(httpx.AsyncClient):
    """HTTP client wrapper that enforces profile access control."""

    def _check_write_access(self, profile_id: str, method: str, url: str) -> None:
        """Check write access; raise AccessDeniedError if denied."""
        if can_write_profile(profile_id):
            return

        if is_read_only():
            error_msg = "Write operation denied: server is in read-only mode"
        else:
            error_msg = f"Write access denied for profile: {profile_id}"

        logger.warning(f"{error_msg} (method={method}, url={str(url).split('?', 1)[0]})")
        raise AccessDeniedError(error_msg, code=ErrorCode.WRITE_ACCESS_DENIED, profile_id=profile_id)

    def _check_read_access(self, profile_id: str, method: str, url: str) -> None:
        """Check read access; raise AccessDeniedError if denied."""
        if can_read_profile(profile_id):
            return

        error_msg = f"Read access denied for profile: {profile_id}"
        logger.warning(f"{error_msg} (method={method}, url={str(url).split('?', 1)[0]})")
        raise AccessDeniedError(error_msg, code=ErrorCode.READ_ACCESS_DENIED, profile_id=profile_id)

    def _check_collection_write_access(self, method: str, url: str) -> None:
        """Enforce global write denials for collection endpoints (no profile_id in URL).

        Collection endpoints such as POST /profiles create resources that belong to a
        profile, so they must respect read-only mode and the writable-profile set even
        though the URL carries no profile_id.
        """
        if is_read_only():
            error_msg = "Write operation denied: server is in read-only mode"
            logger.warning(f"{error_msg} (method={method}, url={url})")
            raise AccessDeniedError(error_msg, code=ErrorCode.WRITE_ACCESS_DENIED)

        if get_writable_profiles_set() is None:
            error_msg = "Write access denied: no profiles are writable"
            logger.warning(f"{error_msg} (method={method}, url={url})")
            raise AccessDeniedError(error_msg, code=ErrorCode.WRITE_ACCESS_DENIED)

    def _check_collection_read_access(self, method: str, url: str) -> None:
        """Enforce global read denials for collection endpoints (no profile_id in URL).

        Collection endpoints such as GET /profiles list profile-scoped resources, so
        they must respect the readable-profile deny-all default even though the URL
        carries no profile_id.
        """
        if get_readable_profiles_set() is None:
            error_msg = "Read access denied: no profiles are readable"
            logger.warning(f"{error_msg} (method={method}, url={url})")
            raise AccessDeniedError(error_msg, code=ErrorCode.READ_ACCESS_DENIED)

    def _check_access(self, profile_id: str, method: str, url: str) -> None:
        """Check access control for profile operations; raise AccessDeniedError if denied."""
        if is_write_operation(method):
            self._check_write_access(profile_id, method, url)
        else:
            self._check_read_access(profile_id, method, url)

    def _check_collection_access(self, method: str, url: str) -> None:
        """Check access control for collection operations; raise AccessDeniedError if denied."""
        if is_write_operation(method):
            self._check_collection_write_access(method, url)
        else:
            self._check_collection_read_access(method, url)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        """Make an HTTP request with access control checks.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Raises:
            AccessDeniedError: If the request is denied by the profile access
                control layer (read/write ACL, read-only mode, or fail-closed
                URL classification). No network request is made.

        Returns:
            Response from the API
        """
        # Query strings can carry sensitive data (search terms, device IDs, cursor
        # tokens). Log only the path at INFO; log the full URL at DEBUG. (issue #139)
        logged_path = str(url).split("?", 1)[0]
        logger.info(f"HTTP Request: {method} {logged_path}")
        logger.debug(f"HTTP Request: {method} {url}")

        request_path = _normalized_request_path(str(url))
        is_absolute_url = request_path is None
        contains_traversal = request_path is not None and ".." in request_path
        # /profiles paths that name something after the prefix but do not carry a
        # safe, extractable profile id (e.g. /profiles/abc.def/settings).
        unclassifiable_profiles_path = request_path is not None and request_path.rstrip("/").startswith("/profiles/")
        profile_id = _extract_profile_id_from_path(request_path)

        if profile_id:
            self._check_access(profile_id, method, url)
        elif is_absolute_url or contains_traversal or unclassifiable_profiles_path:
            # Fail closed: absolute/authority-bearing URLs, traversal payloads, and
            # unclassifiable /profiles paths cannot be matched against the profile
            # ACL, so deny them instead of letting them bypass the check entirely.
            error_msg = f"Forbidden URL: {url!s}"
            logger.warning(f"Forbidden URL: {logged_path} (method={method})")
            raise AccessDeniedError(error_msg, code=ErrorCode.ACCESS_DENIED, profile_id=profile_id or "")
        else:
            self._check_collection_access(method, url)

        # No body coercion here: string values in JSON bodies are passed through
        # unchanged. Schema-aware coercion of tool arguments already happens in
        # StripExtraFieldsMiddleware, so blindly coercing body values would corrupt
        # string fields such as profile names ("12345") or passwords ("0012").
        return await super().request(method, url, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: Any, **kwargs: Any) -> AsyncIterator[httpx.Response]:  # type: ignore[override]
        """Stream a response with access control checks.

        httpx's ``stream()`` does not call ``request()``, so the ACL guard is
        applied here as well. When access is denied, AccessDeniedError is raised
        (propagating on ``__aenter__``) without any network request, exactly as
        ``request()`` does.
        """
        logger.info(f"HTTP Stream: {method} {url}")

        profile_id = extract_profile_id_from_url(str(url))
        if profile_id:
            self._check_access(profile_id, method, str(url))

        async with super().stream(method, url, **kwargs) as response:
            yield response


def create_nextdns_client() -> httpx.AsyncClient:
    """Create an authenticated HTTP client for NextDNS API with access control.

    Returns:
        httpx.AsyncClient: Configured async HTTP client with authentication and access control

    Raises:
        ConfigurationError: If the API key is absent or empty.
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


_client: httpx.AsyncClient | None = None


def get_api_client() -> httpx.AsyncClient:
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
