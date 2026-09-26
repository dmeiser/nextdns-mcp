"""Analytics plotting tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import asyncio
import io
import logging
from datetime import datetime
from typing import Any, Literal

import httpx
import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import mcp.types
from fastmcp.utilities.types import Image

from .. import client
from ..coercion import OptionalProfileId
from ..config import get_default_profile
from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import _validate_profile_id

matplotlib.use("Agg")

logger = logging.getLogger(__name__)

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
PlotMetric = Literal[
    "status",
    "devices",
    "protocols",
    "queryTypes",
    "ipVersions",
    "dnssec",
    "encryption",
    "reasons",
    "ips",
]

# Metrics supported by the analytics time-series plotting tools.
_PLOT_ANALYTICS_METRICS = frozenset(
    {
        "status",
        "devices",
        "protocols",
        "queryTypes",
        "ipVersions",
        "dnssec",
        "encryption",
        "reasons",
        "ips",
    }
)


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
    """Parse an ISO 8601 timestamp returned by the NextDNS API."""
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S.%f%z")


def _render_series_chart(
    metric: str,
    times: list[str],
    series_data: list[dict[str, Any]],
) -> bytes:
    """Render a PNG line chart from time-series data and return the raw bytes."""
    parsed_times = [_parse_series_timestamp(t) for t in times]
    numeric_times = mdates.date2num(parsed_times)

    fig, ax = plt.subplots(figsize=(10, 6))
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

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


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
    if metric not in _PLOT_ANALYTICS_METRICS:
        return error_payload(
            ErrorCode.UNSUPPORTED_METRIC,
            f"Unsupported metric: {metric}",
            supported_metrics=sorted(_PLOT_ANALYTICS_METRICS),
        )

    if interval < 60:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            "interval must be at least 60 seconds",
            minimum_interval=60,
        )

    target_profile = profile_id if profile_id else get_default_profile()
    if not target_profile:
        return error_payload(
            ErrorCode.MISSING_PROFILE_ID,
            "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
            hint="Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
        )

    error = _validate_profile_id(target_profile)
    if error:
        return error

    params: dict[str, Any] = {
        "from": from_time,
        "to": to_time,
        "interval": interval,
        "alignment": alignment,
        "timezone": timezone,
        "partials": partials,
        "limit": limit,
    }

    url = f"/profiles/{target_profile}/analytics/{metric};series"
    logger.info(f"Plotting analytics series: {metric} for profile {target_profile}")

    try:
        response = await client.api_client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as e:
        logger.error(f"HTTP error while fetching analytics series {metric}: {e}")
        return http_error_payload(
            f"HTTP error while fetching analytics series {metric}: {e}", e, fallback_code=ErrorCode.HTTP_ERROR
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error while fetching analytics series {metric}: {e}")
        return error_payload(
            ErrorCode.INTERNAL_ERROR,
            f"Unexpected error while fetching analytics series {metric}: {e}",
        )

    meta = payload.get("meta", {})
    series_meta = meta.get("series", {})
    times = series_meta.get("times", [])
    series_data = payload.get("data", [])

    if not times or not series_data:
        return error_payload(
            ErrorCode.NO_DATA,
            "No time-series data available to plot",
            metric=metric,
            profile_id=target_profile,
        )

    try:
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
