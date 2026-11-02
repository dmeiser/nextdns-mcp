"""Shared validation and normalization helpers for NextDNS MCP Server.

SPDX-License-Identifier: MIT

This module validates and normalizes values - profile ids, time ranges,
``partials`` spellings, query-param shapes - and nothing else. It imports no
network layer: the request path lives in ``transport``, the access control in
``client``, and the names those layers share (the profile-id shape, the hint
its failures repeat) live in ``constants`` so no module here has to import a
transport to describe an id.
"""

import re
from typing import Any

from .config import get_default_profile
from .constants import PROFILE_ID_HINT, SAFE_PROFILE_ID_PATTERN
from .errors import ErrorCode, error_payload


def is_safe_profile_id(value: str | int) -> bool:
    """Return True if value is an API-shaped profile_id (6 or 10 lowercase alphanumeric chars).

    The live API issues 10-character IDs for new profiles; legacy profiles
    carry 6-character IDs. Anything else fails fast here instead of 404ing
    upstream.
    """
    return bool(SAFE_PROFILE_ID_PATTERN.match(str(value)))


def is_integer_shaped(value: str) -> bool:
    """Return True if value is a string that represents an integer.

    Decimal digits (optionally signed with ``-``) qualify, so non-decimal
    digit-like characters such as ``"²"`` are rejected instead of relying on
    ``int()`` accepting them inconsistently.
    """
    try:
        int(value)
        return value.isdecimal() or (value.startswith("-") and value[1:].isdecimal())
    except ValueError:
        return False


def is_float_shaped(value: str) -> bool:
    """Return True if value is a string that could represent a float.

    At most one ``.`` and one ``-`` are ignored when testing for decimal digits,
    so malformed values (``"3.14.15"``) and Unicode digit-like characters
    (``"1.²"``) are rejected. Parsing may still fail for an accepted shape; the
    caller is responsible for handling that.
    """
    return value.replace(".", "", 1).replace("-", "", 1).isdecimal()


def _missing_profile_payload() -> dict[str, Any]:
    """The payload for a call that named no profile id at all."""
    return error_payload(
        ErrorCode.MISSING_PROFILE_ID,
        "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
        hint="Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
    )


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
        first element is the validated profile ID string and the second is
        None. On failure, the first element is None and the second
        is a standardized error payload dict.
    """
    if allow_default:
        target_profile: str | int | None = profile_id or get_default_profile()
        if not target_profile:
            return None, _missing_profile_payload()
        if not is_safe_profile_id(target_profile):
            return None, error_payload(
                ErrorCode.INVALID_PROFILE_ID,
                f"Invalid profile_id format: {target_profile}",
                hint=PROFILE_ID_HINT,
            )
        return str(target_profile), None

    return _resolve_required_profile_id(profile_id)


def _resolve_required_profile_id(profile_id: str | int | None) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a mandatory profile_id: no NEXTDNS_DEFAULT_PROFILE fallback."""
    if not profile_id:
        return None, error_payload(
            ErrorCode.MISSING_PROFILE_ID,
            "profile_id is required for this tool; NEXTDNS_DEFAULT_PROFILE is not used by this operation",
        )
    if not is_safe_profile_id(profile_id):
        return None, error_payload(
            ErrorCode.INVALID_PROFILE_ID,
            f"Invalid profile_id format: {profile_id}",
            hint=PROFILE_ID_HINT,
        )
    return str(profile_id), None


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


# The spellings of a relative range that the analytics endpoints do NOT accept
# but callers reach for: `now-1d` (Grafana/Prometheus) and a bare negative
# number, which upstream reads as an absolute Unix timestamp.
_NOW_OFFSET_RE = re.compile(r"^now\s*(?:-\s*(\d+\s*[smhdwy]))?$", re.IGNORECASE)
_BARE_NEGATIVE_RE = re.compile(r"^-\d+$")


def normalize_time_value(value: Any) -> Any:
    """Rewrite a ``from``/``to`` argument into a form the API accepts.

    Two shapes arrive live and both fail silently or obscurely upstream:

    - A bare negative number, ``from_time=-86400``. As an absolute Unix
      timestamp that is 1969, so the endpoint answers 200 with no rows and the
      caller has nothing to correct: across three runs the model retried that
      call ~40 times, varying the interval each time. A negative absolute time
      is not a thing, so it is read as the offset it means, in the API's own
      relative-seconds spelling (``-86400s``, verified accepted).
    - ``now-1d``, the Grafana/Prometheus spelling. Upstream answers "`from` must
      be number"; the same span in its own spelling (``-1d``) works.

    Anything else - a Unix timestamp, ISO 8601, ``now``, ``-1d`` - is returned
    unchanged.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return f"{value}s" if value < 0 else value
    return _normalize_time_text(value)


def _normalize_time_text(value: Any) -> Any:
    """Rewrite the two text spellings the API reads wrongly (see normalize_time_value)."""
    text = str(value).strip()
    match = _NOW_OFFSET_RE.match(text)
    if match:
        return "now" if match.group(1) is None else f"-{match.group(1).replace(' ', '')}"
    if _BARE_NEGATIVE_RE.match(text):
        return f"{text}s"
    return value


# The two values the `;series` endpoints accept for `partials` (probed live:
# `none` and `all` answer 200, everything else answers 400 enum without naming
# the allowed set). Callers send a boolean instead.
PARTIALS_VALUES = ("none", "all")
_PARTIALS_ALIASES = {
    "false": "none",
    "0": "none",
    "no": "none",
    "off": "none",
    "true": "all",
    "1": "all",
    "yes": "all",
    "on": "all",
}


def normalize_partials(value: Any) -> Any:
    """Map a boolean-ish ``partials`` onto the value the API means by it.

    ``partials=false`` was sent live and answered 400 with an enum error that
    names no allowed values. "no partials" is ``none`` and "partials" is ``all``;
    anything else is left as sent so the tool can reject it by name.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    return _PARTIALS_ALIASES.get(text, text)


def _query_bool(value: Any) -> Any:
    """Render a bool the way the API reads it; every other value passes through."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _build_query_params(**kwargs: Any) -> dict[str, Any]:
    """Build a query-param dict, dropping None values and normalizing booleans."""
    return {k: _query_bool(v) for k, v in kwargs.items() if v is not None}


def schema_required(schema: dict[str, Any] | None) -> list[str]:
    """The ``required`` list of a JSON-Schema object, as strings ([] if absent).

    One reading of the one contract: the tools publish their parameter schema
    with a ``required`` list, and both the generated-tool layer and the
    disclosure layer name those arguments to callers. Two hand-rolled reads
    of the same field would be two chances to read it differently.
    """
    return [str(name) for name in ((schema or {}).get("required") or [])]
