"""The analytics series point budget and the ``;series`` response ceiling.

This module holds the one responsibility of pricing a generated ``;series``
call before it goes out (the same 2000-point budget ``plotAnalytics``
enforces, with the endpoint's adaptive interval default estimated for calls
that state none) and of refusing an over-wide answer at every layer it can
come back through: the transport's streaming byte cap, measured on the wire,
and the serialized backstop for transports that were never asked to cap.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from fastmcp.utilities.openapi import HTTPRoute

from . import transport as _transport
from .charts import current_time, resolve_time_point, series_budget_error
from .errors import ErrorCode, error_payload
from .transport import MAX_SERIES_RESPONSE_BYTES

# The endpoint's adaptive series-interval default, priced for calls that state
# no interval. Probed live 2026-11: windows over 7 days answer at 86400s with
# no self-cap (a 4000-day range returns 4000 points), 7-day windows at 10800s.
_UNINTERVALLED_FINEST_DEFAULT = 10800


_UNINTERVALLED_FINEST_WINDOW = 7 * 86400


_UNINTERVALLED_COARSEST_DEFAULT = 86400


def _interval_seconds(value: Any) -> int | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _series_budget_for_generated_call(route: HTTPRoute, query: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse a generated ``;series`` call whose range, interval or row count outpoint the budget.

    Two ceilings are enforced before the request is constructed, on every
    call that states a ``from``:

    * Per series: the same 2000-point budget ``plotAnalytics`` enforces
      (issue #267 applies to both doors). A call that names no interval is
      priced at the endpoint's adaptive default rather than passed through:
      probed live 2026-11, windows over 7 days answer at 86400 with **no
      self-cap** - a 4000-day range returns 4000 points - so the old
      "default is already bounded" pass-through was wrong. The estimate is
      10800s for windows up to 7 days (probed: 7d -> 10800) and 86400s above
      it (probed: 30d, 365d, 4000d all answer at 86400), which bounds every
      no-interval call the same way a stated interval is bounded.
    * Across the whole response: rows a ``;series`` endpoint returns are
      capped upstream (a default top-N this server does not publish), so the
      row count is not fully ours to price up front; the fetched payload
      gets a byte ceiling instead (:meth:`_series_response_ceiling_error`).

    A stated ``to`` of 0 is the epoch, not an omission: ``or`` would silently
    swap in ``now`` and re-price the window.
    """
    if ";series" not in route.path or query.get("from") is None:
        return None
    return _price_series_range(query["from"], query.get("to"), _interval_seconds(query.get("interval")))


def _price_series_range(from_time: Any, to_time_raw: Any, interval: int | None) -> dict[str, Any] | None:
    """Price one series call's range, estimating the interval default when unnamed."""
    if interval is None:
        interval = _default_interval_estimate(from_time, to_time_raw)
        if interval is None:
            return None  # unresolvable range: the endpoint gives its own 400
    to_time = "now" if to_time_raw is None else to_time_raw
    return series_budget_error(from_time, to_time, interval)


def _default_interval_estimate(from_time: Any, to_time_raw: Any) -> int | None:
    """Price the endpoint's adaptive interval default for a no-interval call.

    Probed live 2026-11: windows over 7 days answer at 86400s with no
    self-cap (a 4000-day range returns 4000 points), 7-day windows at
    10800s. Neither range resolving (garbage input) means no price here -
    the endpoint gives its own 400.
    """
    reference = current_time()
    to_time = "now" if to_time_raw is None else to_time_raw
    start = resolve_time_point(from_time, reference)
    end = resolve_time_point(to_time, reference)
    if start is None or end is None:
        return None
    return (
        _UNINTERVALLED_FINEST_DEFAULT
        if end - start <= _UNINTERVALLED_FINEST_WINDOW
        else _UNINTERVALLED_COARSEST_DEFAULT
    )


def _capped_transport_kwargs(route: HTTPRoute, request: Callable[..., Any]) -> dict[str, Any]:
    """The transport kwargs a call needs: the cap only for routes that want it and transports that take it."""
    if ";series" in route.path and _transport_accepts_cap(request):
        return {"max_response_bytes": MAX_SERIES_RESPONSE_BYTES}
    return {}


# A capability answer belongs to one transport object, so the memo is keyed on
# identity rather than equality: a transport whose class defines __eq__ would
# otherwise let one transport answer for another. The callable is kept beside
# its answer so the key's id cannot be recycled while the entry lives.
_CAP_SUPPORT_BY_REQUEST: dict[int, tuple[Callable[..., Any], bool]] = {}


def _ceiling_measured_on_the_wire(request: Callable[..., Any], transport_kwargs: dict[str, Any]) -> bool:
    """Whether the transport that answered already held the body under the ceiling.

    Only the transport this package ships enforces the cap: it streams under
    ``max_response_bytes`` and refuses the overshoot (see
    ``transport._request_capped``), so the byte count of anything it hands back
    is a fact established on the wire, and re-serializing that payload to price
    it again measures a bound the wire already proved. A signature is not that
    evidence - :func:`_transport_accepts_cap` decides only how the call is
    *shaped*, and an injected callable can name ``max_response_bytes`` and
    ignore it - so the serialized backstop in
    :func:`_series_response_ceiling_error` stays in charge of every other
    transport, and of this one when a test replaces it by attribute.
    """
    return bool(transport_kwargs) and request is _transport._api_request_payload


def _transport_accepts_cap(request: Callable[..., Any]) -> bool:
    """Whether the resolved transport accepts a ``max_response_bytes`` keyword.

    ``transport`` exists so a caller can inject a per-tool request function,
    and a callable written before the cap existed has exactly the legacy
    ``(method, url, params, json_body)`` signature. Passing the cap kwarg to
    such a callable would raise ``TypeError`` before its first line runs, so
    the capability is probed from the signature instead: a legacy transport
    keeps the old call shape, and its oversized ``;series`` answers are still
    refused by the serialized backstop in :func:`_series_refusal`. A callable
    whose signature cannot be introspected is treated as legacy for the same
    reason - the unpriced path still enforces the ceiling, the priced one
    must not guess.

    The transport is fixed per route and a callable's signature cannot change
    while the callable lives, so the answer is memoized per callable: reading
    the signature again on every dispatch can only return what was just read.
    """
    cached = _CAP_SUPPORT_BY_REQUEST.get(id(request))
    if cached is not None and cached[0] is request:
        return cached[1]
    accepted = _probe_transport_cap(request)
    _CAP_SUPPORT_BY_REQUEST[id(request)] = (request, accepted)
    return accepted


def _probe_transport_cap(request: Callable[..., Any]) -> bool:
    """Read the capability out of the callable's signature."""
    try:
        parameters = inspect.signature(request).parameters
    except (TypeError, ValueError):
        return False
    named = parameters.get("max_response_bytes")
    if named is not None and named.kind in (named.KEYWORD_ONLY, named.POSITIONAL_OR_KEYWORD):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _series_refusal(
    route: HTTPRoute, payload: Any, ceiling_measured_on_the_wire: bool = False
) -> dict[str, Any] | None:
    """The typed ceiling refusal for a ``;series`` result, from either layer.

    The transport enforces the cap while streaming and answers with the typed
    ``response_too_large`` payload; that gets the series wording with no
    measured size claimed (nothing was measured - the refusal came from the
    wire). The serialized check is the backstop for a transport that was never
    asked to cap the body, and ``ceiling_measured_on_the_wire`` says whether
    this call's transport was (see
    :func:`_ceiling_measured_on_the_wire`): a payload the shipped transport
    streamed in under the cap needs no second measurement, because the second
    measurement re-serializes the whole answer to price a bound the wire
    already enforced. The ceiling is a ``;series`` policy only: aggregate
    endpoints (a wide denylist, a big profile listing) legitimately answer
    with larger documents and are not refused.
    """
    if ";series" not in route.path or not isinstance(payload, dict):
        return None
    if payload.get("code") == ErrorCode.RESPONSE_TOO_LARGE:
        return _series_ceiling_payload(str(route.operation_id), None)
    if ceiling_measured_on_the_wire:
        return None
    return _series_response_ceiling_error(route, payload)


def _series_ceiling_payload(operation_id: str, response_bytes: int | None) -> dict[str, Any]:
    """The typed refusal for a ``;series`` answer over the byte ceiling.

    ``response_bytes`` is the measured size when something measured it (the
    serialized backstop); None when the transport refused mid-stream and the
    only known fact is that the cap was crossed.
    """
    answered = (
        f"{response_bytes} bytes" if response_bytes is not None else f"more than {MAX_SERIES_RESPONSE_BYTES} bytes"
    )
    extra: dict[str, Any] = {"max_response_bytes": MAX_SERIES_RESPONSE_BYTES}
    if response_bytes is not None:
        extra["response_bytes"] = response_bytes
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        f"{operation_id} answered {answered}, more than the "
        f"{MAX_SERIES_RESPONSE_BYTES}-byte series response ceiling; the range/interval produced "
        "more data than this server returns in one tool result",
        hint="use a larger interval or a shorter range, or read an aggregate endpoint instead of ;series",
        **extra,
    )


def _series_response_ceiling_error(route: HTTPRoute, payload: Any) -> dict[str, Any] | None:
    """Typed refusal when a fetched ``;series`` payload is too big to hand back.

    The transport enforces ``MAX_SERIES_RESPONSE_BYTES`` while streaming;
    this is the backstop for a transport that answered without being asked to
    cap the body (a legacy injected double, a future custom transport): a
    payload whose JSON is over the ceiling is refused as a typed error rather
    than dumped into the caller's context. Only reached for ``;series``
    routes, and only when :func:`_ceiling_measured_on_the_wire` says the
    answering transport did no measuring of its own.

    What it prices is the re-serialized payload, not the bytes that arrived:
    a compact or non-ASCII body can re-dump larger than the wire it came in
    on (``json.dumps`` escapes every non-ASCII character and spaces its
    separators), which is why this is a backstop and the streamed cap is the
    layer that bounds a normal call.
    """
    try:
        size = len(json.dumps(payload, default=str))
    except (TypeError, ValueError):  # pragma: no cover - transport payloads are JSON-shaped
        size = len(str(payload))
    if size <= MAX_SERIES_RESPONSE_BYTES:
        return None
    return _series_ceiling_payload(str(route.operation_id), size)
