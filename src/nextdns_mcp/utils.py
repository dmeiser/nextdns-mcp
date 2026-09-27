"""Shared validation and API request utilities for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import re
from typing import Any

import httpx
import json

from . import client
from .client import SAFE_PROFILE_ID_PATTERN, AccessDeniedError, _log_safe_error, _redacted
from .config import get_default_profile
from .errors import (
    ErrorCode,
    NextDNSAuthError,
    NextDNSError,
    NextDNSRateLimitError,
    NextDNSServerError,
    error_payload,
    http_error_payload,
)

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

    if not profile_id:
        return None, error_payload(
            ErrorCode.MISSING_PROFILE_ID,
            "profile_id is required for this tool; NEXTDNS_DEFAULT_PROFILE is not used by this operation",
        )
    if not is_safe_profile_id(profile_id):
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


def _optional_entry_id_error(entry_id: str | None) -> dict[str, Any] | None:
    """Validate an entry id that is only required by some operations."""
    if entry_id is None:
        return None
    return _validate_entry_id(entry_id)


def _cap_limit(value: int | None, cap: int) -> int | None:
    """Clamp a caller-supplied limit to the server-side range ``[1, cap]``.

    Returns the clamped value, or None when the caller supplied no limit.
    Values above the cap are clamped down to it and non-positive values are
    raised to 1, so a bad caller value can never be forwarded upstream as
    ``?limit=-1`` (issue #267).
    """
    if value is None:
        return None
    if value < 1:
        return 1
    if value > cap:
        return cap
    return value


def _build_series_params(
    from_time: str | int | None = None,
    to_time: str | int | None = None,
    interval: int | None = None,
    alignment: str | None = None,
    timezone: str | None = None,
    partials: str | None = None,
) -> dict[str, Any]:
    """Build the query-param dict shared by the analytics ``;series`` endpoints.

    Both ``queryAnalytics(series=True)`` and ``plotAnalytics`` read the same
    ``/analytics/{metric};series`` endpoint, so they build the same parameter
    set through this one helper and cannot drift (issue #267). The ``;series``
    endpoints take no ``limit``, so callers that expose one (as the plot tool
    does) keep it out of this set and validate it on its own.
    """
    return _build_query_params(
        **{
            "from": from_time,
            "to": to_time,
            "interval": interval,
            "alignment": alignment,
            "timezone": timezone,
            "partials": partials,
        }
    )


def _build_query_params(**kwargs: Any) -> dict[str, Any]:
    """Build a query-param dict, dropping None values and normalizing booleans."""
    params = {k: v for k, v in kwargs.items() if v is not None}
    return {k: ("true" if v else "false") if isinstance(v, bool) else v for k, v in params.items()}


def access_denied_payload(exc: AccessDeniedError) -> dict[str, Any]:
    """Build the standardized error payload for an ACL denial (issue #178).

    Carries the typed denial ``code`` (``read_access_denied`` /
    ``write_access_denied`` / ``access_denied``) and the full denial reason in
    ``error``, with a nominal ``status_code`` of 403 for caller compatibility.
    """
    payload = error_payload(exc.code, exc.message, status_code=403)
    payload["profile_id"] = exc.profile_id
    return payload


def _handle_api_error(e: NextDNSError) -> dict[str, Any]:
    """Convert a NextDNSError into a standardized error payload.

    If the exception carries an error_payload (e.g., from an API-level error),
    return it directly. Otherwise, if the exception wraps an httpx.HTTPError,
    use http_error_payload to preserve the original error structure.
    For any other case, fall back to a generic internal error.
    """
    if getattr(e, "error_payload", None) is not None:
        return e.error_payload
    cause = getattr(e, "__cause__", None)
    if cause is not None and isinstance(cause, httpx.HTTPError):
        return http_error_payload(str(e), cause)
    return error_payload(ErrorCode.INTERNAL_ERROR, str(e))


async def _api_request(
    method: str, url: str, params: dict[str, Any] | None = None, json_body: Any = None
) -> dict[str, Any]:
    """Make an HTTP request through the access-controlled client and return JSON.

    Access-control denials raised by the ACL layer are reported as standardized
    error payloads (see ``errors.py``) rather than raised exceptions, so callers
    get a consistent, typed failure shape:
    ``{"error": ..., "code": "read_access_denied" | "write_access_denied" | "access_denied", "status_code": 403, ...}``.

    All other failures are raised as typed exceptions with status_code and response_body attributes, so callers can implement retry-with-backoff for 429 or circuit-breaking for 5xx.
    ``NextDNSError`` is the base exception, with subclasses ``NextDNSAuthError`` (401/403), ``NextDNSRateLimitError`` (429), and ``NextDNSServerError`` (5xx).
    For non-HTTP failures, a ``NextDNSError`` is raised with status_code=None.
    """
    try:
        response = await client.api_client.request(method, url, params=params, json=json_body)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"success": True}
        return response.json()
    except AccessDeniedError as e:
        # Raised by the ACL layer before any network request. Kept out of the
        # httpx.HTTPError branch: a real upstream 403 (raise_for_status) must
        # keep its existing http_error path.
        logger.warning(f"Access denied in {method} {_redacted(url)}: {_log_safe_error(e)}")
        return access_denied_payload(e)
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in {method} {_redacted(url)}: {_log_safe_error(e)}")
        message = f"HTTP error in {method} {url}: {e}"
        response = getattr(e, 'response', None)
        status_code = getattr(response, 'status_code', None) if response is not None else None
        response_body = getattr(response, 'text', None) if response is not None else None
        if status_code in (401, 403):
            raise NextDNSAuthError(message, status_code=status_code, response_body=response_body) from e
        elif status_code == 429:
            raise NextDNSRateLimitError(message, status_code=status_code, response_body=response_body) from e
        elif status_code is not None and 500 <= status_code < 600:
            raise NextDNSServerError(message, status_code=status_code, response_body=response_body) from e
        else:
            raise NextDNSError(message, status_code=status_code, response_body=response_body) from e
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error in {method} {_redacted(url)}: {_log_safe_error(e)}")
        raise NextDNSError(f"Unexpected error in {method} {url}: {e}", status_code=None, response_body=str(e)) from e
