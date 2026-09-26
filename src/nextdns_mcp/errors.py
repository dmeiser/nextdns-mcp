"""Standardized structured error payloads for NextDNS MCP tools.

Every tool failure is reported as a plain dict with a stable, machine-readable
``code`` and a human-readable ``error`` message. Callers (including LLMs) can
therefore predict the failure shape regardless of which tool produced it, and can
branch on the typed ``code`` instead of parsing opaque exception text.

SPDX-License-Identifier: MIT
"""

from typing import Any


# Typed error codes. These form a stable external contract: consumers branch on
# the code, never on the free-form message. Add new codes here; do not reuse an
# existing code for a different failure class.
class ErrorCode:
    """Typed codes for the standardized error payload contract."""

    INVALID_PROFILE_ID = "invalid_profile_id"
    INVALID_ENTRY_ID = "invalid_entry_id"
    INVALID_RECORD_TYPE = "invalid_record_type"
    MISSING_PROFILE_ID = "missing_profile_id"
    READ_ACCESS_DENIED = "read_access_denied"
    WRITE_ACCESS_DENIED = "write_access_denied"
    ACCESS_DENIED = "access_denied"
    MISSING_REQUIRED_ARGUMENT = "missing_required_argument"
    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNSUPPORTED_METRIC = "unsupported_metric"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"
    NO_DATA = "no_data"


def error_payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build a standardized error payload with a typed ``code`` and ``error`` message.

    Args:
        code: A stable typed error code (see :class:`ErrorCode`).
        message: A human-readable description of the failure.
        **extra: Optional context fields (e.g. ``status_code``, ``profile_id``).

    Returns:
        A dict of the form ``{"error": message, "code": code, **extra}``.
    """
    payload: dict[str, Any] = {"error": message, "code": code}
    payload.update(extra)
    return payload


def http_error_payload(message: str, exc: Exception, fallback_code: str = ErrorCode.HTTP_ERROR) -> dict[str, Any]:
    """Build a typed error payload for a failed HTTP request.

    If the failed response body is a structured JSON error - for example the
    synthetic 403 emitted by the access-controlled client for an ACL denial - its
    fields (including the typed ``code`` and the denial reason) are surfaced so
    they are not lost. Otherwise a generic payload built from ``fallback_code`` is
    returned, always carrying ``status_code`` when the response has one.

    Args:
        message: The human-readable message used when no structured body exists.
        exc: The caught exception; a ``response`` attribute is read when present.
        fallback_code: Typed code used when the body is not a structured error.

    Returns:
        A standardized error payload dict.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) if response is not None else None

    if response is not None:
        try:
            parsed = response.json()
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error"):
            payload = dict(parsed)
            payload.setdefault("code", fallback_code)
            payload.setdefault("status_code", status_code)
            return payload

    payload = error_payload(fallback_code, message, status_code=status_code)
    body = getattr(response, "text", None) if response is not None else None
    if body:
        payload["response_body"] = body
    return payload
