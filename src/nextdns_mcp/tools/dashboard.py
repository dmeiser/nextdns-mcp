"""Interactive analytics dashboard and specialized analytics UI tools for NextDNS.

SPDX-License-Identifier: MIT
"""

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any, Literal, NamedTuple, Protocol, get_args

from fastmcp.tools.base import ToolResult
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
    Column,
    DataTable,
    DataTableColumn,
    Heading,
    Progress,
    Row,
    Text,
)
from prefab_ui.components.charts import AreaChart, BarChart, ChartSeries, PieChart

from ..coercion import OptionalProfileId
from ..errors import ErrorCode, error_payload
from ..transport import MAX_SERIES_RESPONSE_BYTES, fetch_analytics_payload
from ..utils import (
    _build_query_params,
    _build_series_params,
    normalize_time_value,
    resolve_profile_id,
)
from .plots import (
    DASHBOARD_THEME,
    _build_chart_data,
    _build_chart_series,
    _resolve_time_point,
    _series_budget_error,
    _validate_series_payload,
)

logger = logging.getLogger(__name__)


# Reusable table columns shared across dashboard builders.
QUERIES_COLUMN = DataTableColumn(key="queries", header="Queries", sortable=True, format="number")
SHARE_COLUMN = DataTableColumn(key="share", header="Share", sortable=True)


def _time_params(from_time: str | int | None, to_time: str | int | None) -> dict[str, Any]:
    """Extract normalized time window parameters."""
    return _build_query_params(
        **{
            "from": normalize_time_value(from_time),
            "to": normalize_time_value(to_time),
        }
    )


