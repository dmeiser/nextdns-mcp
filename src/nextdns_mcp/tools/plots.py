"""Analytics plotting tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import asyncio
import io
import logging
from datetime import datetime
from typing import Any

import mcp.types
from fastmcp.utilities.types import Image

from ..coercion import OptionalProfileId
from ..errors import ErrorCode, error_payload
from ..utils import _api_request, _build_series_params, _cap_limit, resolve_profile_id
from .metrics import PLOT_METRICS, PlotMetric

logger = logging.getLogger(__name__)

# Server-side caps for the ``limit`` and ``interval`` parameters of the
# ``;series`` endpoint (issue #267). The plot path forwards both verbatim, so
# without these a single call could ask the API for an unbounded number of
# points per series, every one of which is buffered and then rendered.
PLOT_LIMIT_MAX = 500
PLOT_INTERVAL_MAX = 86400

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

    Raises:
        ValueError: If the value matches none of the supported formats.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                # Naive results are intentional: they mirror fromisoformat,
                # which also leaves timezone-less strings naive.
                return datetime.strptime(value, fmt)  # noqa: DTZ007
            except ValueError:
                continue
        raise


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


def _validate_plot_params(
    metric: str,
    interval: int,
    profile_id: OptionalProfileId,
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate metric, interval, and profile ID parameters for plotting."""
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
    payload = await _api_request("GET", url, params=params)
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
    target_profile, val_error = _validate_plot_params(metric, interval, profile_id)
    if val_error:
        return val_error
    assert target_profile is not None

    params: dict[str, Any] = _build_series_params(
        from_time=from_time,
        to_time=to_time,
        interval=_cap_limit(interval, PLOT_INTERVAL_MAX),
        alignment=alignment,
        timezone=timezone,
        partials=partials,
        limit=_cap_limit(limit, PLOT_LIMIT_MAX),
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

    Examples:
        - ``plotAnalytics(metric="status", profile_id="abc123", from_time="-1d")``
        - ``plotAnalytics(metric="devices", profile_id="abc123", from_time="-7d", interval=86400)``

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
