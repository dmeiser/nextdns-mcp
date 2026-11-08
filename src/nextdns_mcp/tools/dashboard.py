"""Interactive analytics dashboard and specialized analytics UI tools for NextDNS.

SPDX-License-Identifier: MIT
"""

import asyncio
import logging
from typing import Any, Literal

from fastmcp.tools.base import ToolResult
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
    Metric,
    Progress,
    Row,
    Text,
)
from prefab_ui.components.charts import LineChart, PieChart

from ..coercion import OptionalProfileId
from ..errors import ErrorCode, error_payload
from ..transport import MAX_SERIES_RESPONSE_BYTES, _api_request_payload
from ..utils import normalize_time_value, resolve_profile_id
from .plots import _build_chart_data, _build_chart_series, _validate_series_payload

logger = logging.getLogger(__name__)


def _time_params(from_time: str | int | None, to_time: str | int | None) -> dict[str, Any]:
    """Extract normalized time window parameters."""
    p: dict[str, Any] = {}
    if from_time is not None:
        p["from"] = normalize_time_value(from_time)
    if to_time is not None:
        p["to"] = normalize_time_value(to_time)
    return p


def _build_analytics_params(
    from_time: str | int | None = "-30d",
    to_time: str | int | None = "now",
    limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build normalized query parameters for analytics endpoints."""
    params = _time_params(from_time, to_time)
    if limit is not None:
        params["limit"] = limit
    params.update({k: v for k, v in extra.items() if v is not None})
    return params


async def _fetch_analytics_payload(
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics endpoint payload through the access-controlled transport."""
    url = f"/profiles/{profile_id}/analytics/{endpoint}"
    payload = await _api_request_payload("GET", url, params=params, max_response_bytes=MAX_SERIES_RESPONSE_BYTES)
    if isinstance(payload, dict) and "code" in payload and "error" in payload and "data" not in payload:
        return None, payload
    return payload, None


def _format_share(count: int, total: int) -> str:
    """Format a query count as a percentage share of the total."""
    return f"{(count / total * 100):.1f}%" if total > 0 else "0.0%"


def _item_queries(item: dict[str, Any]) -> int:
    """Extract integer query count from an item dict."""
    return int(item.get("queries") or 0)


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
    """Build the three top KPI metric cards."""
    with Row(gap=6) as row:
        Metric(label="Queries", value=f"{kpis['total']:,}")
        Metric(label="Blocked Queries", value=f"{kpis['blocked']:,}")
        Metric(label="Queries Blocked", value=f"{kpis['rate']:.2f}%")
    return row


def _build_queries_series_card(series_payload: dict[str, Any]) -> Card | None:
    """Build the queries evolution chart card from a time-series payload."""
    times, series_data = _validate_series_payload(series_payload)
    if not times or not series_data:
        return None

    chart_data = _build_chart_data(times, series_data)
    chart_series = _build_chart_series(series_data)

    with Card(css_class="w-full") as card:
        with CardHeader():
            CardTitle("Queries")
            CardDescription("Evolution of queries over time.")
        with CardContent():
            LineChart(
                data=chart_data,
                series=chart_series,
                xAxis="time",
                xAxisLabel="Time",
                yAxisLabel="Queries",
                showLegend=True,
                showTooltip=True,
                showGrid=True,
            )
    return card


def _build_domains_card(
    title: str,
    description: str,
    data: list[dict[str, Any]],
    key_name: str = "domain",
) -> Card:
    """Build a domain listing card with a sortable data table."""
    rows: list[dict[str, Any] | Any] = [
        {"domain": str(item.get(key_name) or "-"), "queries": f"{_item_queries(item):,}"} for item in data
    ]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle(title)
            CardDescription(description)
        with CardContent():
            DataTable(
                columns=[
                    DataTableColumn(key="domain", header="Domain", sortable=True),
                    DataTableColumn(key="queries", header="Queries", sortable=True),
                ],
                rows=rows,
            )
    return card


def _reason_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one reasons-table row."""
    q = _item_queries(item)
    return {
        "name": str(item.get("name") or item.get("id") or "Unknown"),
        "queries": f"{q:,}",
        "share": _format_share(q, total),
    }


def _build_reasons_card(data: list[dict[str, Any]]) -> Card:
    """Build the reasons for blocking card."""
    total = sum(_item_queries(item) for item in data)
    rows: list[dict[str, Any] | Any] = [_reason_row(item, total) for item in data]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("Reasons for Blocking")
            CardDescription("Security, Privacy and/or Parental Control settings that blocked the most queries.")
        with CardContent():
            DataTable(
                columns=[
                    DataTableColumn(key="name", header="Reason / Filter", sortable=True),
                    DataTableColumn(key="queries", header="Queries", sortable=True),
                    DataTableColumn(key="share", header="Share", sortable=True),
                ],
                rows=rows,
            )
    return card


def _device_name(item: dict[str, Any]) -> str:
    """Extract a human-readable device name."""
    dev_id = str(item.get("id") or "-")
    return "Unidentified" if dev_id == "__UNIDENTIFIED__" else str(item.get("name") or dev_id)


def _device_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one device row."""
    q = _item_queries(item)
    return {
        "device": _device_name(item),
        "model": str(item.get("model") or "-"),
        "queries": f"{q:,}",
        "share": _format_share(q, total),
    }


def _build_devices_card(data: list[dict[str, Any]]) -> Card:
    """Build the devices card."""
    total = sum(_item_queries(item) for item in data)
    rows: list[dict[str, Any] | Any] = [_device_row(item, total) for item in data]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("Devices")
            CardDescription("Devices making the queries.")
        with CardContent():
            DataTable(
                columns=[
                    DataTableColumn(key="device", header="Device", sortable=True),
                    DataTableColumn(key="model", header="Model", sortable=True),
                    DataTableColumn(key="queries", header="Queries", sortable=True),
                    DataTableColumn(key="share", header="Share", sortable=True),
                ],
                rows=rows,
            )
    return card


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
        "queries": f"{_item_queries(item):,}",
    }


