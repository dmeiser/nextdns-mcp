"""Grouped analytics query tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any

from ..coercion import ProfileId
import httpx

from ..errors import ErrorCode, error_payload, http_error_payload
from ..utils import (
    NextDNSAuthError,
    NextDNSError,
    NextDNSRateLimitError,
    NextDNSServerError,
    _api_request,
    _build_query_params,
    _build_series_params,
    _cap_limit,
    resolve_profile_id,
)
from .metrics import NON_SERIES_METRICS, AnalyticsMetric

# Server-side cap for the ``limit`` parameter (maximum accepted by the NextDNS API).
ANALYTICS_LIMIT_MAX = 500


async def _query_analytics_impl(
    metric: AnalyticsMetric,
    profile_id: ProfileId,
    from_time: str | int | None = None,
    to_time: str | int | None = None,
    interval: int | None = None,
    alignment: str | None = None,
    timezone: str | None = None,
    partials: str | None = None,
    limit: int | None = None,
    destination_type: str | None = None,
    series: bool = False,
    cursor: str | None = None,
    device: str | None = None,
    status: str | None = None,
    root: bool | None = None,
) -> dict[str, Any]:
    """Grouped implementation for NextDNS analytics endpoints."""
    target_profile, error = resolve_profile_id(profile_id, allow_default=False)
    if error:
        return error
    assert target_profile is not None

    if series and metric in NON_SERIES_METRICS:
        return error_payload(
            ErrorCode.UNSUPPORTED_PARAMETER,
            f"series=true is not supported for the '{metric}' metric",
            metric=metric,
            series=series,
        )

    suffix = ";series" if series else ""
    url = f"/profiles/{target_profile}/analytics/{metric}{suffix}"

    capped_limit = _cap_limit(limit, ANALYTICS_LIMIT_MAX)
    params: dict[str, Any] = _build_query_params(
        **{"from": from_time, "to": to_time, "limit": capped_limit, "cursor": cursor, "device": device}
    )

    if series:
        # Same ``;series`` endpoint as plotAnalytics: build the shared
        # parameter set through the one helper so the two paths cannot drift.
        # from/to are already in ``params`` from the base call above.
        params.update(
            _build_series_params(
                interval=interval,
                alignment=alignment,
                timezone=timezone,
                partials=partials,
            )
        )

    if metric == "destinations":
        if not destination_type:
            return error_payload(
                ErrorCode.MISSING_REQUIRED_ARGUMENT, "destination_type is required for destinations metric"
            )
        params["type"] = destination_type

    if metric == "domains":
        params.update(_build_query_params(status=status, root=root))

    try:
            return await _api_request("GET", url, params=params)
    except (NextDNSError, NextDNSAuthError, NextDNSRateLimitError, NextDNSServerError) as e:
        cause = e.__cause__
        if cause is not None and isinstance(cause, httpx.HTTPError):
            return http_error_payload(str(e), cause)
        else:
            return error_payload(ErrorCode.INTERNAL_ERROR, str(e))


async def queryAnalytics(
    metric: AnalyticsMetric,
    profile_id: ProfileId,
    from_time: str | int | None = None,
    to_time: str | int | None = None,
    interval: int | None = None,
    alignment: str | None = None,
    timezone: str | None = None,
    partials: str | None = None,
    limit: int | None = None,
    destination_type: str | None = None,
    series: bool = False,
    cursor: str | None = None,
    device: str | None = None,
    status: str | None = None,
    root: bool | None = None,
) -> dict[str, Any]:
    """Query NextDNS analytics metrics.

    Metrics:
        - ``status``: Query resolution status (default, blocked, allowed, relayed).
        - ``domains``: Top domains; supports the ``status`` and ``root`` filters.
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

    ``limit`` is clamped server-side to the range 1-500 (the range accepted by
    the NextDNS API): values above 500 are reduced to 500 and non-positive
    values to 1. The clamp is silent, so ask for at most 500.

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
        metric=metric,
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        interval=interval,
        alignment=alignment,
        timezone=timezone,
        partials=partials,
        limit=limit,
        destination_type=destination_type,
        series=series,
        cursor=cursor,
        device=device,
        status=status,
        root=root,
    )
