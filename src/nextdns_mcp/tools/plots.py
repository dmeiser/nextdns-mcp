"""Analytics plotting tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import asyncio
import io
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

import mcp.types
from fastmcp.utilities.types import Image

from ..coercion import OptionalProfileId
from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import (
    NextDNSAuthError,
    NextDNSError,
    NextDNSRateLimitError,
    NextDNSServerError,
    _api_request,
    _build_series_params,
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
    for key in ("name", "status", "protocol", "version", "id"):
        value = series.get(key)
        if value is not None and value != "":
            return str(value)
    if "validated" in series:
        return "validated" if series["validated"] else "not_validated"
    if "encrypted" in series:
        return "encrypted" if series["encrypted"] else "unencrypted"
    return f"series_{index}"


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
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                # Anchored to UTC below, so the naive result is intentional.
                parsed = datetime.strptime(value, fmt)  # noqa: DTZ007
                break
            except ValueError:
                continue
        else:
            raise
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
        return float(value)
    text = str(value).strip()
    if text.lower() == "now":
        return now
    match = _RELATIVE_TIME_RE.match(text)
    if match:
        sign = -1.0 if match.group(1) else 1.0
        return now + sign * int(match.group(2)) * _RELATIVE_UNIT_SECONDS[match.group(3)]
    try:
        return float(int(text))
    except ValueError:
        pass
    try:
        return _parse_series_timestamp(text).timestamp()
    except ValueError:
        return None


def _series_span_seconds(from_time: str | int, to_time: str | int, now: float) -> float | None:
    """Return the length in seconds of the requested range, or None if unknown.

    None means the range could not be resolved because one of the two values is
    unparseable. A span at or below zero (an end not after the start) is
    returned as computed, so the caller can distinguish an unresolvable range
    from a nonsensical one.
    """
    start = _resolve_time_point(from_time, now)
    end = _resolve_time_point(to_time, now)
    if start is None or end is None:
        return None
    return end - start


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
    span = _series_span_seconds(from_time, to_time, _now() if now is None else now)
    if span is None:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            (
                f"Could not interpret the requested time range ({from_time!r} to {to_time!r}); "
                "use a Unix timestamp, an ISO 8601 timestamp, 'now', or a relative offset such as '-7d'"
            ),
            from_time=from_time,
            to_time=to_time,
        )
    if span <= 0:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"Requested time range must end after it starts, got {from_time!r} to {to_time!r}",
            from_time=from_time,
            to_time=to_time,
        )
    max_span = PLOT_MAX_POINTS * interval
    if span <= max_span:
        return None
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        (
            f"Requested range spans {span:.0f}s, more than the {PLOT_MAX_POINTS}-point series budget "
            f"allows at interval={interval}s (max {max_span}s); use a larger interval or a shorter range"
        ),
        max_points=PLOT_MAX_POINTS,
        max_span_seconds=max_span,
        interval=interval,
    )


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

    if interval < 60:
        return None, error_payload(
            ErrorCode.INVALID_ARGUMENT,
            "interval must be at least 60 seconds",
            minimum_interval=60,
        )

    if interval > PLOT_INTERVAL_MAX:
        return None, error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"interval must not exceed {PLOT_INTERVAL_MAX} seconds, got {interval}",
            max_interval=PLOT_INTERVAL_MAX,
            interval=interval,
        )

    if limit > PLOT_LIMIT_MAX:
        return None, error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"limit must not exceed {PLOT_LIMIT_MAX}, got {limit}",
            max_limit=PLOT_LIMIT_MAX,
            limit=limit,
        )

    budget_error = _series_budget_error(from_time, to_time, interval)
    if budget_error:
        return None, budget_error

    return resolve_profile_id(profile_id, allow_default=True)


async def _fetch_series_payload(
    url: str,
    params: dict[str, Any],
    metric: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch time-series payload from the API through the shared wrapper.

    Goes through ``_api_request`` (not the raw client) so plotting shares the
    same error handling, logging, and any future retry/rate-limit/telemetry
    behavior as every other tool (issue #183). Failures surface as the
    wrapper's standardized error payloads, so no behavior changes for callers.
    """
    try:
        payload = await _api_request("GET", url, params=params)
    except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
        cause = e.__cause__
        if cause is not None and isinstance(cause, httpx.HTTPError):
            return None, http_error_payload(str(e), cause)
        else:
            return None, error_payload(ErrorCode.INTERNAL_ERROR, str(e))
    if "error" in payload:
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

    url = f"/profiles/{target_profile}/analytics/{metric};series"
    logger.info(f"Plotting analytics series: {metric} for profile {target_profile}")

    payload, fetch_error = await _fetch_series_payload(url, params, metric)
    if fetch_error:
        return fetch_error
    assert payload is not None

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
        - ``plotAnalytics(metric="status", profile_id="abc123", from_time="-1d")``
        - ``plotAnalytics(metric="devices", profile_id="abc123", from_time="-7d", interval=86400)``
        - a year of data: ``plotAnalytics(metric="status", profile_id="abc123", from_time="-1y", interval=43200)``

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
