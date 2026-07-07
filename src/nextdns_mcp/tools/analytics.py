"""Grouped analytics query tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal, Optional

from ..coercion import ProfileId
from ..utils import _api_request, _build_query_params, _validate_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
AnalyticsMetric = Literal[
    "status",
    "domains",
    "queryTypes",
    "reasons",
    "ips",
    "dnssec",
    "encryption",
    "ipVersions",
    "protocols",
    "devices",
    "destinations",
]


async def _query_analytics_impl(
    metric: AnalyticsMetric,
    profile_id: ProfileId,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    interval: Optional[int] = None,
    alignment: Optional[str] = None,
    timezone: Optional[str] = None,
    partials: Optional[str] = None,
    limit: Optional[int] = None,
    destination_type: Optional[str] = None,
    series: bool = False,
    cursor: Optional[str] = None,
    device: Optional[str] = None,
    status: Optional[str] = None,
    root: Optional[bool] = None,
) -> dict[str, Any]:
    """Grouped implementation for NextDNS analytics endpoints."""
    error = _validate_profile_id(profile_id)
    if error:
        return error

    if series and metric == "domains":
        raise ValueError("series=true is not supported for the 'domains' metric")

    suffix = ";series" if series else ""
    url = f"/profiles/{profile_id}/analytics/{metric}{suffix}"

    params: dict[str, Any] = _build_query_params(
        **{"from": from_time, "to": to_time, "limit": limit, "cursor": cursor, "device": device}
    )

    if series:
        params.update(
            _build_query_params(
                interval=interval,
                alignment=alignment,
                timezone=timezone,
                partials=partials,
            )
        )

    if metric == "destinations":
        if not destination_type:
            return {"error": "destination_type is required for destinations metric"}
        params["type"] = destination_type

    if metric == "domains":
        params.update(_build_query_params(status=status, root=root))

    return await _api_request("GET", url, params=params)


async def queryAnalytics(
    metric: AnalyticsMetric,
    profile_id: ProfileId,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    interval: Optional[int] = None,
    alignment: Optional[str] = None,
    timezone: Optional[str] = None,
    partials: Optional[str] = None,
    limit: Optional[int] = None,
    destination_type: Optional[str] = None,
    series: bool = False,
    cursor: Optional[str] = None,
    device: Optional[str] = None,
    status: Optional[str] = None,
    root: Optional[bool] = None,
) -> dict[str, Any]:
    """Query NextDNS analytics metrics.

    Metrics:
        - ``status``: Query resolution status (default, blocked, allowed, relayed).
        - ``devices``: Queries per device.
        - ``protocols``: DNS transport protocol (DoH, DoT, Do53 UDP/TCP, DoQ).
        - ``queryTypes``: DNS record types requested (A, AAAA, CNAME, etc.).
        - ``ipVersions``: IPv4 vs IPv6 queries.
        - ``dnssec``: DNSSEC validation results.
        - ``encryption``: Encrypted vs unencrypted queries.
        - ``reasons``: Why queries were blocked or allowed.
        - ``ips``: Top source IPs.
        - ``destinations``: Top destinations; requires ``destination_type`` such as
          ``countries`` or ``gafam``.

    Set ``series=true`` to fetch time-series data instead of aggregate totals.
    Time values can be Unix timestamps or relative strings like ``-1d``.
    Note: ``series=true`` is not supported when ``metric="domains"``.

    Optional filters:
        - ``cursor``: Pagination cursor from a previous response.
        - ``device``: Filter analytics to a single device id.
        - ``status``: For the ``domains`` metric, filter by resolution status.
        - ``root``: For the ``domains`` metric, group results by root domain (boolean).

    Examples:
        - totals: ``queryAnalytics(metric="status", profile_id="abc123", from_time="-1d")``
        - time series: ``queryAnalytics(metric="status", profile_id="abc123", from_time="-1d", series=true)``
        - destinations: ``queryAnalytics(metric="destinations", profile_id="abc123", from_time="-1d", destination_type="countries")``
    """
    return await _query_analytics_impl(
        metric,
        profile_id,
        from_time,
        to_time,
        interval,
        alignment,
        timezone,
        partials,
        limit,
        destination_type,
        series,
        cursor,
        device,
        status,
        root,
    )
