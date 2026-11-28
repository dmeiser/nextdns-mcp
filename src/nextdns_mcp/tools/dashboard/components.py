"""Prefab UI builders for the dashboard tools.

Every function here answers "what does this card look like": cards, charts, tables,
progress sections, and the page-level views that hold them. It reads analytics rows
through ``data``, the shared series primitives through ``nextdns_mcp.charts`` and the
shared chart components through ``nextdns_mcp.chart_ui``; it never calls the transport
itself, and it never writes the summary text a model reads back - that is ``formatters``.

Names beginning with ``_`` are private to this module: helpers shared with
the other dashboard modules cross under public names (issue #637), and the
package's public surface is what ``__init__`` re-exports.

SPDX-License-Identifier: MIT
"""

import logging
import re
from typing import Any, NamedTuple

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
    ExpandableRow,
    Heading,
    Progress,
    Row,
    Text,
)
from prefab_ui.components.charts import BarChart, ChartSeries, PieChart

from ...chart_ui import (
    DASHBOARD_THEME,
    bounded_app_result,
    build_chart_series,
    build_timeseries_area_chart,
)
from ...charts import (
    build_chart_data,
    cap_chart_categories,
    cap_chart_series,
    downsample_chart_series,
    extract_series_fields,
)
from .data import (
    ExtraDashboardPayloads,
    calc_rate,
    destination_label,
    destination_row,
    device_label,
    device_row,
    domain_label,
    gafam_rows,
    ip_label,
    ip_row,
    item_queries,
    reason_label,
    reason_row,
)

logger = logging.getLogger(__name__)

DEFAULT_BAR_COLOR = "#3b82f6"
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

QUERIES_COLUMN = DataTableColumn(key="queries", header="Queries", sortable=True, format="number")
SHARE_COLUMN = DataTableColumn(key="share", header="Share", sortable=True)

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
    """Build one bounded, paginated dashboard table.

    Every dashboard table is built here: the row cap, the page size and the
    pagination flag are one decision shared by six cards, and spelling the
    kwargs out per card is how the six would drift apart.
    """
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


def _build_queries_series_card(series_payload: dict[str, Any]) -> Card | None:
    """Build the queries evolution chart card from a time-series payload.

    The rendered rows are downsampled through the shared chart budget: a
    multi-year overview answers up to 1,999 intervals, and this card is one of
    many on the page, so full-resolution rows here bloat the whole
    ``PrefabApp`` rather than one chart result (issue #550). The series count is
    capped by the same kind of budget before those rows are built, with the
    omitted series rolled up into one ``Other`` series (issue #789).
    """
    try:
        times, series_data = extract_series_fields(series_payload)
        if not times or not series_data:
            return None
        series_data = cap_chart_series(series_data)
        chart_times, chart_series_data = downsample_chart_series(times, series_data)
        chart_data = build_chart_data(chart_times, chart_series_data)
        chart_series = build_chart_series(chart_series_data)
    except (ValueError, TypeError, KeyError, AttributeError):
        logger.warning("Error preparing queries series data")
        return None

    with Card(css_class="w-full") as card:
        with CardHeader():
            CardTitle("Queries")
            CardDescription("Evolution of queries over time.")
        with CardContent():
            build_timeseries_area_chart(chart_data, chart_series)
    return card


def _bar_color(color: str) -> str:
    """Only full six-digit hex colors may reach Prefab's style injection."""
    if isinstance(color, str) and _HEX_COLOR.fullmatch(color):
        return color
    return DEFAULT_BAR_COLOR


