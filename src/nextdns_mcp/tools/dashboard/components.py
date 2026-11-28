"""Prefab UI builders for the dashboard tools.

Every function here answers "what does this card look like": cards, charts, tables,
progress sections, and the page-level views that hold them. It consumes data the
caller already prepared - it reads analytics rows through ``data``, the shared chart
components through ``nextdns_mcp.chart_ui``; it never calls the transport itself, it
never writes the summary text a model reads back - that is ``formatters`` - and it
never assembles the MCP ``ToolResult`` - that is ``tool`` (WORK-339).

Names beginning with ``_`` are private to this module: helpers shared with
the other dashboard modules cross under public names (issue #637), and the
package's public surface is what ``__init__`` re-exports.

SPDX-License-Identifier: MIT
"""

import re
from typing import Any, cast

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
    ExpandableRow,
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
from ...charts import cap_chart_categories
from .data import (
    PreparedExtraSections,
    destination_label,
    destination_row,
    device_label,
    device_rows,
    domain_label,
    ip_row,
    item_queries,
    reason_label,
    reason_rows,
)

DEFAULT_BAR_COLOR = "#3b82f6"
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

QUERIES_COLUMN = DataTableColumn(key="queries", header="Queries", sortable=True, format="number")
SHARE_COLUMN = DataTableColumn(key="share", header="Share", sortable=True, format="percent:1")

# ``DataTable.rows`` is invariant in its element type: a plain ``list[dict[str, Any]]``
# is not accepted where ``list[dict[str, Any] | ExpandableRow]`` is expected. Build
# row collections with the component's own element type instead of suppressing mypy.
_TableRows = list[dict[str, Any] | ExpandableRow]

# A ``DataTable`` serializes every row it is given, so ``limit`` (accepted up to
# 500) was the only thing between a caller and 500 rows per card inside one
# result: ``section="all"`` at ``limit=500`` embedded thousands of rows in the
# document handed back to the model (issue #789). Tables render the busiest rows
# and paginate them - a capped table and a paginated table are the same decision,
# so they are written once, in ``_data_table``.
MAX_TABLE_ROWS = 100
TABLE_PAGE_SIZE = 25


def _data_table(columns: list[DataTableColumn], rows: _TableRows) -> DataTable:
    """Build one bounded, paginated dashboard table."""
    rendered = rows[:MAX_TABLE_ROWS]
    return DataTable(
        columns=columns,
        rows=rendered,
        paginated=len(rendered) > TABLE_PAGE_SIZE,
        pageSize=TABLE_PAGE_SIZE,
    )


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


def _build_queries_series_card(chart_data: list[dict[str, Any]], chart_series_data: list[dict[str, Any]]) -> Card:
    """Build the queries evolution chart card from prepared, downsampled series rows.

    Validation and downsampling happened upstream in ``data.prepare_queries_series``;
    this only renders what it is handed.
    """
    with Card(css_class="w-full") as card:
        with CardHeader():
            CardTitle("Queries")
            CardDescription("Evolution of queries over time.")
        with CardContent():
            build_timeseries_area_chart(chart_data, build_chart_series(chart_series_data))
    return card


def _bar_color(color: str) -> str:
    """Only full six-digit hex colors may reach Prefab's style injection."""
    if isinstance(color, str) and _HEX_COLOR.fullmatch(color):
        return color
    return DEFAULT_BAR_COLOR


def _bar_chart(chart_data: list[dict[str, Any]], label: str, color: str = DEFAULT_BAR_COLOR) -> BarChart:
    """Build a horizontal bar chart with rounded pill bars."""
    return BarChart(
        data=cap_chart_categories(chart_data),
        series=[ChartSeries(dataKey="queries", label=label, color=_bar_color(color))],
        xAxis="label",
        horizontal=True,
        barRadius=6,
        showLegend=True,
        showTooltip=True,
        showGrid=True,
    )


