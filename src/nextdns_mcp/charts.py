"""Shared time-series primitives: range resolution, payload shape, chart rows and series budgets.

Both hand-written analytics surfaces consume this module - ``tools.plots`` plots one
series, ``tools.dashboard`` renders many - and neither one owns it. The primitives
used to live in ``tools.plots`` and be imported by name from there, which made a
rename inside the plotting tool a silent break in the dashboard and left a 1,100-line
module reaching across a module boundary for ``_``-prefixed names. Public names here
are the contract; the ``_``-prefixed helpers are this module's own detail.

Nothing in this module imports the Prefab UI component library. The builders that
need it - the dashboard theme, the ``ChartSeries`` metadata, the shared
``AreaChart`` factory - live beside it in ``chart_ui`` (issue #550). The split is
what lets ``spec_tools``, the door every generated tool stands behind, price a
series request through this module without pulling the component tree into a
process that renders nothing.

SPDX-License-Identifier: MIT
"""

import math
import re
import time
from datetime import UTC, datetime
from typing import Any

from .errors import ErrorCode, error_payload

# Maximum number of data points a single series response may contain (issue #267).
# The point count is the requested range divided by the interval, so bounding the
# count - not the interval argument alone - is what keeps a series response small
# enough to buffer and then render. It is enforced as a typed rejection naming the
# limit, never by silently rewriting the caller's request: the ``;series`` endpoints
# take no ``limit`` parameter, so the range is the only exposure knob. The sibling
# bound on the ``interval`` argument itself stays with the tool that publishes it.
PLOT_MAX_POINTS = 2000

# Visual cap on the points one rendered chart carries: even when the series budget
# allows up to PLOT_MAX_POINTS points, embedding that many raw point dicts in
# structured_content bloats MCP/LLM client contexts. When a series exceeds this,
# the visual chart is downsampled into buckets by ``downsample_chart_series``.
# The plotting tool set this ceiling; the dashboard's queries card must render
# under it too - it sits inside a page of other cards, so an unbudgeted chart
# there is worse, not better, than in a single-chart result (issue #550).
PLOT_CHART_MAX_POINTS = 250

# Relative from/to values such as "-1d" or "-7d", with their unit in seconds.
# "y" is a 365-day year, matching how the analytics endpoints read a year. The
# unit is matched case-sensitively: an upper-case letter is a different unit
# upstream ("-1M" reads as months), so folding it onto its lower-case twin
# would size a months-long range as minutes.
_RELATIVE_TIME_RE = re.compile(r"^(-?)(\d+)([smhdwy])$")
_RELATIVE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def _parse_series_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp returned by the NextDNS API.

    Uses ``datetime.fromisoformat`` (which, on the supported Python >=3.12,
    already handles ``'Z'`` suffixes, offsets without colons, and microsecond
    fractions of any length).

    A value carrying no timezone is anchored to UTC, so the same string always
    denotes the same instant no matter which machine resolves it.

    Raises:
        ValueError: If the value matches none of the supported formats.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _extract_series_queries(series_data: list[dict[str, Any]]) -> list[list[Any]]:
    """Read each series' query list once, in series order.

    A missing or null ``queries`` value becomes an empty list here, which is the
    same coalescing the per-point build used to repeat for every timestamp.
    """
    return [series.get("queries") or [] for series in series_data]


def _build_chart_point(
    time_str: str,
    time_index: int,
    query_lists: list[list[Any]],
) -> dict[str, Any]:
    """Validate timestamp and return a single time-aligned data point.

    ``query_lists`` is each series' query list prepared once by the caller (see
    ``_extract_series_queries``), so building a point does no per-series lookups.
    """
    _parse_series_timestamp(time_str)
    point: dict[str, Any] = {"time": time_str}
    for s_idx, queries in enumerate(query_lists):
        point[f"series_{s_idx}"] = queries[time_index] if time_index < len(queries) else 0
    return point


def build_chart_data(
    times: list[str],
    series_data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the list of data rows for the Prefab LineChart.

    Each series' query list is extracted before the timestamp loop, so a chart of
    N intervals over M series reads M query lists instead of N x M of them
    (issue #434).
    """
    query_lists = _extract_series_queries(series_data)
    return [_build_chart_point(t, idx, query_lists) for idx, t in enumerate(times)]


def _bucket_series_queries(queries: list[int] | None, n: int, bucket_size: int) -> list[int]:
    """Sum a single series' query counts into chunks of ``bucket_size``."""
    q = queries or []
    return [sum(q[start : min(start + bucket_size, n)]) for start in range(0, n, bucket_size)]


def _bucket_single_series(series: dict[str, Any], n: int, bucket_size: int) -> dict[str, Any]:
    """Return a copy of ``series`` with its query counts bucketed."""
    updated = dict(series)
    updated["queries"] = _bucket_series_queries(series.get("queries"), n, bucket_size)
    return updated


def downsample_chart_series(
    times: list[str],
    series_data: list[dict[str, Any]],
    max_points: int = PLOT_CHART_MAX_POINTS,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Downsample the visual chart series into at most ``max_points`` buckets.

    Aggregates each series' per-point query counts by summing within a
    bucket and keeps a representative timestamp per bucket, so the visual
    chart stays compact while totals are preserved. Full-resolution data is
    not retained in the rendered chart.

    Callers that also *report* the interval count report the upstream
    ``len(times)``, not the downsampled length: the chart was drawn with
    fewer points, but the series the caller asked for still had all of them.
    """
    n = len(times)
    if n <= max_points:
        return times, series_data
    bucket_size = math.ceil(n / max_points)
    new_times = [times[i] for i in range(0, n, bucket_size)]
    new_series = [_bucket_single_series(s, n, bucket_size) for s in series_data]
    return new_times, new_series


def current_time() -> float:
    """Return the current Unix time in seconds (indirection keeps tests deterministic)."""
    return time.time()


def _relative_time_seconds(text: str, now: float) -> float | None:
    """Resolve ``now`` and relative offsets (``-1d``, ``+2h``) to Unix seconds.

    Total: an offset with a huge digit count (``-9`` + 400 digits + ``d``) is
    syntactically a relative offset but cannot be added to a float - the
    unresolvable-None path covers it, same as an unparseable one. Past
    CPython's 4300-digit integer-string limit (issue #544) ``int()`` itself
    raises ``ValueError`` on the same kind of input, and the same None is
    the answer.
    """
    if text.lower() == "now":
        return now
    match = _RELATIVE_TIME_RE.match(text)
    if not match:
        return None
    sign = -1.0 if match.group(1) else 1.0
    try:
        return now + sign * int(match.group(2)) * _RELATIVE_UNIT_SECONDS[match.group(3)]
    except (ValueError, OverflowError):
        return None


def _absolute_or_iso_seconds(text: str) -> float | None:
    """A bare Unix-seconds number, else an ISO 8601 instant, else None.

    Total over the caller's input domain, not just the well-formed part of it:
    ``float()`` on an integer past the float range and ``datetime.timestamp()``
    on an unrepresentable instant raise ``OverflowError`` (or ``OSError`` on
    some platforms), and an unresolvable value is exactly what the None means
    here - the caller reports an unresolved range through the typed error, so
    an extreme value must not escape as a crash. (Reproduced: a 400-digit
    numeric ``from``.)
    """
    try:
        return float(int(text))
    except (ValueError, OverflowError):
        pass
    try:
        return _parse_series_timestamp(text).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _numeric_seconds(value: float) -> float | None:
    """A finite float for a numeric time point, or None when it cannot be one.

    Non-finite floats (JSON ``Infinity``/``NaN`` survive Python's json decoder)
    resolve to nothing: they bound no range, and letting them through would
    put them in the budget arithmetic and the error text. A Python int past
    the float range (JSON carries arbitrary integers) raises OverflowError on
    the conversion - same unresolvable answer.
    """
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def resolve_time_point(value: str | int, now: float) -> float | None:
    """Resolve a ``from``/``to`` argument to Unix seconds, or None if unparseable.

    Accepts the forms the analytics endpoints accept: Unix timestamps, ISO 8601
    timestamps, ``now``, and relative offsets such as ``-1d`` or ``-7d``. A
    relative offset's unit letter is read case-sensitively, and an ISO 8601
    value with no timezone is read as UTC. An unparseable value yields None so
    the caller can reject the request instead of forwarding a range whose size
    it cannot bound.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _numeric_seconds(value)
    text = str(value).strip()
    relative = _relative_time_seconds(text, now)
    if relative is not None:
        return relative
    return _absolute_or_iso_seconds(text)


def _format_resolved(seconds: float | None) -> str:
    """Render a resolved time point so an error can name the range it computed.

    An error that only reports the span leaves the caller guessing which of its
    two arguments was wrong: a model that passed from_time=-86400 (a bare
    number, so an absolute Unix timestamp in seconds, i.e. 1969) retried three
    different intervals before anything said so.

    Total: ``fromtimestamp`` refuses values outside the platform's range with
    OverflowError/OSError, and this runs *while reporting* such a refusal - a
    reporter that crashes on its own subject replaces one bad timestamp with
    an internal error. Out-of-range values are named as what they are.
    """
    if seconds is None:
        return "unresolved"
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return f"out of range ({seconds!r})"


def _unresolved_range_error(from_time: str | int, to_time: str | int) -> dict[str, Any]:
    """Typed error for a range whose endpoints do not resolve to instants."""
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        (
            f"Could not interpret the requested time range ({from_time!r} to {to_time!r}); "
            "use a Unix timestamp, an ISO 8601 timestamp, 'now', or a relative offset such as '-7d'"
        ),
        from_time=from_time,
        to_time=to_time,
    )


def _empty_range_error(from_time: str | int, to_time: str | int) -> dict[str, Any]:
    """Typed error for a range that does not end after it starts."""
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        f"Requested time range must end after it starts, got {from_time!r} to {to_time!r}",
        from_time=from_time,
        to_time=to_time,
    )


def _budget_exceeded_error(start: float, end: float, span: float, interval: int) -> dict[str, Any]:
    """Typed error naming the point budget the resolved range blows and what to change."""
    max_span = PLOT_MAX_POINTS * interval
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        (
            f"Requested range spans {span:.0f}s "
            f"({_format_resolved(start)} to {_format_resolved(end)}), more than the {PLOT_MAX_POINTS}-point "
            f"series budget allows at interval={interval}s (at this interval the span must stay under "
            f"{max_span}s to answer at most {PLOT_MAX_POINTS} points); use a larger interval or a "
            "shorter range. A bare number is an absolute Unix timestamp in seconds - a relative range "
            "needs a unit, such as '-1d'"
        ),
        max_points=PLOT_MAX_POINTS,
        max_span_seconds=max_span,
        interval=interval,
        resolved_from=_format_resolved(start),
        resolved_to=_format_resolved(end),
    )


def _range_bounds_or_error(
    from_time: str | int, to_time: str | int, now: float
) -> tuple[float, float] | dict[str, Any]:
    """The resolved ``(start, end)`` of a readable, ordered window, or the typed error for one that is not.

    The check fails closed: a range that does not resolve to a positive span is
    rejected, because an unbounded request is exactly what the budget below
    exists to prevent.
    """
    start = resolve_time_point(from_time, now)
    end = resolve_time_point(to_time, now)
    if start is None or end is None:
        return _unresolved_range_error(from_time, to_time)
    if end <= start:
        return _empty_range_error(from_time, to_time)
    return start, end


def analytics_range_error(
    from_time: str | int,
    to_time: str | int,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Typed error for a ``from``/``to`` window that cannot be read or does not end after it starts.

    Every analytics call owes this much, series or not: an unparseable window
    reaches upstream as a value the API reads differently, where it answers 200
    with no rows or a 400 that names neither argument, so the caller has nothing
    to correct. The point budget in ``series_budget_error`` is the stricter
    check only a ``;series`` call can breach - a non-series endpoint answers
    top-N rows, so its response is bounded by ``limit``, not by the span.
    """
    bounds = _range_bounds_or_error(from_time, to_time, current_time() if now is None else now)
    return bounds if isinstance(bounds, dict) else None


def series_budget_error(
    from_time: str | int,
    to_time: str | int,
    interval: int,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return a typed error when the series response would exceed the point budget.

    The API returns one point per interval across the requested range, so a wide
    range with a small interval asks for hundreds of thousands of points, all of
    which ``_api_request`` buffers and ``_render_series_chart`` then plots. The
    budget is enforced where the series request is constructed - as a rejection
    naming the limit, never as a silent substitution of the caller's interval,
    which would return a different series than the one asked for (issue #267).

    The budget boundary is exact, not approximate: a series answers one point
    per interval *including both endpoints*, so a span of ``n * interval`` is
    ``n + 1`` points. A span must stay strictly under
    ``PLOT_MAX_POINTS * interval`` for the answer to fit inside the
    ``PLOT_MAX_POINTS`` cap - the old ``<=`` admitted exactly the span that
    answers 2001 points while naming a 2000-point budget.
    """
    bounds = _range_bounds_or_error(from_time, to_time, current_time() if now is None else now)
    if isinstance(bounds, dict):
        return bounds
    start, end = bounds
    span = end - start
    if span < PLOT_MAX_POINTS * interval:
        return None
    return _budget_exceeded_error(start, end, span, interval)


def validate_series_payload(payload: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Extract the time axis and data rows from a time-series payload.

    Nulls are normalized with ``or`` rather than ``dict.get(key, default)``:
    the default only applies when a key is *absent*, so a JSON ``null`` from
    the API would otherwise reach ``.get()`` and raise ``AttributeError``
    (issue #286). Any other unexpected shape raises and is normalized to an
    ``internal_error`` payload by the caller, like every other failure path.
    """
    meta = payload.get("meta") or {}
    series_meta = meta.get("series") or {}
    return series_meta.get("times") or [], payload.get("data") or []