def _bar_chart(chart_data: list[dict[str, Any]], label: str, color: str = DEFAULT_BAR_COLOR) -> BarChart:
    """Build a horizontal bar chart with rounded pill bars, capped to the chart budget."""
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
    zero or above here (issue FUNC-5 / WORK-324). The clamp runs before the
    render cap so a negative row cannot win a place in the top entries
    (issue #789).
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


def _domain_table(items: list[tuple[str, int]]) -> DataTable:
    """Build domain data table from the labels already shaped for the summary."""
    rows: _TableRows = [{"domain": label, "queries": queries} for label, queries in items]
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


def _render_domain_content(title: str, items: list[tuple[str, int]], view_type: str, color: str) -> None:
    """Render domain card content from the labels already shaped for the summary."""
    if not items:
        Text(f"No {title.lower()} available for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _domain_table(items)
    else:
        chart_data = [{"label": label, "queries": queries} for label, queries in items]
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
    items: list[tuple[str, int]],
    view_type: str = "table",
    color: str = DEFAULT_BAR_COLOR,
) -> Card:
    """Build a domain listing card from the labels already shaped for the summary."""
    return _build_analytics_card(
        title,
        description,
        lambda: _render_domain_content(title, items, view_type, color),
    )


def _reasons_table(items: list[tuple[str, int]]) -> DataTable:
    """Build reasons data table from the labels already shaped for the summary."""
    total = sum(queries for _, queries in items)
    rows: _TableRows = [reason_row(name, queries, total) for name, queries in items]
    return _data_table(
        [
            DataTableColumn(key="name", header="Reason / Filter", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows,
    )


def build_reasons_card(items: list[tuple[str, int]], view_type: str = "auto") -> Card:
    """Build the reasons for blocking card from the labels already shaped for the summary."""

    def _render():
        if not items:
            Text("No queries were blocked during this period.", css_class="text-sm text-muted-foreground py-4")
        elif view_type == "table":
            _reasons_table(items)
        else:
            chart_data = [{"label": name, "queries": queries} for name, queries in items]
            if view_type == "pie":
                _pie_chart(chart_data)
            else:
                _bar_chart(chart_data, "Blocked Queries", "#ef4444")

    return _build_analytics_card(
        "Reasons for Blocking",
        "Security, Privacy and/or Parental Control settings that blocked the most queries.",
        _render,
    )


def _devices_table(data: list[dict[str, Any]], items: list[tuple[str, int]]) -> DataTable:
    """Build devices data table, reusing the labels already shaped for the summary."""
    total = sum(queries for _, queries in items)
    rows: _TableRows = [device_row(item, total, label) for item, (label, _) in zip(data, items)]
    return _data_table(
        [
            DataTableColumn(key="device", header="Device", sortable=True),
            DataTableColumn(key="model", header="Model", sortable=True),
            QUERIES_COLUMN,
            SHARE_COLUMN,
        ],
        rows,
    )


def _render_device_content(data: list[dict[str, Any]], items: list[tuple[str, int]], view_type: str) -> None:
    """Render devices card content from the labels already shaped for the summary."""
    if not items:
        Text("No device queries recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _devices_table(data, items)
    else:
        chart_data = [{"label": label, "queries": queries} for label, queries in items]
        _render_chart_view(chart_data, "Queries", "#3b82f6", view_type)


def build_devices_card(data: list[dict[str, Any]], items: list[tuple[str, int]], view_type: str = "auto") -> Card:
    """Build the devices card from fetched rows and their summary labels."""
    return _build_analytics_card(
        "Devices",
        "Devices making the queries.",
        lambda: _render_device_content(data, items, view_type),
    )


def build_ips_card(data: list[dict[str, Any]], items: list[tuple[str, int]]) -> Card:
    """Build the IP addresses card; this fixed-format card is always a table, with labels reused from the summary."""
    rows: _TableRows = [ip_row(item, label) for item, (label, _) in zip(data, items)]
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("IPs")
            CardDescription("IP addresses making the queries.")
        with CardContent():
            if not data:
                Text("No IP queries recorded for this period.", css_class="text-sm text-muted-foreground py-4")
            else:
                _data_table(
                    [
                        DataTableColumn(key="ip", header="IP Address", sortable=True),
                        DataTableColumn(key="isp", header="ISP", sortable=True),
                        DataTableColumn(key="location", header="Location", sortable=True),
                        QUERIES_COLUMN,
                    ],
                    rows,
                )
    return card


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


def build_gafam_card(
    chart_data: list[dict[str, Any]],
    table_rows: list[dict[str, Any]],
    view_type: str = "auto",
) -> Card:
    """Build the GAFAM dominance donut chart and table card from prefetched rows."""
    # ``chart_data`` holds one row per upstream item, so an empty chart means empty data.
    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("GAFAM Dominance")
            CardDescription(
                "Traffic share of dominant internet companies (Google, Apple, Facebook, Amazon, Microsoft)."
            )
        with CardContent():
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
    return card


def _build_security_section(title: str, val: int, total: int, pct: float) -> Column:
    """Build a progress-bar section for one security indicator."""
    with Column(gap=2) as col:
        Text(f"{title}: {pct:.2f}%", css_class="font-semibold")
        Progress(value=pct)
        Text(f"{val:,} / {total:,} queries", css_class="text-sm text-muted-foreground")
    return col


def build_security_card(encryption_rate: tuple[int, int, float], dnssec_rate: tuple[int, int, float]) -> Card:
    """Build the security card from the encrypted DNS and DNSSEC rate tuples."""
    enc_val, enc_total, enc_pct = encryption_rate
    dnssec_val, dnssec_total, dnssec_pct = dnssec_rate

    with Card(css_class="flex-1 min-w-[300px]") as card:
        with CardHeader():
            CardTitle("Security & Privacy")
            CardDescription("Encryption and DNSSEC validation rates.")
        with CardContent(), Column(gap=4):
            _build_security_section("Encrypted DNS", enc_val, enc_total, enc_pct)
            _build_security_section("DNSSEC Validation", dnssec_val, dnssec_total, dnssec_pct)
    return card


def _destinations_table(data: list[dict[str, Any]], items: list[tuple[str, int]]) -> DataTable:
    """Build destinations table, reusing the labels already shaped for the summary."""
    rows: _TableRows = [destination_row(item, label) for item, (label, _) in zip(data, items)]
    return _data_table(
        [
            DataTableColumn(key="country", header="Country", sortable=True),
            DataTableColumn(key="code", header="Code", sortable=True),
            QUERIES_COLUMN,
            DataTableColumn(key="sample_domains", header="Sample Domains"),
        ],
        rows,
    )


def _render_destination_content(data: list[dict[str, Any]], items: list[tuple[str, int]], view_type: str) -> None:
    """Render destination countries card content from the labels already shaped for the summary."""
    if not items:
        Text("No country traffic recorded for this period.", css_class="text-sm text-muted-foreground py-4")
    elif view_type in ("table", "auto"):
        _destinations_table(data, items)
    else:
        chart_data = [{"label": label, "queries": queries} for label, queries in items]
        _render_chart_view(chart_data, "Queries", "#3b82f6", view_type)


def build_destinations_card(data: list[dict[str, Any]], items: list[tuple[str, int]], view_type: str = "auto") -> Card:
    """Build the destination countries card from fetched rows and their summary labels."""
    return _build_analytics_card(
        "Traffic Destination",
        "Top destination countries based on DNS queries.",
        lambda: _render_destination_content(data, items, view_type),
    )


def assemble_overview_layout(
    kpis: dict[str, Any],
    series_payload: dict[str, Any],
    resolved_items: list[tuple[str, int]],
    blocked_items: list[tuple[str, int]],
    domain_view: str = "table",
) -> Column:
    """Build the main overview cards layout from the labels already shaped for the summary."""
    with Column(gap=6, css_class="p-6") as view:
        Heading("NextDNS Overview Dashboard", level=1)
        _build_kpi_row(kpis)
        _build_queries_series_card(series_payload)
        with Row(gap=4):
            build_domains_card(
                "Resolved Domains",
                "Domains that resolved without being blocked.",
                resolved_items,
                view_type=domain_view,
                color="#3b82f6",
            )
            build_domains_card(
                "Blocked Domains",
                "Domains blocked by security, privacy, or parental controls.",
                blocked_items,
                view_type=domain_view,
                color="#ef4444",
            )
    return view


class _ExtraItems(NamedTuple):
    reasons: list[tuple[str, int]]
    devices: list[tuple[str, int]]
    ips: list[tuple[str, int]]
    countries: list[tuple[str, int]]


def _extra_items(payloads: ExtraDashboardPayloads) -> _ExtraItems:
    """Shape extra-section labels once, for the cards that render them."""
    return _ExtraItems(
        reasons=[(reason_label(item), item_queries(item)) for item in payloads.reasons],
        devices=[(device_label(item), item_queries(item)) for item in payloads.devices],
        ips=[(ip_label(item), item_queries(item)) for item in payloads.ips],
        countries=[(destination_label(item), item_queries(item)) for item in payloads.countries],
    )


def append_extra_cards(
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
    items = _extra_items(payloads)
    with view:
        with Row(gap=4):
            build_reasons_card(items.reasons, view_type=view_type)
            build_devices_card(payloads.devices, items.devices, view_type=view_type)
        with Row(gap=4):
            root_items = [(domain_label(item), item_queries(item)) for item in payloads.root_domains]
            build_domains_card("Root Domains", "Aggregate root domains.", root_items, view_type=domain_view)
            build_ips_card(payloads.ips, items.ips)
        with Row(gap=4):
            gafam_chart, gafam_table = gafam_rows(payloads.gafam)
            build_gafam_card(gafam_chart, gafam_table, view_type=view_type)
            build_destinations_card(payloads.countries, items.countries, view_type=view_type)
        with Row(gap=4):
            build_security_card(
                calc_rate(payloads.encryption, "encrypted"),
                calc_rate(payloads.dnssec, "validated"),
            )


def build_page_app(view: Column) -> PrefabApp:
    """Wrap an assembled dashboard view in the served Prefab app."""
    return PrefabApp(view=view, theme=DASHBOARD_THEME)


def build_kpi_app(kpis: dict[str, Any]) -> PrefabApp:
    """Build the standalone KPI page: the three metric cards, nothing else."""
    with Column(gap=4, css_class="p-6") as view:
        _build_kpi_row(kpis)
    return build_page_app(view)


def wrap_card_result(card: Card, summary_text: str) -> ToolResult | dict[str, Any]:
    """Wrap a single visual card into a ToolResult, or refuse it over the result ceiling."""
    with Column(gap=4, css_class="p-6", children=[card]) as view:
        pass
    return bounded_app_result(build_page_app(view), summary_text, "dashboard card")