def _build_analytics_params(
    from_time: str | int | None = "-30d",
    to_time: str | int | None = "now",
    limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build normalized query parameters for analytics endpoints."""
    return _build_query_params(
        **{
            "from": normalize_time_value(from_time),
            "to": normalize_time_value(to_time),
            "limit": limit,
            **extra,
        }
    )


async def _fetch_analytics_payload(
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics endpoint payload through the access-controlled transport.

    The fetch and the typed-error classification are ``transport.fetch_analytics_payload``
    - shared with ``plots._fetch_series_payload`` so the error contract cannot be
    copied twice and drift. What stays here is the decision only a dashboard
    endpoint can make: a ``;series`` response is capped at
    ``MAX_SERIES_RESPONSE_BYTES``, the bounded analytics endpoints are not.
    """
    url = f"/profiles/{profile_id}/analytics/{endpoint}"
    max_bytes = MAX_SERIES_RESPONSE_BYTES if ";series" in endpoint else None
    return await fetch_analytics_payload(url, params, max_response_bytes=max_bytes)


def _format_share(count: int, total: int) -> str:
    """Format a query count as a percentage share of the total."""
    return f"{(count / total * 100):.1f}%" if total > 0 else "0.0%"


def _item_queries(item: dict[str, Any]) -> int:
    """Extract integer query count from an item dict."""
    try:
        return int(item.get("queries") or 0)
    except (ValueError, TypeError):
        return 0


def _blocked_queries(item: dict[str, Any]) -> int:
    """Extract blocked query count for a status item."""
    return _item_queries(item) if item.get("status") == "blocked" else 0


def _calculate_status_kpis(data: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary KPIs from status totals."""
    total = sum(_item_queries(item) for item in data)
    blocked = sum(_blocked_queries(item) for item in data)
    rate = (blocked / total * 100.0) if total > 0 else 0.0
    return {"total": total, "blocked": blocked, "rate": rate}


def _build_kpi_row(kpis: dict[str, Any]) -> Row:
    """Build the three top KPI metric cards with centered and aligned styling."""
    with Row(gap=6, css_class="w-full") as row:
        with Card(css_class="flex-1 text-center py-6"):
            Text(f"{kpis['total']:,}", css_class="text-3xl font-bold")
            Text("queries", css_class="text-sm text-muted-foreground mt-1")
        with Card(css_class="flex-1 text-center py-6"):
            Text(f"{kpis['blocked']:,}", css_class="text-3xl font-bold text-red-500")
            Text("blocked queries", css_class="text-sm text-muted-foreground mt-1")
        with Card(css_class="flex-1 text-center py-6"):
            Text(f"{kpis['rate']:.2f}%", css_class="text-3xl font-bold")
            Text("of queries blocked", css_class="text-sm text-muted-foreground mt-1")
    return row


def _build_queries_series_card(series_payload: dict[str, Any]) -> Card | None:
    """Build the queries evolution chart card from a time-series payload."""
    try:
        times, series_data = _validate_series_payload(series_payload)
        if not times or not series_data:
            return None
        chart_data = _build_chart_data(times, series_data)
        chart_series = _build_chart_series(series_data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Error preparing queries series data: {exc}")
        return None

    with Card(css_class="w-full") as card:
        with CardHeader():
            CardTitle("Queries")
            CardDescription("Evolution of queries over time.")
        with CardContent():
            AreaChart(
                data=chart_data,
                series=chart_series,
                xAxis="time",
                curve="smooth",
                showDots=False,
                height=260,
                showLegend=True,
                showTooltip=True,
                showGrid=True,
            )
    return card


def _bar_chart(chart_data: list[dict[str, Any]], label: str, color: str = "#3b82f6") -> BarChart:
    """Build a horizontal bar chart with rounded pill bars."""
    return BarChart(
        data=chart_data,
        series=[ChartSeries(dataKey="queries", label=label, color=color)],
        xAxis="label",
        horizontal=True,
        barRadius=6,
        showLegend=True,
        showTooltip=True,
        showGrid=True,
    )


def _pie_chart(chart_data: list[dict[str, Any]]) -> PieChart:
    """Build a donut pie chart with slice labels, spacing, and tooltips."""
    return PieChart(
        data=chart_data,
        dataKey="queries",
        nameKey="label",
        innerRadius=60,
        paddingAngle=3,
        showLegend=True,
        showTooltip=True,
        aggregateBelow=0.03,
    )


def _domain_label(item: dict[str, Any], key_name: str = "domain") -> str:
    """Extract domain label."""
    return str(item.get(key_name) or item.get("domain") or "-")


def _domain_table(data: list[dict[str, Any]], key_name: str) -> DataTable:
    """Build domain data table."""
    rows: list[dict[str, Any]] = [
        {"domain": _domain_label(item, key_name), "queries": _item_queries(item)} for item in data
    ]
    return DataTable(
        columns=[
            DataTableColumn(key="domain", header="Domain", sortable=True),
            QUERIES_COLUMN,
        ],
        rows=rows,  # type: ignore[arg-type]
    )


def _render_chart_view(chart_data: list[dict[str, Any]], title: str, color: str, view_type: str) -> None:
    """Render either a pie chart or horizontal bar chart."""
    if view_type == "pie":
        _pie_chart(chart_data)
    else:
        _bar_chart(chart_data, title, color)


def _render_domain_content(title: str, data: list[dict[str, Any]], key_name: str, view_type: str, color: str) -> None:
    """Render domain card content based on chosen visualization type."""
    if not data:
        Text(f"No {title.lower()} available for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _domain_table(data, key_name)
    else:
        chart_data = [{"label": _domain_label(item, key_name), "queries": _item_queries(item)} for item in data]
        _render_chart_view(chart_data, title, color, view_type)


def _build_analytics_card(title: str, description: str, render_content) -> Card:
    """Build a generic analytics card shell around rendered content."""
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle(title)
            CardDescription(description)
        with CardContent():
            render_content()
    return card


def _build_domains_card(
    title: str,
    description: str,
    data: list[dict[str, Any]],
    key_name: str = "domain",
    view_type: str = "table",
    color: str = "#3b82f6",
) -> Card:
    """Build a domain listing card with a sortable data table or bar chart."""
    return _build_analytics_card(
        title,
        description,
        lambda: _render_domain_content(title, data, key_name, view_type, color),
    )


def _reason_label(item: dict[str, Any]) -> str:
    """Extract reason or blocklist name."""
    return str(item.get("name") or item.get("id") or "Unknown")


def _reason_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one reasons-table row."""
    q = _item_queries(item)
    return {
        "name": _reason_label(item),
        "queries": q,
        "share": _format_share(q, total),
    }


def _reasons_table(data: list[dict[str, Any]]) -> DataTable:
    """Build reasons data table."""
    total = sum(_item_queries(item) for item in data)
    rows: list[dict[str, Any]] = [_reason_row(item, total) for item in data]
    return DataTable(
        columns=[
            DataTableColumn(key="name", header="Reason / Filter", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows=rows,  # type: ignore[arg-type]
    )


def _build_reasons_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the reasons for blocking card."""

    def _render():
        if not data:
            Text("No queries were blocked during this period.", css_class="text-sm text-muted-foreground py-4")
        elif view_type == "table":
            _reasons_table(data)
        else:
            chart_data = [{"label": _reason_label(item), "queries": _item_queries(item)} for item in data]
            if view_type == "pie":
                _pie_chart(chart_data)
            else:
                _bar_chart(chart_data, "Blocked Queries", "#ef4444")

    return _build_analytics_card(
        "Reasons for Blocking",
        "Security, Privacy and/or Parental Control settings that blocked the most queries.",
        _render,
    )


def _device_name(item: dict[str, Any]) -> str:
    """Extract a human-readable device name."""
    dev_id = str(item.get("id") or "-")
    return "Unidentified" if dev_id == "__UNIDENTIFIED__" else str(item.get("name") or dev_id)


def _device_label(item: dict[str, Any]) -> str:
    """Extract human-readable device name and model."""
    dev_name = _device_name(item)
    model = str(item.get("model") or "")
    if model and model != "-" and model != dev_name:
        return f"{dev_name} ({model})"
    return dev_name


def _device_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one device row."""
    q = _item_queries(item)
    return {
        "device": _device_label(item),
        "model": str(item.get("model") or "-"),
        "queries": q,
        "share": _format_share(q, total),
    }


def _devices_table(data: list[dict[str, Any]]) -> DataTable:
    """Build devices data table."""
    total = sum(_item_queries(item) for item in data)
    rows: list[dict[str, Any]] = [_device_row(item, total) for item in data]
    return DataTable(
        columns=[
            DataTableColumn(key="device", header="Device", sortable=True),
            DataTableColumn(key="model", header="Model", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows=rows,  # type: ignore[arg-type]
    )


def _render_device_content(data: list[dict[str, Any]], view_type: str) -> None:
    """Render devices card content based on chosen visualization type."""
    if not data:
        Text("No device queries recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _devices_table(data)
    else:
        chart_data = [{"label": _device_label(item), "queries": _item_queries(item)} for item in data]
        _render_chart_view(chart_data, "Queries", "#3b82f6", view_type)


def _build_devices_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the devices card."""
    return _build_analytics_card(
        "Devices",
        "Devices making the queries.",
        lambda: _render_device_content(data, view_type),
    )


def _location_string(geo: dict[str, Any]) -> str:
    """Format city and country into a readable location string."""
    city = geo.get("city")
    country = geo.get("country")
    if city and country:
        return f"{city}, {country}"
    return str(city or country or "-")


def _ip_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one IP address row."""
    geo = item.get("geo") or {}
    network = item.get("network") or {}
    return {
        "ip": str(item.get("ip") or "-"),
        "isp": str(network.get("isp") or "-"),
        "location": _location_string(geo),
        "queries": _item_queries(item),
    }


def _build_ips_card(data: list[dict[str, Any]]) -> Card:
    """Build the IP addresses card."""
    rows: list[dict[str, Any]] = [_ip_row(item) for item in data]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("IPs")
            CardDescription("IP addresses making the queries.")
        with CardContent():
            if not data:
                Text("No IP queries recorded for this period.", css_class="text-sm text-muted-foreground py-4")
            else:
                DataTable(
                    columns=[
                        DataTableColumn(key="ip", header="IP Address", sortable=True),
                        DataTableColumn(key="isp", header="ISP", sortable=True),
                        DataTableColumn(key="location", header="Location", sortable=True),
                        QUERIES_COLUMN,
                    ],
                    rows=rows,  # type: ignore[arg-type]
                )
    return card


def _single_gafam_row(item: dict[str, Any], total: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format single company entry for GAFAM donut chart and table."""
    name = str(item.get("company") or "others").capitalize()
    q = _item_queries(item)
    return {"label": name, "company": name, "queries": q}, {
        "company": name,
        "queries": q,
        "share": _format_share(q, total),
    }


def _gafam_rows(data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Format data rows for the GAFAM donut chart and data table."""
    total = sum(_item_queries(item) for item in data)
    pairs = [_single_gafam_row(item, total) for item in data]
    table_rows: list[dict[str, Any]] = [p[1] for p in pairs]
    return [p[0] for p in pairs], table_rows


def _gafam_table(table_rows: list[dict[str, Any]]) -> DataTable:
    """Build GAFAM table."""
    return DataTable(
        columns=[
            DataTableColumn(key="company", header="Company", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows=table_rows,  # type: ignore[arg-type]
    )


def _build_gafam_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the GAFAM dominance donut chart and table card."""
    chart_data, table_rows = _gafam_rows(data)
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("GAFAM Dominance")
            CardDescription(
                "Traffic share of dominant internet companies (Google, Apple, Facebook, Amazon, Microsoft)."
            )
        with CardContent():
            if not data:
                Text("No GAFAM traffic recorded for this period.", css_class="text-sm text-muted-foreground py-4")
            elif view_type == "bar":
                _bar_chart(chart_data, "Queries", "#3b82f6")
            elif view_type == "table":
                _gafam_table(table_rows)
            else:
                with Column(gap=4):
                    _pie_chart(chart_data)
                    _gafam_table(table_rows)
    return card


def _matching_queries(item: dict[str, Any], flag_key: str) -> int:
    """Return queries when flag condition is satisfied."""
    return _item_queries(item) if item.get(flag_key) is True else 0


def _calc_rate(data: list[dict[str, Any]], flag_key: str) -> tuple[int, int, float]:
    """Calculate (verified_count, total_count, percentage) for boolean-shaped analytics."""
    total = sum(_item_queries(item) for item in data)
    matching = sum(_matching_queries(item, flag_key) for item in data)
    pct = (matching / total * 100.0) if total > 0 else 0.0
    return matching, total, pct


def _build_security_section(title: str, val: int, total: int, pct: float) -> Column:
    """Build a progress-bar section for one security indicator."""
    with Column(gap=2) as col:
        Text(f"{title}: {pct:.2f}%", css_class="font-semibold")
        Progress(value=pct)
        Text(f"{val:,} / {total:,} queries", css_class="text-sm text-muted-foreground")
    return col


def _build_security_card(
    encryption_data: list[dict[str, Any]],
    dnssec_data: list[dict[str, Any]],
) -> Card:
    """Build the security card for encrypted DNS and DNSSEC rates."""
    enc_val, enc_total, enc_pct = _calc_rate(encryption_data, "encrypted")
    dnssec_val, dnssec_total, dnssec_pct = _calc_rate(dnssec_data, "validated")

    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("Security & Privacy")
            CardDescription("Encryption and DNSSEC validation rates.")
        with CardContent(), Column(gap=4):
            _build_security_section("Encrypted DNS", enc_val, enc_total, enc_pct)
            _build_security_section("DNSSEC Validation", dnssec_val, dnssec_total, dnssec_pct)
    return card


def _destination_label(item: dict[str, Any]) -> str:
    """Format country destination name."""
    return str(item.get("name") or item.get("code") or "-")


def _accumulate_domains(domains: list[str] | None, budget: int = 60) -> str:
    """Accumulate domain labels until the display budget is reached.

    Returns a comma-separated string of domains whose total length with separators
    does not exceed *budget*. If the first domain alone exceeds the budget, return a
    truncated prefix of that domain. The function stops reading further domains once the
    budget would be exceeded, avoiding a full join of the input list.
    """
    if not domains:
        return ""
    labels: list[str] = []
    first = str(domains[0])
    if len(first) > budget:
        return first[:budget]
    labels.append(first)
    used = len(first)
    for domain in domains[1:]:
        domain = str(domain)
        need = used + 2 + len(domain)
        if need > budget:
            break
        labels.append(domain)
        used = need
    return ", ".join(labels)


def _destination_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one country destination row."""
    sample = _accumulate_domains(item.get("domains"), budget=60)
    return {
        "country": _destination_label(item),
        "code": str(item.get("code") or "-"),
        "queries": _item_queries(item),
        "sample_domains": sample or "-",
    }


def _destinations_table(data: list[dict[str, Any]]) -> DataTable:
    """Build destinations table."""
    rows: list[dict[str, Any]] = [_destination_row(item) for item in data]
    return DataTable(
        columns=[
            DataTableColumn(key="country", header="Country", sortable=True),
            DataTableColumn(key="code", header="Code", sortable=True),
            QUERIES_COLUMN,
            DataTableColumn(key="sample_domains", header="Sample Domains"),
        ],
        rows=rows,  # type: ignore[arg-type]
    )


def _render_destination_content(data: list[dict[str, Any]], view_type: str) -> None:
    """Render destination countries card content based on chosen visualization type."""
    if not data:
        Text("No country traffic recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _destinations_table(data)
    else:
        chart_data = [{"label": _destination_label(item), "queries": _item_queries(item)} for item in data]
        _render_chart_view(chart_data, "Queries", "#3b82f6", view_type)


def _build_destinations_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the destination countries card."""
    return _build_analytics_card(
        "Traffic Destination",
        "Top destination countries based on DNS queries.",
        lambda: _render_destination_content(data, view_type),
    )


# ==============================================================================
# Implementation functions
# ==============================================================================


async def _resolve_profile(profile_id: OptionalProfileId) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve target profile ID or return typed error payload."""
    target_profile, val_error = resolve_profile_id(profile_id, allow_default=True)
    if val_error:
        return None, val_error
    if target_profile is None:
        return None, error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution failed")
    return target_profile, None


def _payload_data(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract list data rows from an API payload dictionary."""
    return (payload or {}).get("data") or []


def _overview_interval(from_time: str | int, to_time: str | int) -> int:
    """Pick interval based on span: 3600 for <= 2 days, 86400 for longer."""
    now = time.time()
    start = _resolve_time_point(from_time, now)
    end = _resolve_time_point(to_time, now)
    if start is not None and end is not None:
        span = end - start
        if 0 < span <= 172800:
            return 3600
    return 86400


def _validate_card_data(
    payload: dict[str, Any] | None, endpoint: str, pid: str
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Extract list data from payload or return typed no-data error."""
    data = _payload_data(payload)
    if payload is None:
        return None, error_payload(ErrorCode.NO_DATA, f"No {endpoint} data available", profile_id=pid)
    return data, None


async def _fetch_card_data(
    profile_id: OptionalProfileId,
    endpoint: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int | None = None,
    **extra: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Fetch single endpoint data list with profile validation and error classification."""
    pid, err = await _resolve_profile(profile_id)
    if err or pid is None:
        return None, err
    params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, **extra)
    payload, fetch_err = await _fetch_analytics_payload(pid, endpoint, params)
    if fetch_err:
        return None, fetch_err
    data, no_data_err = _validate_card_data(payload, endpoint, pid)
    return data, no_data_err


def _wrap_card_result(card: Card, summary_text: str) -> ToolResult:
    """Wrap a single visual card into a ToolResult."""
    with Column(gap=4, css_class="p-6", children=[card]) as view:
        pass
    app = PrefabApp(view=view, theme=DASHBOARD_THEME)
    return ToolResult(content=summary_text, structured_content=app)


OVERVIEW_FANOUT_CONCURRENCY = 2


async def _bounded_fetch_analytics_payload(
    semaphore: asyncio.Semaphore,
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics payload while bounding concurrent upstream fan-out."""
    async with semaphore:
        return await _fetch_analytics_payload(profile_id, endpoint, params)


def _first_error(*errs: Any) -> dict[str, Any] | None:
    """Return the first non-None error tuple from a gather result."""
    for err in errs:
        if err:
            return err
    return None


async def _fetch_overview_payloads(
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch all datasets required for the main overview cards."""
    from_time = normalize_time_value(from_time)
    to_time = normalize_time_value(to_time)
    interval = _overview_interval(from_time, to_time)
    budget_err = _series_budget_error(from_time, to_time, interval)
    if budget_err:
        return {}, {}, [], [], budget_err

    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="default")
    blk_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="blocked")
    ser_params = _build_series_params(from_time=from_time, to_time=to_time, interval=interval)

    sem = asyncio.Semaphore(OVERVIEW_FANOUT_CONCURRENCY)
    status_task = _bounded_fetch_analytics_payload(sem, pid, "status", params)
    series_task = _bounded_fetch_analytics_payload(sem, pid, "status;series", ser_params)
    resolved_task = _bounded_fetch_analytics_payload(sem, pid, "domains", dom_params)
    blocked_task = _bounded_fetch_analytics_payload(sem, pid, "domains", blk_params)

    (
        (status_payload, status_err),
        (series_payload, series_err),
        (resolved_payload, resolved_err),
        (blocked_payload, blocked_err),
    ) = await asyncio.gather(status_task, series_task, resolved_task, blocked_task)
    err = _first_error(status_err, series_err, resolved_err, blocked_err)
    if err:
        return {}, {}, [], [], err
    return (
        (status_payload or {}),
        (series_payload or {}),
        _payload_data(resolved_payload),
        _payload_data(blocked_payload),
        None,
    )


def _assemble_overview_layout(
    kpis: dict[str, Any],
    series_payload: dict[str, Any],
    resolved_data: list[dict[str, Any]],
    blocked_data: list[dict[str, Any]],
    domain_view: str = "table",
) -> Column:
    """Build the main overview cards layout."""
    with Column(gap=6, css_class="p-6") as view:
        Heading("NextDNS Overview Dashboard", level=1)
        _build_kpi_row(kpis)
        _build_queries_series_card(series_payload)
        with Row(gap=4):
            _build_domains_card(
                "Resolved Domains",
                "Domains that resolved without being blocked.",
                resolved_data,
                view_type=domain_view,
                color="#3b82f6",
            )
            _build_domains_card(
                "Blocked Domains",
                "Domains blocked by security, privacy, or parental controls.",
                blocked_data,
                view_type=domain_view,
                color="#ef4444",
            )
    return view


class ExtraDashboardPayloads(NamedTuple):
    """Named data lists for extra dashboard sections."""

    reasons: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    ips: list[dict[str, Any]]
    root_domains: list[dict[str, Any]]
    gafam: list[dict[str, Any]]
    countries: list[dict[str, Any]]
    encryption: list[dict[str, Any]]
    dnssec: list[dict[str, Any]]


async def _fetch_extra_payloads(
    profile_id: str, dom_params: dict[str, Any], params: dict[str, Any]
) -> ExtraDashboardPayloads | dict[str, Any]:
    """Fetch data lists for extra dashboard sections.

    Fetchers are keyed by result-field name and the tuple is built with
    keyword arguments, so reordering entries cannot reassign section data.
    """
    sem = asyncio.Semaphore(OVERVIEW_FANOUT_CONCURRENCY)
    fetchers = {
        "reasons": _bounded_fetch_analytics_payload(sem, profile_id, "reasons", dom_params),
        "devices": _bounded_fetch_analytics_payload(sem, profile_id, "devices", dom_params),
        "ips": _bounded_fetch_analytics_payload(sem, profile_id, "ips", dom_params),
        "root_domains": _bounded_fetch_analytics_payload(sem, profile_id, "domains", {**dom_params, "root": "true"}),
        "gafam": _bounded_fetch_analytics_payload(sem, profile_id, "destinations", {**params, "type": "gafam"}),
        "countries": _bounded_fetch_analytics_payload(sem, profile_id, "destinations", {**params, "type": "countries"}),
        "encryption": _bounded_fetch_analytics_payload(sem, profile_id, "encryption", params),
        "dnssec": _bounded_fetch_analytics_payload(sem, profile_id, "dnssec", params),
    }
    pending = {name: asyncio.ensure_future(coro) for name, coro in fetchers.items()}
    results = {name: await task for name, task in pending.items()}
    err = _first_error(*(r[1] for r in results.values()))
    if err:
        return err
    return ExtraDashboardPayloads(
        reasons=_payload_data(results["reasons"][0]),
        devices=_payload_data(results["devices"][0]),
        ips=_payload_data(results["ips"][0]),
        root_domains=_payload_data(results["root_domains"][0]),
        gafam=_payload_data(results["gafam"][0]),
        countries=_payload_data(results["countries"][0]),
        encryption=_payload_data(results["encryption"][0]),
        dnssec=_payload_data(results["dnssec"][0]),
    )


async def _append_extra_dashboard_sections(
    profile_id: str,
    view: Column,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    domain_view: str = "table",
) -> dict[str, Any] | None:
    """Fetch and append reasons, devices, IPs, root domains, GAFAM and security cards."""
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit)
    params = _build_analytics_params(from_time=from_time, to_time=to_time)

    payloads = await _fetch_extra_payloads(profile_id, dom_params, params)
    if isinstance(payloads, dict):
        return payloads

    with view:
        with Row(gap=4):
            _build_reasons_card(payloads.reasons, view_type=domain_view)
            _build_devices_card(payloads.devices, view_type=domain_view)
        with Row(gap=4):
            _build_domains_card("Root Domains", "Aggregate root domains.", payloads.root_domains, view_type=domain_view)
            _build_ips_card(payloads.ips)
        with Row(gap=4):
            _build_gafam_card(payloads.gafam, view_type=domain_view)
            _build_destinations_card(payloads.countries, view_type=domain_view)
        with Row(gap=4):
            _build_security_card(payloads.encryption, payloads.dnssec)
    return None


def _format_top_items(items: list[tuple[str, int]], limit: int = 5) -> str:
    """Format top items as 'name (count)' list."""
    formatted = [f"{name} ({q:,})" for name, q in items[:limit]]
    return ", ".join(formatted)


def _format_items_summary(title: str, noun: str, items: list[tuple[str, int]], total: int) -> str:
    """Format compact factual summary for a list-based visual card."""
    count = len(items)
    top_str = _format_top_items(items)
    if top_str:
        return f"[Rendered {title} ({count} {noun}, {total:,} queries): {top_str}]"
    return f"[Rendered {title} ({count} {noun}, {total:,} queries)]"


def _format_gafam_summary(chart_data: list[dict[str, Any]], total: int) -> str:
    """Format GAFAM dominance summary."""
    items = [(row["label"], row["queries"]) for row in chart_data]
    top_str = _format_top_items(items)
    if top_str:
        return f"[Rendered GAFAM Dominance ({total:,} queries): {top_str}]"
    return f"[Rendered GAFAM Dominance ({total:,} queries)]"


async def _gafam_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    view_type: Literal["auto", "pie", "bar", "table"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the GAFAM dominance donut chart and table."""
    data, err = await _fetch_card_data(profile_id, "destinations", from_time=from_time, to_time=to_time, type="gafam")
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    chart_data, _ = _gafam_rows(data)
    total = sum(r["queries"] for r in chart_data)
    summary = _format_gafam_summary(chart_data, total)
    return _wrap_card_result(_build_gafam_card(data, view_type=view_type), summary)


async def _security_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Render the security analytics cards (Encrypted DNS and DNSSEC rates)."""
    pid, err = await _resolve_profile(profile_id)
    if err is not None:
        return err
    if pid is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution returned no id")
    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    (encryption_payload, enc_err), (dnssec_payload, sec_err) = await asyncio.gather(
        _fetch_analytics_payload(pid, "encryption", params),
        _fetch_analytics_payload(pid, "dnssec", params),
    )
    first_err = enc_err or sec_err
    if first_err:
        return first_err

    encryption_data = _payload_data(encryption_payload)
    dnssec_data = _payload_data(dnssec_payload)
    enc_val, enc_total, enc_pct = _calc_rate(encryption_data, "encrypted")
    dnssec_val, dnssec_total, dnssec_pct = _calc_rate(dnssec_data, "validated")

    card = _build_security_card(encryption_data, dnssec_data)
    summary = (
        f"[Rendered Security Analytics: Encrypted DNS {enc_pct:.2f}% ({enc_val:,}/{enc_total:,}), "
        f"DNSSEC {dnssec_pct:.2f}% ({dnssec_val:,}/{dnssec_total:,})]"
    )
    return _wrap_card_result(card, summary)


async def _reasons_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the reasons for blocking card."""
    data, err = await _fetch_card_data(profile_id, "reasons", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_reason_label(item), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary("Reasons for Blocking", "blocklists", items, total)
    return _wrap_card_result(_build_reasons_card(data, view_type=view_type), summary)


async def _device_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the device analytics card."""
    data, err = await _fetch_card_data(profile_id, "devices", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_device_label(item), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary("Device Analytics", "devices", items, total)
    return _wrap_card_result(_build_devices_card(data, view_type=view_type), summary)


def _ip_summary_items(data: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Format (ip, queries) pairs for IP analytics summary."""
    return [(str(item.get("ip") or "-"), _item_queries(item)) for item in data]


async def _ip_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the IP analytics card."""
    data, err = await _fetch_card_data(profile_id, "ips", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = _ip_summary_items(data)
    total = sum(q for _, q in items)
    summary = _format_items_summary("IP Analytics", "IP addresses", items, total)
    return _wrap_card_result(_build_ips_card(data), summary)


async def _domain_card_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "table", "bar", "pie"] = "auto",
    *,
    fetch_extra: dict[str, Any],
    title: str,
    description: str,
    summary_title: str,
    summary_noun: str = "domains",
    color: str = "#3b82f6",
    key_name: str = "domain",
) -> dict[str, Any] | ToolResult:
    """Render a domain analytics card (root/resolved/blocked)."""
    data, err = await _fetch_card_data(
        profile_id, "domains", from_time=from_time, to_time=to_time, limit=limit, **fetch_extra
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_domain_label(item, key_name), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary(summary_title, summary_noun, items, total)
    card = _build_domains_card(
        title,
        description,
        data,
        key_name=key_name,
        view_type=view_type,
        color=color,
    )
    return _wrap_card_result(card, summary)


async def _country_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "table", "bar", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the destination countries analytics card."""
    data, err = await _fetch_card_data(
        profile_id, "destinations", from_time=from_time, to_time=to_time, limit=limit, type="countries"
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_destination_label(item), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary("Traffic Destinations", "countries", items, total)
    return _wrap_card_result(_build_destinations_card(data, view_type=view_type), summary)


async def _kpi_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Render top KPI metric cards."""
    data, err = await _fetch_card_data(profile_id, "status", from_time=from_time, to_time=to_time)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    kpis = _calculate_status_kpis(data)
    with Column(gap=4, css_class="p-6") as view:
        _build_kpi_row(kpis)
    summary = (
        f"[Rendered NextDNS KPI Metrics: {kpis['total']:,} queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%)]"
    )
    app = PrefabApp(view=view, theme=DASHBOARD_THEME)
    return ToolResult(content=summary, structured_content=app)


# ==============================================================================
# Dispatch and Public Overview Dashboard Tool
# ==============================================================================

DashboardSection = Literal[
    "overview",
    "all",
    "kpi",
    "domains",
    "blocked_domains",
    "reasons",
    "devices",
    "ips",
    "root_domains",
    "countries",
    "gafam",
    "security",
]

DASHBOARD_SECTIONS = get_args(DashboardSection)


class _WidgetHandler(Protocol):
    def __call__(
        self,
        profile_id: OptionalProfileId = None,
        from_time: str | int = "-30d",
        to_time: str | int = "now",
        limit: int = 10,
        view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    ) -> Awaitable[dict[str, Any] | ToolResult]: ...


WidgetHandler = _WidgetHandler


async def _dispatch_kpi(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _kpi_analytics_impl(profile_id, from_time=from_time, to_time=to_time)


async def _dispatch_domains(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _domain_card_analytics_impl(
        profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        view_type=view_type,
        fetch_extra={"status": "default"},
        title="Resolved Domains",
        description="Domains that resolved without being blocked.",
        summary_title="Resolved Domains",
        color="#3b82f6",
    )


async def _dispatch_blocked_domains(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _domain_card_analytics_impl(
        profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        view_type=view_type,
        fetch_extra={"status": "blocked"},
        title="Blocked Domains",
        description="Domains blocked by security, privacy, or parental controls.",
        summary_title="Blocked Domains",
        color="#ef4444",
    )


async def _dispatch_reasons(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _reasons_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_devices(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _device_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_ips(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _ip_analytics_impl(profile_id, from_time=from_time, to_time=to_time, limit=limit)


async def _dispatch_root_domains(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _domain_card_analytics_impl(
        profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        view_type=view_type,
        fetch_extra={"root": "true"},
        title="Root Domains",
        description="Aggregate queries for root domains and subdomains.",
        summary_title="Root Domains",
        key_name="domain",
    )


async def _dispatch_countries(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _country_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_gafam(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _gafam_analytics_impl(profile_id, from_time=from_time, to_time=to_time, view_type=view_type)


async def _dispatch_security(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _security_analytics_impl(profile_id, from_time=from_time, to_time=to_time)


_WIDGET_DISPATCH: dict[str, _WidgetHandler] = {
    "kpi": _dispatch_kpi,
    "domains": _dispatch_domains,
    "blocked_domains": _dispatch_blocked_domains,
    "reasons": _dispatch_reasons,
    "devices": _dispatch_devices,
    "ips": _dispatch_ips,
    "root_domains": _dispatch_root_domains,
    "countries": _dispatch_countries,
    "gafam": _dispatch_gafam,
    "security": _dispatch_security,
}


def _resolve_domain_view(
    domain_view: Literal["table", "bar", "pie"],
    view_type: Literal["auto", "bar", "table", "pie"],
) -> Literal["table", "bar", "pie"]:
    """Resolve active domain view format."""
    if view_type in ("table", "bar", "pie"):
        return view_type
    return domain_view


def _overview_top_summary(r_data: list[dict[str, Any]], b_data: list[dict[str, Any]]) -> str:
    """Summarize leading resolved and blocked domains."""
    top_res = _format_top_items([(_domain_label(item, "domain"), _item_queries(item)) for item in r_data])
    top_blk = _format_top_items([(_domain_label(item, "domain"), _item_queries(item)) for item in b_data])
    return f"Top resolved: {top_res or 'none'}; Top blocked: {top_blk or 'none'}"


async def _render_overview_page(
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    section: str,
    domain_view: Literal["table", "bar", "pie"],
) -> dict[str, Any] | ToolResult:
    """Fetch payloads, assemble layout, and return ToolResult for overview or all."""
    s_p, ser_p, r_data, b_data, fetch_err = await _fetch_overview_payloads(pid, from_time, to_time, limit)
    if fetch_err:
        return fetch_err

    kpis = _calculate_status_kpis(_payload_data(s_p))
    view = _assemble_overview_layout(kpis, ser_p, r_data, b_data, domain_view=domain_view)
    if section == "all":
        extra_err = await _append_extra_dashboard_sections(
            pid, view, from_time, to_time, limit, domain_view=domain_view
        )
        if extra_err:
            return extra_err

    top_summary = _overview_top_summary(r_data, b_data)
    summary = (
        f"[Rendered NextDNS Dashboard: {kpis['total']:,} queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%). "
        f"{top_summary}]"
    )
    return ToolResult(content=summary, structured_content=PrefabApp(view=view, theme=DASHBOARD_THEME))


def _validate_dashboard_arg(name: str, value: Any, allowed_literal: Any) -> dict[str, Any] | None:
    """Return an INVALID_ARGUMENT payload when ``value`` is outside ``allowed_literal``."""
    allowed = set(get_args(allowed_literal))
    if value in allowed:
        return None
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid '{name}': {value!r}; allowed values: {sorted(allowed)}",
        **{name: value},
    )


def _validate_overview_dashboard_args(section: Any, view_type: Any, domain_view: Any) -> dict[str, Any] | None:
    """Validate overview dashboard arguments; return an error payload or None."""
    for name, value, allowed in (
        ("section", section, DashboardSection),
        ("view_type", view_type, Literal["auto", "bar", "table", "pie"]),
        ("domain_view", domain_view, Literal["table", "bar", "pie"]),
    ):
        err = _validate_dashboard_arg(name, value, allowed)
        if err is not None:
            return err
    return None


async def overviewDashboard(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    section: DashboardSection = "overview",
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    domain_view: Literal["table", "bar", "pie"] = "table",
) -> dict[str, Any] | ToolResult:
    """Render interactive visual Prefab UI analytics dashboards and specialized widgets.

    Displays visual cards, charts, progress indicators, and sortable tables for NextDNS analytics.
    Use this tool whenever the user asks for a dashboard, visual overview, or interactive widget
    (e.g., 'show dashboard', 'visualize top domains', 'display device widget', 'show security cards').
    For raw JSON query data without UI rendering, use the respective getAnalytics* API tools instead.

    Sections available via the `section` parameter:
    - "overview" (default): Full overview layout mirroring the NextDNS web UI (KPI cards, queries evolution chart, resolved & blocked domains).
    - "all": Complete dashboard with overview cards plus reasons, devices, root domains, IPs, GAFAM, and security cards.
    - "kpi": Top KPI summary cards (Total Queries, Blocked Queries in red, Queries Blocked rate).
    - "domains": Top resolved domains card with format selection.
    - "blocked_domains": Top blocked domains card with format selection.
    - "reasons": Block reasons breakdown (security/privacy filters and blocklists).
    - "devices": Active client devices and models breakdown.
    - "ips": Client IP addresses and ISP/geo details.
    - "root_domains": Top aggregate root domains.
    - "countries": Traffic breakdown by country destination or origin.
    - "gafam": Traffic dominance share of Google, Apple, Facebook, Amazon, and Microsoft.
    - "security": Encrypted DNS (DoH/DoT) and DNSSEC validation percentage cards.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start of time window (e.g. "-30d", "-7d", "-1d", Unix timestamp).
        to_time: End of time window (default "now").
        limit: Maximum number of rows to display in lists and tables (default 10).
        section: Dashboard section or widget to render (default "overview").
        view_type: Visualization format: "auto" (default: table or card default), "bar", "table", or "pie".
        domain_view: Visualization format for domain cards in overview mode: "table" (default), "bar", or "pie".
    """
    arg_err = _validate_overview_dashboard_args(section, view_type, domain_view)
    if arg_err is not None:
        return arg_err

    eff_view = _resolve_domain_view(domain_view, view_type)

    if section in _WIDGET_DISPATCH:
        norm_from = normalize_time_value(from_time)
        norm_to = normalize_time_value(to_time)
        budget_err = _series_budget_error(norm_from, norm_to, _overview_interval(norm_from, norm_to))
        if budget_err is not None:
            return budget_err
        return await _WIDGET_DISPATCH[section](profile_id, norm_from, norm_to, limit, eff_view)

    pid, err = await _resolve_profile(profile_id)
    if err is not None:
        return err
    if pid is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution returned no id")

    return await _render_overview_page(pid, from_time, to_time, limit, section, eff_view)
