"""Shared validation and API request utilities for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import re
from typing import Any

import httpx

from . import client
from .client import SAFE_PROFILE_ID_PATTERN
from .config import get_default_profile
from .errors import ErrorCode, error_payload, http_error_payload

logger = logging.getLogger(__name__)

# Entry IDs are domain-like identifiers with a looser safe-character set.
SAFE_ENTRY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")


def is_safe_profile_id(value: str | int) -> bool:
    """Return True if value is a spec-shaped profile_id (6 lowercase alphanumeric chars)."""
    return bool(SAFE_PROFILE_ID_PATTERN.match(str(value)))


def is_safe_entry_id(value: str) -> bool:
    """Return True if value is a safe entry_id segment (allows domain-like IDs).

    Rejects path separators and parent-directory sequences that could be used
    for path traversal when the ID is embedded in a URL path.
    """
    if not value or "/" in value or "\\" in value or ".." in value:
        return False
    return bool(SAFE_ENTRY_ID_PATTERN.match(value))


def resolve_profile_id(
    profile_id: str | int | None = None,
    *,
    allow_default: bool = True,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve and validate a NextDNS profile ID with optional default fallback.

    Args:
        profile_id: The profile ID to resolve and validate, or None.
        allow_default: If True (default), fall back to the configured default
            profile when profile_id is None or empty. If False, profile_id is
            mandatory.

    Returns:
        A tuple of ``(resolved_profile_id, error_payload)``. On success, the
        first element is the validated 6-character profile ID string and the
        second is None. On failure, the first element is None and the second
        is a standardized error payload dict.
    """
    if allow_default:
        target_profile: str | int | None = profile_id
        if not target_profile:
            target_profile = get_default_profile()
        if not target_profile:
            return None, error_payload(
                ErrorCode.MISSING_PROFILE_ID,
                "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
                hint="Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
            )
        if not is_safe_profile_id(target_profile):
            return None, error_payload(
                ErrorCode.INVALID_PROFILE_ID,
                f"Invalid profile_id format: {target_profile}",
            )
        return str(target_profile), None

    if profile_id is None or not is_safe_profile_id(profile_id):
        return None, error_payload(
            ErrorCode.INVALID_PROFILE_ID,
            f"Invalid profile_id format: {profile_id}",
        )
    return str(profile_id), None


def _validate_entry_id(entry_id: str) -> dict[str, Any] | None:
    """Return an error dict if entry_id is not a safe identifier."""
    if not is_safe_entry_id(entry_id):
        return error_payload(ErrorCode.INVALID_ENTRY_ID, f"Invalid entry_id format: {entry_id}")
    return None


def _cap_limit(value: int | None, cap: int) -> tuple[int | None, bool]:
    """Clamp a caller-supplied limit to a server-side cap.

    Returns the (possibly capped) value and whether capping occurred.
    Values at or below the cap pass through untouched; values above the cap
    are clamped down to it.
    """
    if value is None:
        return None, False
    if value > cap:
        return cap, True
    return value, False


def _build_query_params(**kwargs: Any) -> dict[str, Any]:
    """Build a query-param dict, dropping None values and normalizing booleans."""
    params = {k: v for k, v in kwargs.items() if v is not None}
    return {k: ("true" if v else "false") if isinstance(v, bool) else v for k, v in params.items()}


async def _api_request(method: str, url: str, params: dict[str, Any] | None = None, json: Any = None) -> dict[str, Any]:
    """Make an HTTP request through the access-controlled client and return JSON.

    Failures are reported as standardized error payloads (see ``errors.py``) rather
    than raised exceptions, so callers get a consistent, typed failure shape:
    ``{"error": ..., "code": "http_error", "status_code": ...}`` for HTTP failures
    (401/403/429/5xx are distinguishable via ``status_code``) and
    ``{"error": ..., "code": "internal_error"}`` for non-HTTP failures.
    """
    try:
        response = await client.api_client.request(method, url, params=params, json=json)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"success": True}
        return response.json()
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in {method} {url}: {e}")
        message = f"HTTP error in {method} {url}: {e}"
        return http_error_payload(message, e, fallback_code=ErrorCode.HTTP_ERROR)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error in {method} {url}: {e}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Unexpected error in {method} {url}: {e}")
