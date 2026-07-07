"""HTTP client with profile access control for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import posixpath
import re
from typing import Any, Optional

import httpx

from .config import (
    NEXTDNS_BASE_URL,
    can_read_profile,
    can_write_profile,
    get_api_key,
    get_http_timeout,
    is_read_only,
)
from .coercion import coerce_json_types

logger = logging.getLogger(__name__)

# Safe identifier pattern to prevent path traversal and ACL bypass.
_SAFE_PROFILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def extract_profile_id_from_url(url: str) -> Optional[str]:
    """Extract profile_id from a URL path.

    Args:
        url: The URL path (e.g., "/profiles/abc123/settings")

    Returns:
        The profile_id if found and safe, None otherwise
    """
    # Reject any path containing parent-directory references defensively.
    # This blocks traversal payloads such as /profiles/allowed123/../../profiles/denied456
    # before normalization.
    if ".." in url:
        return None

    # Normalize the path so that equivalent paths are treated consistently.
    normalized = posixpath.normpath(url)
    # Match /profiles/{profile_id}/... pattern
    match = re.match(r"^/?profiles/([^/]+)(?:/|$)", normalized)
    if match:
        profile_id = match.group(1)
        if _SAFE_PROFILE_ID_PATTERN.match(profile_id):
            return profile_id
    return None


def is_write_operation(method: str) -> bool:
    """Check if an HTTP method is a write operation.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE)

    Returns:
        True if it's a write operation, False otherwise
    """
    return method.upper() in ("POST", "PUT", "PATCH", "DELETE")


def create_access_denied_response(method: str, url: str, error_msg: str, profile_id: str) -> httpx.Response:
    """Create a 403 Forbidden response for access denied scenarios.

    Args:
        method: HTTP method
        url: Request URL
        error_msg: Error message to include in response
        profile_id: The profile ID that was denied access

    Returns:
        403 Forbidden Response object
    """
    response = httpx.Response(
        status_code=403,
        json={"error": error_msg, "profile_id": profile_id},
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
        return create_access_denied_response(method, url, error_msg, profile_id)

    def _check_read_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check read access and return error response if denied."""
        if can_read_profile(profile_id):
            return None

        error_msg = f"Read access denied for profile: {profile_id}"
        logger.warning(f"{error_msg} (method={method}, url={url})")
        return create_access_denied_response(method, url, error_msg, profile_id)

    def _check_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check access control for profile operations."""
        if is_write_operation(method):
            return self._check_write_access(profile_id, method, url)
        return self._check_read_access(profile_id, method, url)

    def _coerce_json_body(self, kwargs: dict[str, Any]) -> None:
        """Coerce string types in JSON request body.

        This handles type coercion for parameters passed as strings by clients like
        Docker MCP CLI. FastMCP's OpenAPI integration may pass string values for
        boolean/integer fields which need to be coerced before sending to the API.
        """
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            kwargs["json"] = coerce_json_types(kwargs["json"])
            logger.debug(f"Coerced JSON body: {kwargs['json']}")

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

        profile_id = extract_profile_id_from_url(str(url))
        if profile_id:
            error_response = self._check_access(profile_id, method, url)
            if error_response:
                return error_response

        self._coerce_json_body(kwargs)
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


# Create authenticated HTTP client (module-level for access by helper functions)
api_client = create_nextdns_client()
