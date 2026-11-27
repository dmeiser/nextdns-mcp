"""Analytics plotting tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
from typing import Any, NamedTuple

from fastmcp.tools.base import ToolResult
from prefab_ui.app import PrefabApp
from prefab_ui.components import Column, Heading, Metric, Row

from ..chart_ui import (
    DASHBOARD_THEME,
    bounded_app_result,
    build_chart_series,
    build_timeseries_area_chart,
)
from ..charts import (
    build_chart_data,
    cap_chart_series,
    downsample_chart_series,
    extract_series_fields,
    series_budget_error,
)
from ..coercion import OptionalProfileId
from ..constants import escape_control_chars
from ..errors import ErrorCode, error_payload
from ..transport import MAX_SERIES_RESPONSE_BYTES, fetch_analytics_payload
from ..utils import (
    PARTIALS_VALUES,
    build_series_params,
    is_usable_integer,
    limit_range_error,
    normalize_partials,
    normalize_time_value,
    resolve_default_profile,
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


def _build_series_chart(
    metric: str,
    times: list[str],
    series_data: list[dict[str, Any]],
) -> ToolResult | dict[str, Any]:
    """Build an interactive Prefab chart view and return a ToolResult with summary.

    The rendered rows are downsampled to the shared chart budget, but the
    reported interval count is the upstream ``len(times)``: the chart was drawn
    with fewer points, yet the series the caller asked for - and paid the range
    budget for - still carried all of them (issue #550).

    The series count is capped by the sibling budget before those rows are built:
    a ``;series`` answer carries one series per grouped value (per device, per
    protocol), and each one costs a value in every chart row, so an uncapped M
    materialized a dense 250 x M document before anything was drawn. The busiest
    series are kept and the rest are summed interval by interval into one
    ``Other`` series, so the plotted totals still match the summary (issue #789).
    """
    total_queries = sum(sum(series.get("queries") or []) for series in series_data)
    series_data = cap_chart_series(series_data)
    chart_times, chart_series_data = downsample_chart_series(times, series_data)
    chart_data = build_chart_data(chart_times, chart_series_data)
    chart_series = build_chart_series(chart_series_data)

    with Column(gap=4, css_class="p-4") as view:
        Heading(f"NextDNS Analytics: {metric}", level=2)
        with Row(gap=4):
            Metric(label="Total Queries", value=f"{total_queries:,}")
            Metric(label="Data Points", value=str(len(times)))
            Metric(label="Series Count", value=str(len(chart_series_data)))
        build_timeseries_area_chart(chart_data, chart_series)

    summary = f"NextDNS Analytics: {metric} ({total_queries:,} total queries across {len(times)} intervals)"
    return bounded_app_result(PrefabApp(view=view, theme=DASHBOARD_THEME), summary, "analytics chart")


def _validate_interval_and_limit(interval: int, limit: int) -> dict[str, Any] | None:
    """The two bounds a caller can overshoot the same way: interval and limit.

    ``interval`` is type-checked before the band is read: ``float("nan")``
    compares False against both 60 and 86400, and a fractional interval is not
    a window the ``;series`` endpoint can answer, so neither survives a bare
    range check from a direct Python caller (issue #759). The band itself is
    unchanged and stays inclusive.
    """
    if not is_usable_integer(interval):
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"interval must be an integer number of seconds, got {interval}",
            minimum_interval=60,
            max_interval=PLOT_INTERVAL_MAX,
            interval=interval,
        )

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

    return limit_range_error(limit)


class _PlotParams(NamedTuple):
    """Parameters that passed every plot validation, ready for the series fetch.

    The explicit ``error`` field is always ``None`` here; it exists so the
    caller inspects one named field instead of unpacking positions.
    """

    profile_id: str
    error: None = None


class _PlotParamsError(NamedTuple):
    """The typed payload that stopped plot parameter validation."""

    error: dict[str, Any]
    profile_id: None = None


def _validate_plot_params(
    metric: str,
    interval: int,
    profile_id: OptionalProfileId,
    from_time: str | int = "-1d",
    to_time: str | int = "now",
    limit: int = 10,
) -> _PlotParams | _PlotParamsError:
    """Validate metric, interval, limit, range, and profile ID parameters for plotting."""
    if metric not in PLOT_METRICS:
        return _PlotParamsError(
            error_payload(
                ErrorCode.UNSUPPORTED_METRIC,
                f"Unsupported metric: {metric}",
                supported_metrics=sorted(PLOT_METRICS),
            )
        )

    bounds_error = _validate_interval_and_limit(interval, limit)
    if bounds_error:
        return _PlotParamsError(bounds_error)

    budget_error = series_budget_error(from_time, to_time, interval)
    if budget_error:
        return _PlotParamsError(budget_error)

    resolved = resolve_default_profile(profile_id)
    if resolved[1] is not None:
        return _PlotParamsError(resolved[1])
    return _PlotParams(profile_id=resolved[0])


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
    partials, partials_error = _normalize_and_validate_partials(partials)
    if partials_error:
        return partials_error

    validated = _validate_plot_params(metric, interval, profile_id, from_time, to_time, limit)
    if isinstance(validated, _PlotParamsError):
        return validated.error

    params: dict[str, Any] = build_series_params(
        from_time=from_time,
        to_time=to_time,
        interval=interval,
        alignment=alignment,
        timezone=timezone,
        partials=partials,
    )

    return await _fetch_plot_series(metric, validated.profile_id, params)


def _normalize_and_validate_partials(partials: str) -> tuple[Any, dict[str, Any] | None]:
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
    logger.info(
        "Plotting analytics series: %s for profile %s",
        escape_control_chars(metric),
        escape_control_chars(target_profile),
    )

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
        times, series_data = extract_series_fields(payload)

        if not times or not series_data:
            return error_payload(
                ErrorCode.NO_DATA,
                "No time-series data available to plot",
                metric=metric,
                profile_id=target_profile,
            )

        return _build_series_chart(metric, times, series_data)
    except (ValueError, TypeError, KeyError, AttributeError):
        logger.error("Error rendering chart")
        return error_payload(ErrorCode.INTERNAL_ERROR, "Error rendering chart")


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
    this tool's published surface, is rejected outside ``1..500`` rather than
    clamped, and is never forwarded, so the requested range is the only thing
    that bounds the response. Three server-side limits are enforced before the
    request is built, and each is rejected with an ``invalid_argument`` error
    naming the limit rather than silently adjusted:

    - ``interval`` must be between 60 and 86400 seconds.
    - ``limit`` must be between 1 and 500.
    - The range must be interpretable (a Unix timestamp, an ISO 8601
      timestamp, ``now``, or a relative offset such as ``-7d``; a relative
      unit letter is read case-sensitively and a timestamp with no timezone is
      read as UTC), must end after it starts, and spans at most 2000 intervals
      (at most 2000 points per series), so a wide range needs a proportionally
      larger ``interval``.

    What is plotted is bounded independently of what is fetched: the chart draws
    at most 250 downsampled intervals and at most 25 series - the busiest ones,
    with every omitted series summed interval by interval into one ``Other``
    series, so the chart still accounts for the query totals the summary reports.
    A rendered chart over the 262144-byte result ceiling is refused with a
    ``response_too_large`` payload naming its size rather than delivered trimmed.

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
