"""Standardized structured error payloads for NextDNS MCP tools.

Every tool failure is reported as a plain dict with a stable, machine-readable
``code`` and a human-readable ``error`` message. Callers (including LLMs) can
therefore predict the failure shape regardless of which tool produced it, and can
branch on the typed ``code`` instead of parsing opaque exception text.

SPDX-License-Identifier: MIT
"""

from typing import Any

# Server-side cap on the number of characters of an unparseable upstream
# response body inlined into an error payload. The tool result is read by an
# LLM client, so a multi-hundred-KB HTML error page must not be copied
# verbatim into the context window for a request that already failed.
MAX_RESPONSE_BODY_CHARS = 2048

# Marker appended to a response body that was cut at the cap.
TRUNCATION_MARKER = "... [truncated]"

# Typed fields callers branch on (issue #148 contract): never bounded when a
# structured error document is inlined into a tool result.
_UNBOUNDED_STRUCTURED_FIELDS = frozenset({"code", "error", "status_code"})


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
    DOWNLOAD_TOO_LARGE = "download_too_large"
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


def _bound_value(value: Any) -> tuple[Any, bool]:
    """Bound every free-form string in a JSON value, at any depth.

    Strings are capped at ``MAX_RESPONSE_BODY_CHARS`` and cut values carry
    ``TRUNCATION_MARKER``; dicts and lists are rebuilt with their elements
    bounded. Other scalars pass through unchanged.

    Args:
        value: Any JSON-decoded value.

    Returns:
        A ``(bounded_value, truncated)`` pair.
    """
    if isinstance(value, str):
        if len(value) > MAX_RESPONSE_BODY_CHARS:
            return value[:MAX_RESPONSE_BODY_CHARS] + TRUNCATION_MARKER, True
        return value, False
    if isinstance(value, dict):
        bounded: dict[Any, Any] = {}
        truncated = False
        for key, item in value.items():
            bounded[key], cut = _bound_value(item)
            truncated = truncated or cut
        return bounded, truncated
    if isinstance(value, list):
        bounded_items = [_bound_value(item) for item in value]
        return [item for item, _ in bounded_items], any(cut for _, cut in bounded_items)
    return value, False


def _bound_structured_fields(payload: dict[str, Any]) -> bool:
    """Bound free-form values in an inlined structured error document.

    Every free-form string is capped at ``MAX_RESPONSE_BODY_CHARS`` (nested in
    dicts and lists too) and cut values are flagged with ``TRUNCATION_MARKER``,
    so a multi-hundred-KB upstream ``details`` field cannot crowd the LLM
    context for a request that already failed (issue #297). Typed fields
    (``code``, ``error``, ``status_code``) are exempt: the issue #148 contract
    has callers branch on them, so they are surfaced verbatim.

    Args:
        payload: The structured error document; bounded in place.

    Returns:
        True when at least one field was truncated.
    """
    truncated = False
    for key, value in payload.items():
        if key in _UNBOUNDED_STRUCTURED_FIELDS:
            continue
        payload[key], cut = _bound_value(value)
        truncated = truncated or cut
    return truncated


def http_error_payload(message: str, exc: Exception, fallback_code: str = ErrorCode.HTTP_ERROR) -> dict[str, Any]:
    """Build a typed error payload for a failed HTTP request.

    If the failed response body is a structured JSON error, its fields are
    surfaced so they are not lost, with free-form string fields bounded at
    ``MAX_RESPONSE_BODY_CHARS`` and the payload flagged with
    ``response_body_truncated`` when any field was cut (issue #297). Otherwise a
    generic payload built from ``fallback_code`` is returned, always carrying
    ``status_code`` when the response has one. An unparseable body is included
    only as a bounded prefix, flagged with ``response_body_truncated``. (ACL
    denials do not reach this helper: the access-control layer raises the typed
    ``AccessDeniedError`` instead of faking a response.)

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
            if _bound_structured_fields(payload):
                payload["response_body_truncated"] = True
            return payload

    payload = error_payload(fallback_code, message, status_code=status_code)
    body = getattr(response, "text", None) if response is not None else None
    if body:
        truncated = len(body) > MAX_RESPONSE_BODY_CHARS
        payload["response_body"] = body[:MAX_RESPONSE_BODY_CHARS] + (TRUNCATION_MARKER if truncated else "")
        payload["response_body_truncated"] = truncated
    return payload


class NextDNSError(RuntimeError):
    """Base exception for NextDNS API errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class NextDNSAuthError(NextDNSError):
    """Raised for 401 and 403 errors."""


class NextDNSRateLimitError(NextDNSError):
    """Raised for 429 errors."""


class NextDNSServerError(NextDNSError):
    """Raised for 5xx errors."""
