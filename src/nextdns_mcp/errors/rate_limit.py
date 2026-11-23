"""Rate-limit metadata read off a failed response's headers.

A 429 answer carries when the caller may come back, and NextDNS says it in two
dialects at once: ``Retry-After`` (seconds or an HTTP-date) and the
``X-RateLimit-*`` trio. This module owns reading both; it is a leaf that imports
nothing else from the package, so the delay parsing cannot reach the body or
document parsers beside it.

SPDX-License-Identifier: MIT
"""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

_RATE_LIMIT_HEADER_FIELDS = (
    ("X-RateLimit-Limit", "rate_limit_limit"),
    ("X-RateLimit-Remaining", "rate_limit_remaining"),
    ("X-RateLimit-Reset", "rate_limit_reset"),
)


def _parse_http_date_delay(text: str) -> float | None:
    """Parse an HTTP-date string into seconds from now, clamped at zero."""
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    normalized = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return max(0.0, (normalized - datetime.now(UTC)).total_seconds())


def _parse_retry_after(raw: Any) -> float | None:
    """Parse an HTTP Retry-After value (seconds or HTTP-date) into non-negative seconds."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        seconds = float(text)
    except ValueError:
        return _parse_http_date_delay(text)
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _attach_x_ratelimit_metadata(payload: dict[str, Any], headers: Any) -> None:
    """Copy non-empty X-RateLimit-* header strings onto a 429 error payload."""
    for header_name, field in _RATE_LIMIT_HEADER_FIELDS:
        raw = headers.get(header_name)
        if isinstance(raw, str) and raw.strip():
            payload.setdefault(field, raw.strip())


def attach_rate_limit_headers(payload: dict[str, Any], response: Any) -> None:
    """Surface Retry-After and X-RateLimit-* headers when present on a 429 response.

    Public by name because ``errors.http`` is the composer that calls it; the
    parsing helpers it uses stay this module's own.
    """
    if payload.get("status_code") != 429:
        return
    headers = getattr(response, "headers", None)
    if headers is None:
        return
    delay = _parse_retry_after(headers.get("Retry-After"))
    if delay is not None:
        payload.setdefault("retry_after", round(delay, 3))
    _attach_x_ratelimit_metadata(payload, headers)
