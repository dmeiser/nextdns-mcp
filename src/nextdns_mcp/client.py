"""HTTP client with profile access control for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import posixpath
import re
from typing import Any

import httpx

from .config import (
    NEXTDNS_BASE_URL,
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


def create_access_denied_response(
    method: str, url: str, error_msg: str, profile_id: str, code: str = ErrorCode.ACCESS_DENIED
) -> httpx.Response:
    """Create a 403 Forbidden response for access denied scenarios.

    Args:
        method: HTTP method
        url: Request URL
        error_msg: Error message to include in response
        profile_id: The profile ID that was denied access
        code: Typed error code identifying the denial class (read/write)

    Returns:
        403 Forbidden Response object
    """
    response = httpx.Response(
        status_code=403,
        json={"error": error_msg, "code": code, "profile_id": profile_id},
        request=httpx.Request(method, str(url)),
    )
    return response


class AccessControlledClient(httpx.AsyncClient):
    """HTTP client wrapper that enforces profile access control."""

    def _check_write_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check write access and return error response if denied."""
        if can_write_profile(profile_id):
            return None

        if is_read_only():
            error_msg = "Write operation denied: server is in read-only mode"
        else:
            error_msg = f"Write access denied for profile: {profile_id}"

        logger.warning(f"{error_msg} (method={method}, url={url})")
        return create_access_denied_response(method, url, error_msg, profile_id, code=ErrorCode.WRITE_ACCESS_DENIED)

    def _check_read_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check read access and return error response if denied."""
        if can_read_profile(profile_id):
            return None

        error_msg = f"Read access denied for profile: {profile_id}"
        logger.warning(f"{error_msg} (method={method}, url={url})")
        return create_access_denied_response(method, url, error_msg, profile_id, code=ErrorCode.READ_ACCESS_DENIED)

    def _check_collection_write_access(self, method: str, url: str) -> httpx.Response | None:
        """Enforce global write denials for collection endpoints (no profile_id in URL).

        Collection endpoints such as POST /profiles create resources that belong to a
        profile, so they must respect read-only mode and the writable-profile set even
        though the URL carries no profile_id.
        """
        if is_read_only():
            error_msg = "Write operation denied: server is in read-only mode"
            logger.warning(f"{error_msg} (method={method}, url={url})")
            return create_access_denied_response(method, url, error_msg, "")

        if get_writable_profiles_set() is None:
            error_msg = "Write access denied: no profiles are writable"
            logger.warning(f"{error_msg} (method={method}, url={url})")
            return create_access_denied_response(method, url, error_msg, "")

        return None

    def _check_collection_read_access(self, method: str, url: str) -> httpx.Response | None:
        """Enforce global read denials for collection endpoints (no profile_id in URL).

        Collection endpoints such as GET /profiles list profile-scoped resources, so
        they must respect the readable-profile deny-all default even though the URL
        carries no profile_id.
        """
        if get_readable_profiles_set() is None:
            error_msg = "Read access denied: no profiles are readable"
            logger.warning(f"{error_msg} (method={method}, url={url})")
            return create_access_denied_response(method, url, error_msg, "")

        return None

    def _check_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check access control for profile operations."""
        if is_write_operation(method):
            return self._check_write_access(profile_id, method, url)
        return self._check_read_access(profile_id, method, url)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        """Make an HTTP request with access control checks.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Returns:
            Response from the API, or a 403 Forbidden response if access is denied
        """
        logger.info(f"HTTP Request: {method} {url}")

        request_path = _normalized_request_path(str(url))
        is_absolute_url = request_path is None
        contains_traversal = request_path is not None and ".." in request_path
        # /profiles paths that name something after the prefix but do not carry a
        # safe, extractable profile id (e.g. /profiles/abc.def/settings).
        unclassifiable_profiles_path = request_path is not None and request_path.rstrip("/").startswith("/profiles/")
        profile_id = _extract_profile_id_from_path(request_path)

        if profile_id:
            error_response = self._check_access(profile_id, method, url)
            if error_response:
                return error_response
        elif is_absolute_url or contains_traversal or unclassifiable_profiles_path:
            # Fail closed: absolute/authority-bearing URLs, traversal payloads, and
            # unclassifiable /profiles paths cannot be matched against the profile
            # ACL, so deny them instead of letting them bypass the check entirely.
            error_msg = f"Forbidden URL: {url!s}"
            logger.warning(f"{error_msg} (method={method})")
            return create_access_denied_response(method, url, error_msg, profile_id or "")
        elif is_write_operation(method):
            # Collection endpoints (e.g., POST /profiles) carry no profile_id but
            # still create profile-scoped resources; enforce global write denials.
            error_response = self._check_collection_write_access(method, url)
        else:
            # Collection reads (e.g., GET /profiles) carry no profile_id but must
            # still respect the readable-profile deny-all default.
            error_response = self._check_collection_read_access(method, url)
        if error_response:
            return error_response

        # No body coercion here: string values in JSON bodies are passed through
        # unchanged. Schema-aware coercion of tool arguments already happens in
        # StripExtraFieldsMiddleware, so blindly coercing body values would corrupt
        # string fields such as profile names ("12345") or passwords ("0012").
        return await super().request(method, url, **kwargs)


def create_nextdns_client() -> httpx.AsyncClient:
    """Create an authenticated HTTP client for NextDNS API with access control.

    Returns:
        httpx.AsyncClient: Configured async HTTP client with authentication and access control
    """
    headers = {
        "X-Api-Key": get_api_key(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # Remove any headers that weren't set to actual values to satisfy type checkers
    clean_headers = {key: value for key, value in headers.items() if value is not None}

    return AccessControlledClient(
        base_url=NEXTDNS_BASE_URL,
        headers=clean_headers,
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
