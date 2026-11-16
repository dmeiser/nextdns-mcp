"""Analytics plotting tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import math
from typing import Any

from fastmcp.tools.base import ToolResult
from prefab_ui.app import PrefabApp
from prefab_ui.components import Column, Heading, Metric, Row

from ..charts import (
    DASHBOARD_THEME,
    build_chart_data,
    build_chart_series,
    build_timeseries_area_chart,
    series_budget_error,
    validate_series_payload,
)
from ..coercion import OptionalProfileId
from ..errors import ErrorCode, error_payload
from ..transport import MAX_SERIES_RESPONSE_BYTES, fetch_analytics_payload
from ..utils import (
    PARTIALS_VALUES,
    _build_series_params,
    normalize_partials,
    normalize_time_value,
    resolve_profile_id,
)
from .metrics import PLOT_METRICS, PlotMetric

logger = logging.getLogger(__name__)

# Server-side ceiling on the ``interval`` parameter (issue #267). Its partner, the
# maximum number of data points one series response may carry, sits with the shared
# series primitives in ``nextdns_mcp.charts`` because the dashboard prices the same
# budget. Both are enforced as typed rejections naming the limit, never by silently
# rewriting the caller's request: the ``;series`` endpoints take no ``limit``
# parameter, so the range is the only exposure knob.
PLOT_INTERVAL_MAX = 86400

# Ceiling on the tool's published ``limit`` parameter, matching the sibling
# tools' caps. The ``;series`` endpoints consume no limit, so the value is
# validated but never forwarded; an over-cap value is rejected rather than
# silently clamped, so a caller is never told less than the tool enforced.
PLOT_LIMIT_MAX = 500

# Visual cap for the rendered chart series: even when the series
# budget allows up to PLOT_MAX_POINTS points, embedding that many raw
# point dicts in structured_content bloats MCP/LLM client contexts. When a
# series exceeds this, the visual chart is downsampled into buckets.
PLOT_CHART_MAX_POINTS = 250


def _bucket_series_queries(queries: list[int] | None, n: int, bucket_size: int) -> list[int]:
    """Sum a single series' query counts into chunks of ``bucket_size``."""
    q = queries or []
    return [sum(q[start : min(start + bucket_size, n)]) for start in range(0, n, bucket_size)]


def _bucket_single_series(series: dict[str, Any], n: int, bucket_size: int) -> dict[str, Any]:
    """Return a copy of ``series`` with its query counts bucketed."""
    updated = dict(series)
    updated["queries"] = _bucket_series_queries(series.get("queries"), n, bucket_size)
    return updated


def _downsample_chart_series(
    times: list[str],
    series_data: list[dict[str, Any]],
    max_points: int = PLOT_CHART_MAX_POINTS,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Downsample the visual chart series into at most ``max_points`` buckets.

    Aggregates each series' per-point query counts by summing within a
    bucket and keeps a representative timestamp per bucket, so the visual
    chart stays compact while totals are preserved. Full-resolution data is
    not retained in the rendered chart.
    """
    n = len(times)
    if n <= max_points:
        return times, series_data
    bucket_size = math.ceil(n / max_points)
    new_times = [times[i] for i in range(0, n, bucket_size)]
    new_series = [_bucket_single_series(s, n, bucket_size) for s in series_data]
    return new_times, new_series


def _build_series_chart(
    metric: str,
    times: list[str],
    series_data: list[dict[str, Any]],
) -> ToolResult:
    """Build an interactive Prefab chart view and return a ToolResult with summary."""
    total_queries = sum(sum(series.get("queries") or []) for series in series_data)
    chart_times, chart_series_data = _downsample_chart_series(times, series_data)
    chart_data = build_chart_data(chart_times, chart_series_data)
    chart_series = build_chart_series(chart_series_data)

    with Column(gap=4, css_class="p-4") as view:
        Heading(f"NextDNS Analytics: {metric}", level=2)
        with Row(gap=4):
            Metric(label="Total Queries", value=f"{total_queries:,}")
            Metric(label="Data Points", value=str(len(chart_times)))
            Metric(label="Series Count", value=str(len(chart_series_data)))
        build_timeseries_area_chart(chart_data, chart_series)

    summary = f"NextDNS Analytics: {metric} ({total_queries:,} total queries across {len(chart_times)} intervals)"
    app = PrefabApp(view=view, theme=DASHBOARD_THEME)
    return ToolResult(content=summary, structured_content=app)


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

    if limit < 1:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"limit must be at least 1, got {limit}",
            min_limit=1,
            limit=limit,
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

    budget_error = series_budget_error(from_time, to_time, interval)
    if budget_error:
        return None, budget_error

    return resolve_profile_id(profile_id, allow_default=True)


async def _fetch_series_payload(
    url: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch a time-series payload through the shared payload wrapper.

    Goes through ``transport.fetch_analytics_payload``, which awaits
    ``transport._api_request_payload`` - the one place that turns raised
    ``NextDNSError`` into standardized error payloads, returns the ACL layer's
    denial payload, and adds the profile/entry hint to a 404 (issue #183).
    Plotting keeping its own raise/except wrapper around the raw
    ``_api_request`` was a second copy of that contract, and a copy that
    silently lacked the 404 enrichment; keeping its own copy of the payload
    classification was a second copy too, which is why the classification now
    lives beside the wrapper (issue #421).

    A series response is capped at ``MAX_SERIES_RESPONSE_BYTES`` while
    streaming; the cap stays here because it is what a series endpoint is.
    """
    return await fetch_analytics_payload(url, params, max_response_bytes=MAX_SERIES_RESPONSE_BYTES)


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
) -> dict[str, Any] | ToolResult:
    """Generate an interactive chart from a NextDNS analytics time-series endpoint."""
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


async def _fetch_plot_series(metric: str, target_profile: str, params: dict[str, Any]) -> dict[str, Any] | ToolResult:
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
    return _plot_rendered_series(metric, target_profile, payload)


def _plot_rendered_series(metric: str, target_profile: str, payload: dict[str, Any]) -> dict[str, Any] | ToolResult:
    """Validate the fetched payload has plottable data, and build the interactive chart.

    The try mirrors the combined implementation's: a payload whose structure
    cannot be unpacked is reported as INTERNAL_ERROR.
    """
    try:
        times, series_data = validate_series_payload(payload)

        if not times or not series_data:
            return error_payload(
                ErrorCode.NO_DATA,
                "No time-series data available to plot",
                metric=metric,
                profile_id=target_profile,
            )

        return _build_series_chart(metric, times, series_data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error rendering chart for {metric}: {e}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Error rendering chart: {e}")


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
) -> dict[str, Any] | ToolResult:
    """Generate an interactive area chart for a NextDNS analytics time-series metric.

    Use this to visualize query trends over time. The profile should have recent
    query history; otherwise the tool returns an error explaining that no data is
    available.

    This tool names its window arguments ``from_time``/``to_time``; the generated
    ``getAnalytics*Series`` tools name the same window ``from``/``to``. Sending
    ``from``/``to`` here does not set the window: they are not in this tool's
    published schema, the server drops them, and the drop is reported on the
    result (``ignored_arguments``, or a text note beside the chart).

    Supported metrics: ``status``, ``devices``, ``protocols``, ``queryTypes``,
    ``ipVersions``, ``dnssec``, ``encryption``, ``reasons``, ``ips``.

    Time values can be Unix timestamps or relative strings like ``-1d``.

    Default window: ``from_time="-1d"`` to ``to_time="now"`` (1 day).

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
        A FastMCP ToolResult containing an interactive Prefab chart, or an error dict if data is unavailable.
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