def _pie_chart(chart_data: list[dict[str, Any]]) -> PieChart:
    """Build a donut pie chart with slice labels, spacing, and tooltips.

    Upstream counts can arrive netted below zero, and the pie's aggregation
    validation rejects negative values, so every row's count is clamped to
    zero or above here (issue FUNC-5 / WORK-324).
    """
    clamped = [{**row, "queries": max(row.get("queries", 0), 0)} for row in chart_data]
    return PieChart(
        data=cap_chart_categories(clamped),
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
    rows: _TableRows = [{"domain": domain_label(item, key_name), "queries": item_queries(item)} for item in data]
    return _data_table(
        [
            DataTableColumn(key="domain", header="Domain", sortable=True),
            QUERIES_COLUMN,
        ],
        rows,
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
        chart_data = [{"label": domain_label(item, key_name), "queries": item_queries(item)} for item in data]
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


def build_domains_card(
    title: str,
    description: str,
    data: list[dict[str, Any]],
    key_name: str = "domain",
    view_type: str = "table",
    color: str = DEFAULT_BAR_COLOR,
) -> Card:
    """Build a domain listing card with a sortable data table or bar chart."""
    return _build_analytics_card(
        title,
        description,
        lambda: _render_domain_content(title, data, key_name, view_type, color),
    )


def _reasons_table(data: list[dict[str, Any]]) -> DataTable:
    """Build reasons data table from the prepared rows."""
    # ``reason_rows`` returns plain dicts; DataTable's row union is invariant.
    rows: _TableRows = cast(_TableRows, reason_rows(data))
    return _data_table(
        [
            DataTableColumn(key="name", header="Reason / Filter", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows,
    )


def build_reasons_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the reasons for blocking card."""

    def _render():
        if not data:
            Text("No queries were blocked during this period.", css_class="text-sm text-muted-foreground py-4")
        elif view_type == "table":
            _reasons_table(data)
        else:
            chart_data = [{"label": reason_label(item), "queries": item_queries(item)} for item in data]
            _render_chart_view(chart_data, "Blocked Queries", "#ef4444", view_type)

    return _build_analytics_card(
        "Reasons for Blocking",
        "Security, Privacy and/or Parental Control settings that blocked the most queries.",
        _render,
    )


def _devices_table(data: list[dict[str, Any]]) -> DataTable:
    """Build devices data table from the prepared rows."""
    # ``device_rows`` returns plain dicts; DataTable's row union is invariant.
    rows: _TableRows = cast(_TableRows, device_rows(data))
    return _data_table(
        [
            DataTableColumn(key="device", header="Device", sortable=True),
            DataTableColumn(key="model", header="Model", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows,
    )


def _render_device_content(data: list[dict[str, Any]], view_type: str) -> None:
    """Render devices card content based on chosen visualization type."""
    if not data:
        Text("No device queries recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _devices_table(data)
    else:
        chart_data = [{"label": device_label(item), "queries": item_queries(item)} for item in data]
        _render_chart_view(chart_data, "Queries", "#3b82f6", view_type)


def build_devices_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the devices card."""
    return _build_analytics_card(
        "Devices",
        "Devices making the queries.",
        lambda: _render_device_content(data, view_type),
    )


def _ips_table(data: list[dict[str, Any]]) -> DataTable:
    """Build IP addresses data table."""
    rows: _TableRows = [ip_row(item) for item in data]
    return _data_table(
        [
            DataTableColumn(key="ip", header="IP Address", sortable=True),
            DataTableColumn(key="isp", header="ISP", sortable=True),
            DataTableColumn(key="location", header="Location", sortable=True),
            QUERIES_COLUMN,
        ],
        rows,
    )


def _render_ip_content(data: list[dict[str, Any]]) -> None:
    """Render the IP addresses card content."""
    if not data:
        Text("No IP queries recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    else:
        _ips_table(data)


def build_ips_card(data: list[dict[str, Any]]) -> Card:
    """Build the IP addresses card; this fixed-format card is always a table."""
    return _build_analytics_card(
        "IPs",
        "IP addresses making the queries.",
        lambda: _render_ip_content(data),
    )


def _gafam_table(table_rows: list[dict[str, Any]]) -> DataTable:
    """Build GAFAM table."""
    rows: _TableRows = [*table_rows]
    return _data_table(
        [
            DataTableColumn(key="company", header="Company", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows,
    )


def _render_gafam_content(chart_data: list[dict[str, Any]], table_rows: list[dict[str, Any]], view_type: str) -> None:
    """Render the GAFAM card content: one view, or the combined pie-plus-table default."""
    # ``chart_data`` holds one row per upstream item, so an empty chart means empty data.
    if not chart_data:
        Text("No GAFAM traffic recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type == "bar":
        _bar_chart(chart_data, "Queries", "#3b82f6")
    elif view_type == "table":
        _gafam_table(table_rows)
    elif view_type == "pie":
        _pie_chart(chart_data)
    else:
        with Column(gap=4):
            _pie_chart(chart_data)
            _gafam_table(table_rows)


def build_gafam_card(
    chart_data: list[dict[str, Any]],
    table_rows: list[dict[str, Any]],
    view_type: str = "auto",
) -> Card:
    """Build the GAFAM dominance donut chart and table card from prefetched rows."""
    return _build_analytics_card(
        "GAFAM Dominance",
        "Traffic share of dominant internet companies (Google, Apple, Facebook, Amazon, Microsoft).",
        lambda: _render_gafam_content(chart_data, table_rows, view_type),
    )


def _build_security_section(title: str, val: int, total: int, pct: float) -> Column:
    """Build a progress-bar section for one security indicator.

    ``pct`` is the raw rate, and an upstream count can push it outside the
    bar's 0-100 range (a negative ``queries`` value, or matching queries
    counted above the total). The bar is clamped so it never renders past
    full or below empty; the text keeps the number the API reported
    (issue #800).
    """
    with Column(gap=2) as col:
        Text(f"{title}: {pct:.2f}%", css_class="font-semibold")
        Progress(value=max(0.0, min(100.0, pct)))
        Text(f"{val:,} / {total:,} queries", css_class="text-sm text-muted-foreground")
    return col


def _render_security_content(encryption_rate: tuple[int, int, float], dnssec_rate: tuple[int, int, float]) -> None:
    """Render the security card's encrypted DNS and DNSSEC progress sections."""
    enc_val, enc_total, enc_pct = encryption_rate
    dnssec_val, dnssec_total, dnssec_pct = dnssec_rate

    with Column(gap=4):
        _build_security_section("Encrypted DNS", enc_val, enc_total, enc_pct)
        _build_security_section("DNSSEC Validation", dnssec_val, dnssec_total, dnssec_pct)


def build_security_card(encryption_rate: tuple[int, int, float], dnssec_rate: tuple[int, int, float]) -> Card:
    """Build the security card from the encrypted DNS and DNSSEC rate tuples."""
    return _build_analytics_card(
        "Security & Privacy",
        "Encryption and DNSSEC validation rates.",
        lambda: _render_security_content(encryption_rate, dnssec_rate),
    )


def _destinations_table(data: list[dict[str, Any]]) -> DataTable:
    """Build destinations table."""
    rows: _TableRows = [destination_row(item) for item in data]
    return _data_table(
        [
            DataTableColumn(key="country", header="Country", sortable=True),
            DataTableColumn(key="code", header="Code", sortable=True),
            QUERIES_COLUMN,
            DataTableColumn(key="sample_domains", header="Sample Domains"),
        ],
        rows,
    )


def _render_destination_content(data: list[dict[str, Any]], view_type: str) -> None:
    """Render destination countries card content based on chosen visualization type."""
    if not data:
        Text("No country traffic recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _destinations_table(data)
    else:
        chart_data = [{"label": destination_label(item), "queries": item_queries(item)} for item in data]
        _render_chart_view(chart_data, "Queries", "#3b82f6", view_type)


def build_destinations_card(data: list[dict[str, Any]], view_type: str = "auto") -> Card:
    """Build the destination countries card."""
    return _build_analytics_card(
        "Traffic Destination",
        "Top destination countries based on DNS queries.",
        lambda: _render_destination_content(data, view_type),
    )


def assemble_overview_layout(
    kpis: dict[str, Any],
    series_chart: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None,
    resolved_data: list[dict[str, Any]],
    blocked_data: list[dict[str, Any]],
    domain_view: str = "table",
) -> Column:
    """Build the main overview cards layout from prepared data.

    ``series_chart`` is the ``(chart_data, chart_series_data)`` pair
    ``data.prepare_queries_series`` produced, or ``None`` when the series
    payload carried nothing plottable - the card is omitted then.
    """
    with Column(gap=6, css_class="p-6") as view:
        Heading("NextDNS Overview Dashboard", level=1)
        _build_kpi_row(kpis)
        if series_chart is not None:
            _build_queries_series_card(*series_chart)
        with Row(gap=4):
            build_domains_card(
                "Resolved Domains",
                "Domains that resolved without being blocked.",
                resolved_data,
                view_type=domain_view,
                color="#3b82f6",
            )
            build_domains_card(
                "Blocked Domains",
                "Domains blocked by security, privacy, or parental controls.",
                blocked_data,
                view_type=domain_view,
                color="#ef4444",
            )
    return view


def append_extra_cards(
    view: Column,
    sections: PreparedExtraSections,
    domain_view: str = "table",
    view_type: str = "auto",
) -> None:
    """Append the reasons, devices, IPs, root-domain, GAFAM, countries and security cards.

    The caller fetches and prepares; this only lays out. ``sections`` carries one
    named field per section with the aggregate math already done
    (``data.prepare_extra_sections``), so the pairing between a fetch and the card
    that renders it is carried by name rather than by position. Only the
    root-domain card is a domain card, so it alone takes the already-resolved
    ``domain_view``; the other cards take ``view_type``, which keeps ``"auto"``
    and renders each card's own default instead of being coerced to a table (#547).
    """
    with view:
        with Row(gap=4):
            build_reasons_card(sections.reasons, view_type=view_type)
            build_devices_card(sections.devices, view_type=view_type)
        with Row(gap=4):
            build_domains_card("Root Domains", "Aggregate root domains.", sections.root_domains, view_type=domain_view)
            build_ips_card(sections.ips)
        with Row(gap=4):
            build_gafam_card(sections.gafam_chart, sections.gafam_table, view_type=view_type)
            build_destinations_card(sections.countries, view_type=view_type)
        with Row(gap=4):
            build_security_card(sections.encryption_rate, sections.dnssec_rate)


def build_page_app(view: Column) -> PrefabApp:
    """Wrap an assembled dashboard view in the served Prefab app."""
    return PrefabApp(view=view, theme=DASHBOARD_THEME)


def build_kpi_app(kpis: dict[str, Any]) -> PrefabApp:
    """Build the standalone KPI page: the three metric cards, nothing else."""
    with Column(gap=4, css_class="p-6") as view:
        _build_kpi_row(kpis)
    return build_page_app(view)
