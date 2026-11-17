"""Prefab UI builders for the dashboard tools.

Every function here answers "what does this card look like": cards, charts, tables,
progress sections, and the page-level views that hold them. It reads analytics rows
through ``data``, the shared series primitives through ``nextdns_mcp.charts`` and the
shared chart components through ``nextdns_mcp.chart_ui``; it never calls the transport
itself, and it never writes the summary text a model reads back - that is ``formatters``.

Names beginning with ``_`` are package-internal: the dashboard package's public
surface is what ``__init__`` re-exports.

SPDX-License-Identifier: MIT
"""

import logging
from typing import Any

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
from prefab_ui.components.charts import BarChart, ChartSeries, PieChart

from ...chart_ui import (
    DASHBOARD_THEME,
    build_chart_series,
    build_timeseries_area_chart,
)
from ...charts import (
    build_chart_data,
    downsample_chart_series,
    validate_series_payload,
)
from .data import (
    ExtraDashboardPayloads,
    _calc_rate,
    _destination_label,
    _destination_row,
    _device_label,
    _device_row,
    _domain_label,
    _gafam_rows,
    _ip_row,
    _item_queries,
    _reason_label,
    _reason_row,
)

logger = logging.getLogger(__name__)

# Reusable table columns shared across dashboard builders.
QUERIES_COLUMN = DataTableColumn(key="queries", header="Queries", sortable=True, format="number")
SHARE_COLUMN = DataTableColumn(key="share", header="Share", sortable=True)


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
    """Build the queries evolution chart card from a time-series payload.

    The rendered rows are downsampled through the shared chart budget: a
    multi-year overview answers up to 1,999 intervals, and this card is one of
    many on the page, so full-resolution rows here bloat the whole
    ``PrefabApp`` rather than one chart result (issue #550).
    """
    try:
        times, series_data = validate_series_payload(series_payload)
        if not times or not series_data:
            return None
        chart_times, chart_series_data = downsample_chart_series(times, series_data)
        chart_data = build_chart_data(chart_times, chart_series_data)
        chart_series = build_chart_series(chart_series_data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Error preparing queries series data: {exc}")
        return None

    with Card(css_class="w-full") as card:
        with CardHeader():
            CardTitle("Queries")
            CardDescription("Evolution of queries over time.")
        with CardContent():
            build_timeseries_area_chart(chart_data, chart_series)
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


def _append_extra_cards(
    view: Column,
    payloads: ExtraDashboardPayloads,
    domain_view: str = "table",
    view_type: str = "auto",
) -> None:
    """Append the reasons, devices, IPs, root-domain, GAFAM, countries and security cards.

    The caller fetches; this only lays out. ``payloads`` carries one named field
    per section, so the pairing between a fetch and the card that renders it is
    carried by name rather than by position. Only the root-domain card is a domain
    card, so it alone takes the already-resolved ``domain_view``; the other cards
    take ``view_type``, which keeps ``"auto"`` and renders each card's own default
    instead of being coerced to a table (#547).
    """
    with view:
        with Row(gap=4):
            _build_reasons_card(payloads.reasons, view_type=view_type)
            _build_devices_card(payloads.devices, view_type=view_type)
        with Row(gap=4):
            _build_domains_card("Root Domains", "Aggregate root domains.", payloads.root_domains, view_type=domain_view)
            _build_ips_card(payloads.ips)
        with Row(gap=4):
            _build_gafam_card(payloads.gafam, view_type=view_type)
            _build_destinations_card(payloads.countries, view_type=view_type)
        with Row(gap=4):
            _build_security_card(payloads.encryption, payloads.dnssec)


def _build_page_app(view: Column) -> PrefabApp:
    """Wrap an assembled dashboard view in the served Prefab app."""
    return PrefabApp(view=view, theme=DASHBOARD_THEME)


def _build_kpi_app(kpis: dict[str, Any]) -> PrefabApp:
    """Build the standalone KPI page: the three metric cards, nothing else."""
    with Column(gap=4, css_class="p-6") as view:
        _build_kpi_row(kpis)
    return _build_page_app(view)


def _wrap_card_result(card: Card, summary_text: str) -> ToolResult:
    """Wrap a single visual card into a ToolResult."""
    with Column(gap=4, css_class="p-6", children=[card]) as view:
        pass
    app = _build_page_app(view)
    return ToolResult(content=summary_text, structured_content=app)
