"""Analytics plotting tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import asyncio
import io
import logging
import math
import re
import time
from datetime import UTC, datetime
from typing import Any

import mcp.types
from fastmcp.utilities.types import Image

from ..coercion import OptionalProfileId
from ..errors import ErrorCode, error_payload
from ..transport import _api_request_payload
from ..utils import (
    PARTIALS_VALUES,
    _build_series_params,
    normalize_partials,
    normalize_time_value,
    resolve_profile_id,
)
from .metrics import PLOT_METRICS, PlotMetric

logger = logging.getLogger(__name__)

# Server-side ceiling on the ``interval`` parameter, and the maximum number of
# data points a single series response may contain (issue #267). The point count
# is the requested range divided by the interval, so bounding the count - not
# the interval argument alone - is what keeps a series response small enough to
# buffer and then render. Both are enforced as typed rejections naming the limit,
# never by silently rewriting the caller's request: the ``;series`` endpoints
# take no ``limit`` parameter, so the range is the only exposure knob.
PLOT_INTERVAL_MAX = 86400
PLOT_MAX_POINTS = 2000

# Ceiling on the tool's published ``limit`` parameter, matching the sibling
# tools' caps. The ``;series`` endpoints consume no limit, so the value is
# validated but never forwarded; an over-cap value is rejected rather than
# silently clamped, so a caller is never told less than the tool enforced.
PLOT_LIMIT_MAX = 500

# Relative from/to values such as "-1d" or "-7d", with their unit in seconds.
# "y" is a 365-day year, matching how the analytics endpoints read a year. The
# unit is matched case-sensitively: an upper-case letter is a different unit
# upstream ("-1M" reads as months), so folding it onto its lower-case twin
# would size a months-long range as minutes.
_RELATIVE_TIME_RE = re.compile(r"^(-?)(\d+)([smhdwy])$")
_RELATIVE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}

# matplotlib is imported lazily inside _render_series_chart (issue #165): the
# import alone costs ~2s and every stdio cold start would pay it even though
# only the plot tool needs it. These module-level slots are filled once on the
# first render and are only accessed from within _render_series_chart, which
# fills them under the same lazy-import guard.
mdates: Any = None
Figure: Any = None
FigureCanvasAgg: Any = None


def _extract_series_label(series: dict[str, Any], index: int) -> str:
    """Return a human-readable label for a time-series data entry."""
    for key, value in ((key, series.get(key)) for key in ("name", "status", "protocol", "version", "id")):
        if value is not None and value != "":
            return str(value)
    return _series_boolean_label(series, index)


def _series_boolean_label(series: dict[str, Any], index: int) -> str:
    """Label a status-shaped entry by its boolean flag, or by position when it has neither."""
    if "validated" in series:
        return "validated" if series["validated"] else "not_validated"
    if "encrypted" in series:
        return "encrypted" if series["encrypted"] else "unencrypted"
    return f"series_{index}"


def _strptime_fallback(value: str) -> datetime:
    """Parse via the ordered strptime fallback list; re-raises the original ValueError."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            # Anchored to UTC below, so the naive result is intentional.
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparsable timestamp: {value!r}")


def _parse_series_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp returned by the NextDNS API.

    Tries ``datetime.fromisoformat`` first (which, on the supported
    Python >=3.12, already handles ``'Z'`` suffixes, offsets without colons,
    and microsecond fractions of any length). A small ordered list of
    ``strptime`` formats is tried in turn as a fallback, so timestamps that
    ``fromisoformat`` rejects - including whole-second timestamps without a
    timezone - still parse instead of raising an unhandled ``ValueError``.

    A value carrying no timezone is anchored to UTC, so the same string always
    denotes the same instant no matter which machine resolves it.

    Raises:
        ValueError: If the value matches none of the supported formats.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = _strptime_fallback(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _render_series_chart(
    metric: str,
    times: list[str],
    series_data: list[dict[str, Any]],
) -> bytes:
    """Render a PNG line chart from time-series data and return the raw bytes.

    Uses the object-oriented Figure API rather than pyplot global state, so
    concurrent renders from multiple threads cannot interfere with each other.
    The figure is cleared in a finally block so a rendering exception cannot
    leak figure state in the long-running server.
    """
    # Lazy imports: importing matplotlib costs ~2s, so the plot path is the only
    # place that may pull it in (issue #165). Force the headless Agg backend
    # before pyplot is first imported: pyplot resolves the backend at import
    # time, so setting it later is version-fragile. FigureCanvasAgg keeps the
    # rendering fully headless even if another backend is active.
    global mdates, Figure, FigureCanvasAgg
    if Figure is None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as _mdates
        from matplotlib.backends.backend_agg import FigureCanvasAgg as _FigureCanvasAgg
        from matplotlib.figure import Figure as _Figure

        mdates = _mdates
        Figure = _Figure
        FigureCanvasAgg = _FigureCanvasAgg

    parsed_times = [_parse_series_timestamp(t) for t in times]
    numeric_times = mdates.date2num(parsed_times)

    fig = Figure(figsize=(10, 6))
    FigureCanvasAgg(fig)
    buffer = io.BytesIO()
    try:
        ax = fig.add_subplot(111)
        for index, series in enumerate(series_data):
            label = _extract_series_label(series, index)
            queries = series.get("queries", [])
            ax.plot(numeric_times, queries, label=label, marker="o", markersize=3)

        ax.set_title(f"NextDNS Analytics: {metric}")
        ax.set_xlabel("Time")
        ax.set_ylabel("Queries")
        ax.legend()
        ax.tick_params(axis="x", rotation=30)
        fig.autofmt_xdate()

        fig.savefig(buffer, format="png", bbox_inches="tight")
    finally:
        fig.clear()

    buffer.seek(0)
    return buffer.getvalue()


def _now() -> float:
    """Return the current Unix time in seconds (indirection keeps tests deterministic)."""
    return time.time()


def _relative_time_seconds(text: str, now: float) -> float | None:
    """Resolve ``now`` and relative offsets (``-1d``, ``+2h``) to Unix seconds.

    Total: an offset with a huge digit count (``-9`` + 400 digits + ``d``) is
    syntactically a relative offset but cannot be added to a float - the
    unresolvable-None path covers it, same as an unparseable one.
    """
    if text.lower() == "now":
        return now
    match = _RELATIVE_TIME_RE.match(text)
    if not match:
        return None
    sign = -1.0 if match.group(1) else 1.0
    try:
        return now + sign * int(match.group(2)) * _RELATIVE_UNIT_SECONDS[match.group(3)]
    except OverflowError:
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


def _resolve_time_point(value: str | int, now: float) -> float | None:
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


def _span_budget_error(
    start: float, end: float, from_time: str | int, to_time: str | int, interval: int
) -> dict[str, Any] | None:
    """Typed error for a resolved span that is empty or blows the point budget.

    The budget boundary is exact, not approximate: a series answers one point
    per interval *including both endpoints*, so a span of ``n * interval`` is
    ``n + 1`` points. A span must stay strictly under
    ``PLOT_MAX_POINTS * interval`` for the answer to fit inside the
    ``PLOT_MAX_POINTS`` cap - the old ``<=`` admitted exactly the span that
    answers 2001 points while naming a 2000-point budget.
    """
    span = end - start
    if span <= 0:
        return _empty_range_error(from_time, to_time)
    if span < PLOT_MAX_POINTS * interval:
        return None
    return _budget_exceeded_error(start, end, span, interval)


def _series_budget_error(
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

    The check fails closed: a range that does not resolve to a positive span is
    rejected, because an unbounded request is exactly what the budget exists to
    prevent.
    """
    resolved_now = _now() if now is None else now
    start, end = _resolve_time_point(from_time, resolved_now), _resolve_time_point(to_time, resolved_now)
    if start is None or end is None:
        return _unresolved_range_error(from_time, to_time)
    return _span_budget_error(start, end, from_time, to_time, interval)


def _validate_interval_and_limit(interval: int, limit: int) -> dict[str, Any] | None:
    """The two bounds a caller can overshoot the same way: interval and limit."""
    if interval < 60:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            "interval must be at least 60 seconds",
            minimum_interval=60,
        )

    if interval > PLOT_INTERVAL_MAX:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"interval must not exceed {PLOT_INTERVAL_MAX} seconds, got {interval}",
            max_interval=PLOT_INTERVAL_MAX,
            interval=interval,
        )

    if limit > PLOT_LIMIT_MAX:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"limit must not exceed {PLOT_LIMIT_MAX}, got {limit}",
            max_limit=PLOT_LIMIT_MAX,
            limit=limit,
        )
    return None


def _validate_plot_params(
    metric: str,
    interval: int,
    profile_id: OptionalProfileId,
    from_time: str | int = "-1d",
    to_time: str | int = "now",
    limit: int = 10,
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate metric, interval, limit, range, and profile ID parameters for plotting."""
    if metric not in PLOT_METRICS:
        return None, error_payload(
            ErrorCode.UNSUPPORTED_METRIC,
            f"Unsupported metric: {metric}",
            supported_metrics=sorted(PLOT_METRICS),
        )

    bounds_error = _validate_interval_and_limit(interval, limit)
    if bounds_error:
        return None, bounds_error

    budget_error = _series_budget_error(from_time, to_time, interval)
    if budget_error:
        return None, budget_error

    return resolve_profile_id(profile_id, allow_default=True)


async def _fetch_series_payload(
    url: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch a time-series payload through the shared payload wrapper.

    Goes through ``transport._api_request_payload`` - the one place that
    turns raised ``NextDNSError`` into standardized error payloads, returns
    the ACL layer's denial payload, and adds the profile/entry hint to a 404
    (issue #183). Plotting keeping its own raise/except wrapper around the
    raw ``_api_request`` was a second copy of that contract, and a copy that
    silently lacked the 404 enrichment.

    The wrapper returns every failure - raised or returned - as a payload,
    so the classification below is the whole check: a typed failure carries
    a ``code`` and an ``error`` and never a ``data`` key; a 2xx body that
    merely happens to carry an ``error`` key is data.
    """
    from ..spec_tools import MAX_SERIES_RESPONSE_BYTES

    payload = await _api_request_payload("GET", url, params=params, max_response_bytes=MAX_SERIES_RESPONSE_BYTES)
    if isinstance(payload, dict) and "code" in payload and "error" in payload and "data" not in payload:
        return None, payload
    return payload, None


def _validate_series_payload(payload: dict[str, Any]) -> tuple[list[Any], list[Any]]:
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


async def _plot_analytics_series_impl(
    metric: str,
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-1d",
    to_time: str | int = "now",
    interval: int = 3600,
    alignment: str = "end",
    timezone: str = "GMT",
    partials: str = "none",
    limit: int = 10,
) -> dict[str, Any] | mcp.types.ImageContent:
    """Generate a PNG line chart from a NextDNS analytics time-series endpoint."""
    # Read the caller's range before bounding it: a bare negative number means
    # an offset, not 1969 (see utils.normalize_time_value).
    from_time = normalize_time_value(from_time)
    to_time = normalize_time_value(to_time)
    partials, partials_error = _validate_partials(partials)
    if partials_error:
        return partials_error

    target_profile, val_error = _validate_plot_params(metric, interval, profile_id, from_time, to_time, limit)
    if val_error:
        return val_error
    if target_profile is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution failed")

    params: dict[str, Any] = _build_series_params(
        from_time=from_time,
        to_time=to_time,
        interval=interval,
        alignment=alignment,
        timezone=timezone,
        partials=partials,
    )

    return await _fetch_plot_series(metric, target_profile, params)


def _validate_partials(partials: str) -> tuple[Any, dict[str, Any] | None]:
    """Map ``partials`` through normalize_partials and check the endpoint accepts it."""
    partials = normalize_partials(partials)
    if partials is not None and partials not in PARTIALS_VALUES:
        return partials, error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"partials must be one of: {', '.join(PARTIALS_VALUES)}; got {partials!r}",
            allowed=list(PARTIALS_VALUES),
            partials=partials,
        )
    return partials, None


async def _fetch_plot_series(
    metric: str, target_profile: str, params: dict[str, Any]
) -> dict[str, Any] | mcp.types.ImageContent:
    """Fetch the metric's series for the profile and plot it, or return the fetch error."""
    url = f"/profiles/{target_profile}/analytics/{metric};series"
    logger.info(f"Plotting analytics series: {metric} for profile {target_profile}")

    payload, fetch_error = await _fetch_series_payload(url, params)
    if fetch_error:
        return fetch_error
    if payload is None:
        # The fetcher promises a payload whenever it reports no error; if that
        # ever breaks, say so as the typed internal error rather than asserting
        # (python -O strips asserts; a stripped assert turns into a TypeError
        # on None several frames later, far from the truth).
        return error_payload(ErrorCode.INTERNAL_ERROR, f"{metric} series fetch returned no payload or error")
    return await _plot_rendered_series(metric, target_profile, payload)


async def _plot_rendered_series(
    metric: str, target_profile: str, payload: dict[str, Any]
) -> dict[str, Any] | mcp.types.ImageContent:
    """Validate the fetched payload has plottable data, and render the PNG off-loop.

    The try mirrors the combined implementation's: a payload whose structure
    cannot be unpacked is reported as INTERNAL_ERROR.
    """
    try:
        times, series_data = _validate_series_payload(payload)

        if not times or not series_data:
            return error_payload(
                ErrorCode.NO_DATA,
                "No time-series data available to plot",
                metric=metric,
                profile_id=target_profile,
            )

        png_bytes = await asyncio.to_thread(_render_series_chart, metric, times, series_data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error rendering chart for {metric}: {e}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Error rendering chart: {e}")

    return Image(data=png_bytes, format="png").to_image_content()


async def plotAnalytics(
    metric: PlotMetric,
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-1d",
    to_time: str | int = "now",
    interval: int = 3600,
    alignment: str = "end",
    timezone: str = "GMT",
    partials: str = "none",
    limit: int = 10,
) -> dict[str, Any] | mcp.types.ImageContent:
    """Generate a PNG line chart for a NextDNS analytics time-series metric.

    Use this to visualize query trends over time. The profile should have recent
    query history; otherwise the tool returns an error explaining that no data is
    available.

    This tool names its window arguments ``from_time``/``to_time``; the generated
    ``getAnalytics*Series`` tools name the same window ``from``/``to``. Sending
    ``from``/``to`` here does not set the window: they are not in this tool's
    published schema, the server drops them, and the drop is reported on the
    result (``ignored_arguments``, or a text note beside the image).

    Supported metrics: ``status``, ``devices``, ``protocols``, ``queryTypes``,
    ``ipVersions``, ``dnssec``, ``encryption``, ``reasons``, ``ips``.

    Time values can be Unix timestamps or relative strings like ``-1d``.

    The ``;series`` endpoint takes no ``limit`` parameter: ``limit`` is part of
    this tool's published surface, is rejected above 500 rather than clamped,
    and is never forwarded, so the requested range is the only thing that bounds
    the response. Three server-side limits are enforced before the request is
    built, and each is rejected with an ``invalid_argument`` error naming the
    limit rather than silently adjusted:

    - ``interval`` must be between 60 and 86400 seconds.
    - ``limit`` must not exceed 500.
    - The range must be interpretable (a Unix timestamp, an ISO 8601
      timestamp, ``now``, or a relative offset such as ``-7d``; a relative
      unit letter is read case-sensitively and a timestamp with no timezone is
      read as UTC), must end after it starts, and spans at most 2000 intervals
      (at most 2000 points per series), so a wide range needs a proportionally
      larger ``interval``.

    Examples:
        - ``plotAnalytics(metric="status", profile_id="<profile id>", from_time="-1d")``
        - ``plotAnalytics(metric="devices", profile_id="<profile id>", from_time="-7d", interval=86400)``
        - a year of data: ``plotAnalytics(metric="status", profile_id="<profile id>", from_time="-1y", interval=43200)``

    Returns:
        An MCP ImageContent PNG chart, or an error dict if data is unavailable.
    """
    return await _plot_analytics_series_impl(
        metric=metric,
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        interval=interval,
        alignment=alignment,
        timezone=timezone,
        partials=partials,
        limit=limit,
    )