def _build_ips_card(data: list[dict[str, Any]]) -> Card:
    """Build the IP addresses card."""
    rows: list[dict[str, Any] | Any] = [_ip_row(item) for item in data]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("IPs")
            CardDescription("IP addresses making the queries.")
        with CardContent():
            DataTable(
                columns=[
                    DataTableColumn(key="ip", header="IP Address", sortable=True),
                    DataTableColumn(key="isp", header="ISP", sortable=True),
                    DataTableColumn(key="location", header="Location", sortable=True),
                    DataTableColumn(key="queries", header="Queries", sortable=True),
                ],
                rows=rows,
            )
    return card


def _single_gafam_row(item: dict[str, Any], total: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format single company entry for GAFAM donut chart and table."""
    name = str(item.get("company") or "others").capitalize()
    q = _item_queries(item)
    return {"company": name, "queries": q}, {"company": name, "queries": f"{q:,}", "share": _format_share(q, total)}


def _gafam_rows(data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any] | Any]]:
    """Format data rows for the GAFAM donut chart and data table."""
    total = sum(_item_queries(item) for item in data)
    pairs = [_single_gafam_row(item, total) for item in data]
    table_rows: list[dict[str, Any] | Any] = [p[1] for p in pairs]
    return [p[0] for p in pairs], table_rows


def _build_gafam_card(data: list[dict[str, Any]]) -> Card:
    """Build the GAFAM dominance donut chart and table card."""
    chart_data, table_rows = _gafam_rows(data)
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("GAFAM Dominance")
            CardDescription(
                "Traffic share of dominant internet companies (Google, Apple, Facebook, Amazon, Microsoft)."
            )
        with CardContent(), Column(gap=4):
            PieChart(
                data=chart_data,
                dataKey="queries",
                nameKey="company",
                innerRadius=50,
                showLegend=True,
                showTooltip=True,
            )
            DataTable(
                columns=[
                    DataTableColumn(key="company", header="Company", sortable=True),
                    DataTableColumn(key="queries", header="Queries", sortable=True),
                    DataTableColumn(key="share", header="Share", sortable=True),
                ],
                rows=table_rows,
            )
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
        with CardContent(), Column(gap=4) as content_col:
            content_col.children.append(_build_security_section("Encrypted DNS", enc_val, enc_total, enc_pct))
            content_col.children.append(
                _build_security_section("DNSSEC Validation", dnssec_val, dnssec_total, dnssec_pct)
            )
    return card


def _destination_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one country destination row."""
    sample = ", ".join(item.get("domains") or [])[:60]
    return {
        "country": str(item.get("name") or "-"),
        "code": str(item.get("code") or "-"),
        "queries": f"{_item_queries(item):,}",
        "sample_domains": sample or "-",
    }


def _build_destinations_card(data: list[dict[str, Any]]) -> Card:
    """Build the destination countries card."""
    rows: list[dict[str, Any] | Any] = [_destination_row(item) for item in data]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("Traffic Destination")
            CardDescription("Top destination countries based on DNS queries.")
        with CardContent():
            DataTable(
                columns=[
                    DataTableColumn(key="country", header="Country", sortable=True),
                    DataTableColumn(key="code", header="Code", sortable=True),
                    DataTableColumn(key="queries", header="Queries", sortable=True),
                    DataTableColumn(key="sample_domains", header="Sample Domains"),
                ],
                rows=rows,
            )
    return card


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


def _validate_card_data(
    payload: dict[str, Any] | None, endpoint: str, pid: str
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Extract list data from payload or return typed no-data error."""
    data = _payload_data(payload)
    if not data:
        return None, error_payload(ErrorCode.NO_DATA, f"No {endpoint} data available", profile_id=pid)
    return data, None


async def _fetch_card_data(
    profile_id: OptionalProfileId,
    endpoint: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int | None = None,
    **extra: Any,
) -> tuple[str | None, list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Fetch single endpoint data list with profile validation and error classification."""
    pid, err = await _resolve_profile(profile_id)
    if err or pid is None:
        return None, None, err
    params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, **extra)
    payload, fetch_err = await _fetch_analytics_payload(pid, endpoint, params)
    if fetch_err:
        return pid, None, fetch_err
    data, no_data_err = _validate_card_data(payload, endpoint, pid)
    return pid, data, no_data_err


def _wrap_card_result(card: Card, summary_text: str) -> ToolResult:
    """Wrap a single visual card into a ToolResult."""
    with Column(gap=4, css_class="p-6") as view:
        view.children.append(card)
    return ToolResult(content=summary_text, structured_content=view)


async def _fetch_overview_payloads(
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch all datasets required for the main overview cards."""
    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit)
    blk_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="blocked")
    ser_params = {"from": normalize_time_value(from_time), "to": normalize_time_value(to_time), "interval": 86400}

    s_task = _fetch_analytics_payload(pid, "status", params)
    ser_task = _fetch_analytics_payload(pid, "status;series", ser_params)
    r_task = _fetch_analytics_payload(pid, "domains", dom_params)
    b_task = _fetch_analytics_payload(pid, "domains", blk_params)

    (s_p, s_err), (ser_p, _), (r_p, _), (b_p, _) = await asyncio.gather(s_task, ser_task, r_task, b_task)
    if s_err:
        return {}, {}, [], [], s_err
    return (s_p or {}), (ser_p or {}), _payload_data(r_p), _payload_data(b_p), None


def _assemble_overview_layout(
    kpis: dict[str, Any],
    series_payload: dict[str, Any],
    resolved_data: list[dict[str, Any]],
    blocked_data: list[dict[str, Any]],
) -> Column:
    """Build the main overview cards layout."""
    series_card = _build_queries_series_card(series_payload)
    resolved_card = _build_domains_card(
        "Resolved Domains", "Domains that resolved without being blocked.", resolved_data
    )
    blocked_card = _build_domains_card(
        "Blocked Domains", "Domains blocked by security, privacy, or parental controls.", blocked_data
    )

    with Column(gap=6, css_class="p-6") as view:
        Heading("NextDNS Overview Dashboard", level=1)
        view.children.append(_build_kpi_row(kpis))
        if series_card is not None:
            view.children.append(series_card)
        with Row(gap=4) as dom_row:
            dom_row.children.append(resolved_card)
            dom_row.children.append(blocked_card)
        view.children.append(dom_row)
    return view


def _append_two_cards_row(view: Column, left_card: Card, right_card: Card) -> None:
    """Append a two-card horizontal row to a view container."""
    with Row(gap=4) as row:
        row.children.append(left_card)
        row.children.append(right_card)
    view.children.append(row)


async def _fetch_extra_payloads(
    profile_id: str, dom_params: dict[str, Any], params: dict[str, Any]
) -> list[list[dict[str, Any]]]:
    """Fetch data lists for extra dashboard sections."""
    tasks = [
        _fetch_analytics_payload(profile_id, "reasons", dom_params),
        _fetch_analytics_payload(profile_id, "devices", dom_params),
        _fetch_analytics_payload(profile_id, "ips", dom_params),
        _fetch_analytics_payload(profile_id, "domains", {**dom_params, "root": "true"}),
        _fetch_analytics_payload(profile_id, "destinations", {**params, "type": "gafam"}),
        _fetch_analytics_payload(profile_id, "encryption", params),
        _fetch_analytics_payload(profile_id, "dnssec", params),
    ]
    res = await asyncio.gather(*tasks)
    return [_payload_data(item[0]) for item in res]


async def _append_extra_dashboard_sections(
    profile_id: str,
    view: Column,
    from_time: str | int,
    to_time: str | int,
    limit: int,
) -> None:
    """Fetch and append reasons, devices, IPs, root domains, GAFAM and security cards."""
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit)
    params = _build_analytics_params(from_time=from_time, to_time=to_time)

    r_d, d_d, i_d, rt_d, g_d, e_d, sec_d = await _fetch_extra_payloads(profile_id, dom_params, params)

    _append_two_cards_row(view, _build_reasons_card(r_d), _build_devices_card(d_d))
    _append_two_cards_row(
        view, _build_domains_card("Root Domains", "Aggregate root domains.", rt_d), _build_ips_card(i_d)
    )
    _append_two_cards_row(view, _build_gafam_card(g_d), _build_security_card(e_d, sec_d))


async def _overview_dashboard_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    section: Literal["overview", "all"] = "overview",
) -> dict[str, Any] | ToolResult:
    """Construct an overview dashboard matching the NextDNS web UI."""
    pid, err = await _resolve_profile(profile_id)
    if err:
        return err
    assert pid is not None

    s_p, ser_p, r_data, b_data, fetch_err = await _fetch_overview_payloads(pid, from_time, to_time, limit)
    if fetch_err:
        return fetch_err

    kpis = _calculate_status_kpis(_payload_data(s_p))
    view = _assemble_overview_layout(kpis, ser_p, r_data, b_data)

    if section == "all":
        await _append_extra_dashboard_sections(pid, view, from_time, to_time, limit)

    summary = f"NextDNS Dashboard: {kpis['total']:,} total queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%)"
    return ToolResult(content=summary, structured_content=view)


async def _gafam_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Render the GAFAM dominance donut chart and table."""
    _pid, data, err = await _fetch_card_data(
        profile_id, "destinations", from_time=from_time, to_time=to_time, type="gafam"
    )
    if err:
        return err
    assert data is not None
    total = sum(_item_queries(item) for item in data)
    summary = f"GAFAM Dominance: {total:,} total queries analyzed across major tech platforms"
    return _wrap_card_result(_build_gafam_card(data), summary)


async def _security_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Render the security analytics cards (Encrypted DNS and DNSSEC rates)."""
    pid, err = await _resolve_profile(profile_id)
    if err:
        return err
    assert pid is not None
    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    (enc_p, enc_err), (sec_p, sec_err) = await asyncio.gather(
        _fetch_analytics_payload(pid, "encryption", params),
        _fetch_analytics_payload(pid, "dnssec", params),
    )
    first_err = enc_err or sec_err
    if first_err:
        return first_err

    card = _build_security_card(_payload_data(enc_p), _payload_data(sec_p))
    return _wrap_card_result(card, "Security Analytics: Encrypted DNS and DNSSEC status")


async def _reasons_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the reasons for blocking card."""
    _pid, data, err = await _fetch_card_data(profile_id, "reasons", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    assert data is not None
    total = sum(_item_queries(item) for item in data)
    summary = f"Reasons for Blocking: top {len(data)} blocklists accounted for {total:,} blocked queries"
    return _wrap_card_result(_build_reasons_card(data), summary)


async def _device_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the device analytics card."""
    _pid, data, err = await _fetch_card_data(profile_id, "devices", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    assert data is not None
    total = sum(_item_queries(item) for item in data)
    summary = f"Device Analytics: top {len(data)} devices made {total:,} queries"
    return _wrap_card_result(_build_devices_card(data), summary)


async def _ip_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the IP analytics card."""
    _pid, data, err = await _fetch_card_data(profile_id, "ips", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    assert data is not None
    total = sum(_item_queries(item) for item in data)
    summary = f"IP Analytics: top {len(data)} IP addresses made {total:,} queries"
    return _wrap_card_result(_build_ips_card(data), summary)


async def _root_domain_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the root domain analytics card."""
    _pid, data, err = await _fetch_card_data(
        profile_id, "domains", from_time=from_time, to_time=to_time, limit=limit, root="true"
    )
    if err:
        return err
    assert data is not None
    total = sum(_item_queries(item) for item in data)
    summary = f"Root Domain Analytics: top {len(data)} root domains made {total:,} queries"
    card = _build_domains_card(
        "Root Domains", "Aggregate queries for root domains and subdomains.", data, key_name="domain"
    )
    return _wrap_card_result(card, summary)


async def _country_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the destination countries analytics card."""
    _pid, data, err = await _fetch_card_data(
        profile_id, "destinations", from_time=from_time, to_time=to_time, limit=limit, type="countries"
    )
    if err:
        return err
    assert data is not None
    total = sum(_item_queries(item) for item in data)
    summary = f"Traffic Destinations: top {len(data)} countries received {total:,} queries"
    return _wrap_card_result(_build_destinations_card(data), summary)


# ==============================================================================
# Public tool wrappers (re-declare signatures and forward by keyword)
# ==============================================================================


async def overviewDashboard(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    section: Literal["overview", "all"] = "overview",
) -> dict[str, Any] | ToolResult:
    """Interactive NextDNS overview analytics dashboard.

    Displays an overview dashboard mirroring the NextDNS web UI:
    - Top KPI summary cards: Total queries, blocked queries, and percentage blocked.
    - Evolution of queries over time: interactive line chart of query volumes.
    - Top resolved domains table.
    - Top blocked domains table.
    - Setting section="all" appends blocking reasons, devices, IPs, root domains, GAFAM donut, and security metrics.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d, or epoch timestamp).
        to_time: End time (default "now").
        limit: Maximum number of rows per domain table (default 10).
        section: "overview" (default) or "all" to include all analytics cards.
    """
    return await _overview_dashboard_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        section=section,
    )


async def gafamAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Interactive GAFAM dominance donut chart and table.

    Displays the traffic share for Google, Amazon, Facebook (Meta), Apple, Microsoft, and Others.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
    """
    return await _gafam_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
    )


async def securityAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Interactive security analytics for Encrypted DNS and DNSSEC rates.

    Displays percentage progress bars and query breakdown for DoH/DoT transport and DNSSEC validation.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
    """
    return await _security_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
    )


async def reasonsAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Interactive table of reasons for blocking (blocklists and filters).

    Displays the security, privacy, and parental control blocklists that blocked the most queries.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
        limit: Maximum number of blocklists to display (default 10).
    """
    return await _reasons_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )


async def deviceAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Interactive device analytics table.

    Displays active devices, device models, query counts, and traffic share.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
        limit: Maximum number of devices to display (default 10).
    """
    return await _device_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )


async def ipAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Interactive IP address analytics table.

    Displays IP addresses making queries, their ISP/network, and geographic location.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
        limit: Maximum number of IP addresses to display (default 10).
    """
    return await _ip_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )


async def rootDomainAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Interactive root domain analytics table.

    Displays aggregate queries for root domains and all their subdomains.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
        limit: Maximum number of root domains to display (default 10).
    """
    return await _root_domain_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )


async def countryAnalytics(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Interactive destination countries analytics table.

    Displays top destination countries based on DNS queries with country codes and sample domains.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start time (relative string like -30d or -1d).
        to_time: End time (default "now").
        limit: Maximum number of countries to display (default 10).
    """
    return await _country_analytics_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )
