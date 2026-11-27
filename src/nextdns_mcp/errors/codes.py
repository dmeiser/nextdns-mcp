"""The typed error codes and the payload builder every failure path starts from.

This module is a leaf: it reads no response, no header and no upstream document,
and imports nothing else from the package, so a caller that only needs a typed
``code`` (``client``, ``utils``, ``spec_tools``, ``charts`` and the tool modules)
executes no parsing code to get one.

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
    UNSUPPORTED_METRIC = "unsupported_metric"
    HTTP_ERROR = "http_error"
    DOWNLOAD_TOO_LARGE = "download_too_large"
    RESPONSE_TOO_LARGE = "response_too_large"
    INTERNAL_ERROR = "internal_error"
    NO_DATA = "no_data"
    TIMEOUT = "timeout"


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
