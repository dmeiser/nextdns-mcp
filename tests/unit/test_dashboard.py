"""Unit tests for the NextDNS interactive analytics dashboard tools.

Covers the dashboard helpers, render caps, fetch and validation, and tool
execution. The section/widget behaviour lives in ``test_dashboard_sections.py``.
"""

import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools.base import ToolResult
from prefab_ui.components import Card, Column, DataTableColumn

from nextdns_mcp import chart_ui as chart_ui_module
from nextdns_mcp import charts as charts_module
from nextdns_mcp.tools import dashboard as dash_module
from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.dashboard import components as ui
from nextdns_mcp.tools.dashboard import data as data_mod
from nextdns_mcp.tools.dashboard import formatters as fmt
from nextdns_mcp.tools.dashboard import tool as tool_mod
from nextdns_mcp.tools.dashboard.data import MAX_DASHBOARD_RESPONSE_BYTES
from nextdns_mcp.transport import MAX_SERIES_RESPONSE_BYTES


def _find_component(node, comp_type):
    """First component of ``comp_type`` in a builder's component tree, or None."""
    if getattr(node, "type", None) == comp_type:
        return node
    for child in getattr(node, "children", None) or []:
        found = _find_component(child, comp_type)
        if found is not None:
            return found
    return None


def _find_all_components(node, comp_type):
    """Every component of ``comp_type`` in a builder's component tree, in document order."""
    found = []
    if getattr(node, "type", None) == comp_type:
        found.append(node)
    for child in getattr(node, "children", None) or []:
        found.extend(_find_all_components(child, comp_type))
    return found


class TestDashboardHelpers:
    """Tests for unit formatting and component builder helpers."""

    def test_wrap_card_result_uses_documented_composition(self):
        """The card is the view's direct child, and wrapping never mutates the card."""
        card = Card(title="Card title")
        snapshot = card.model_dump(exclude={"id"})
        result = tool_mod._wrap_card_result(card, "Summary text")
        assert isinstance(result, ToolResult)
        assert result.content[0].text == "Summary text"
        view = result.structured_content["view"]
        (column,) = view["children"]
        assert column["type"] == "Column"
        (child,) = column["children"]
        assert child["type"] == "Card"
        # The wrap builds a fresh view around the card; the card itself is untouched.
        assert card.model_dump(exclude={"id"}) == snapshot

    def test_build_analytics_params(self):
        p = data_mod.build_analytics_params("-1d", "now", limit=5, extra_key="val", none_key=None)
        assert p["from"] == "-1d"
        assert p["to"] == "now"
        assert p["limit"] == 5
        assert p["extra_key"] == "val"
        assert "none_key" not in p

    def test_share_ratio(self):
        assert data_mod._share_ratio(25, 100) == 0.25
        assert data_mod._share_ratio(0, 0) == 0.0

    def test_item_and_blocked_queries(self):
        assert data_mod.item_queries({"queries": 42}) == 42
        assert data_mod.item_queries({}) == 0
        assert data_mod.item_queries({"queries": "abc"}) == 0
        assert data_mod.item_queries({"queries": {"nested": 1}}) == 0
        assert data_mod.item_queries({"queries": [1, 2]}) == 0
        assert data_mod.item_queries({"queries": "123"}) == 123
        assert data_mod.item_queries({"queries": float("inf")}) == 0
        assert data_mod.item_queries({"queries": float("-inf")}) == 0
        assert data_mod.item_queries({"queries": json.loads("1e400")}) == 0
        assert data_mod._blocked_queries({"status": "blocked", "queries": 10}) == 10
        assert data_mod._blocked_queries({"status": "allowed", "queries": 10}) == 0

    def test_calculate_status_kpis(self):
        data = [
            {"status": "default", "queries": 80},
            {"status": "blocked", "queries": 20},
        ]
        kpis = data_mod.calculate_status_kpis(data)
        assert kpis["total"] == 100
        assert kpis["blocked"] == 20
        assert kpis["rate"] == 20.0

        empty_kpis = data_mod.calculate_status_kpis([])
        assert empty_kpis["total"] == 0
        assert empty_kpis["rate"] == 0.0

    def test_build_kpi_row(self):
        kpis = {"total": 1000, "blocked": 100, "rate": 10.0}
        row = ui._build_kpi_row(kpis)
        assert len(row.children) == 3

    def test_build_queries_series_card(self):
        assert data_mod.prepare_queries_series({}) is None

        valid_payload = {
            "meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}},
            "data": [{"name": "blocked", "queries": [5]}],
        }
        prepared = data_mod.prepare_queries_series(valid_payload)
        assert prepared is not None
        card = ui._build_queries_series_card(*prepared)
        assert card is not None

    def test_build_queries_series_card_uses_the_shared_factory(self):
        # The dashboard's chart is exactly what the shared factory builds from
        # the same rows, so the two surfaces cannot drift apart (issue #439).
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [
            {"name": "blocked", "queries": [10, 20]},
            {"name": "allowed", "queries": [5, 8]},
        ]
        prepared = data_mod.prepare_queries_series({"meta": {"series": {"times": times}}, "data": series_data})
        assert prepared is not None
        card = ui._build_queries_series_card(*prepared)
        chart = _find_component(card, "AreaChart")
        assert chart is not None
        assert chart.model_dump(exclude={"id"}, by_alias=True) == chart_ui_module.build_timeseries_area_chart(
            charts_module.build_chart_data(times, series_data),
            chart_ui_module.build_chart_series(series_data),
        ).model_dump(exclude={"id"}, by_alias=True)

    def test_build_queries_series_card_downsamples_to_the_chart_budget(self):
        # A multi-year overview answers up to 1,999 intervals; the card must
        # render under the shared chart budget, like plotAnalytics does, or the
        # raw rows inflate every other card on the page (issue #550).
        n = 1999
        times = [f"2024-01-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z" for i in range(n)]
        series_data = [{"name": "blocked", "queries": [2] * n}]
        prepared = data_mod.prepare_queries_series({"meta": {"series": {"times": times}}, "data": series_data})
        assert prepared is not None
        card = ui._build_queries_series_card(*prepared)
        chart = _find_component(card, "AreaChart")
        assert chart is not None
        assert len(chart.data) <= charts_module.PLOT_CHART_MAX_POINTS
        assert len(chart.data) == 250  # ceil(1999 / ceil(1999/250)) = 250 buckets
        # Bucketing sums, so the rendered series still carries the upstream total.
        assert sum(row["series_0"] for row in chart.data) == 2 * n

    def test_build_domains_card(self):
        data = [{"domain": "example.com", "queries": 50}, {}]
        card = ui.build_domains_card("Title", "Desc", data)
        assert card is not None
        assert ui.build_domains_card("Title", "Desc", data, view_type="bar") is not None
        assert ui.build_domains_card("Title", "Desc", data, view_type="pie") is not None

    def test_reasons_card(self):
        data = [{"name": "List A", "queries": 50}, {"id": "list-b", "queries": 50}]
        card = ui.build_reasons_card(data)
        assert card is not None
        assert ui.build_reasons_card(data, view_type="table") is not None
        assert ui.build_reasons_card(data, view_type="pie") is not None

        empty_row = data_mod.reason_row({}, 0)
        assert empty_row["name"] == "Unknown"
        assert empty_row["share"] == 0.0

    def test_devices_card(self):
        data = [
            {"id": "__UNIDENTIFIED__", "queries": 100},
            {"id": "dev1", "name": "iPhone", "model": "16 Pro", "queries": 200},
            {"id": "dev2"},
            {},
        ]
        card = ui.build_devices_card(data)
        assert card is not None
        assert ui.build_devices_card(data, view_type="bar") is not None
        assert ui.build_devices_card(data, view_type="pie") is not None

    def test_location_string_and_ips_card(self):
        assert data_mod._location_string({"city": "City", "country": "Country"}) == "City, Country"
        assert data_mod._location_string({"city": "City"}) == "City"
        assert data_mod._location_string({"country": "Country"}) == "Country"
        assert data_mod._location_string({}) == "-"

        data = [
            {
                "ip": "1.2.3.4",
                "network": {"isp": "ISP"},
                "geo": {"city": "Paris", "country": "France"},
                "queries": 10,
            },
            {},
        ]
        card = ui.build_ips_card(data)
        assert card is not None

    def test_gafam_card(self):
        data = [{"company": "google", "queries": 100}, {}]
        chart_data, table_rows = data_mod.gafam_rows(data)
        card = ui.build_gafam_card(chart_data, table_rows)
        assert card is not None
        assert ui.build_gafam_card(chart_data, table_rows, view_type="bar") is not None
        assert ui.build_gafam_card(chart_data, table_rows, view_type="table") is not None

    def test_gafam_card_pie_view_renders_only_the_chart(self):
        # view_type="pie" must render only the chart; rendering the table too
        # was the FUNC-5 defect (WORK-324). "auto" keeps rendering both.
        data = [{"company": "google", "queries": 100}]
        chart_data, table_rows = data_mod.gafam_rows(data)
        pie_card = ui.build_gafam_card(chart_data, table_rows, view_type="pie")
        assert _find_component(pie_card, "PieChart") is not None
        assert _find_component(pie_card, "DataTable") is None
        auto_card = ui.build_gafam_card(chart_data, table_rows, view_type="auto")
        assert _find_component(auto_card, "PieChart") is not None
        assert _find_component(auto_card, "DataTable") is not None

    def test_pie_chart_clamps_negative_counts(self):
        # A negative upstream count used to raise ValidationError from the
        # pie's aggregation check (FUNC-5 / WORK-324); it clamps to zero.
        chart = ui._pie_chart([{"label": "a", "queries": -1}, {"label": "b", "queries": 20}])
        assert [row["queries"] for row in chart.data] == [0, 20]

    def test_share_cells_sort_numerically_in_every_share_table(self):
        # Issue #800: the Share cells carried pre-formatted ``"10.0%"`` strings,
        # so an ascending sort put 10.0% before 2.0%. The cells now carry the
        # ratio and the column formats it, so the sort is the numeric one.
        counts = [("alpha", 2), ("bravo", 10), ("charlie", 88)]
        _, gafam_table_rows = data_mod.gafam_rows([{"company": n, "queries": q} for n, q in counts])
        tables = {
            "reasons": ui._reasons_table([{"name": n, "queries": q} for n, q in counts]),
            "devices": ui._devices_table([{"name": n, "queries": q} for n, q in counts]),
            "gafam": ui._gafam_table(gafam_table_rows),
        }
        for name, table in tables.items():
            (share_col,) = [c for c in table.columns if c.key == "share"]
            assert share_col.sortable, name
            assert share_col.format == "percent:1", name
            rows = list(table.rows)
            assert [r["share"] for r in sorted(rows, key=lambda r: r["share"])] == [0.02, 0.1, 0.88], name

    def test_security_card(self):
        enc = [{"encrypted": True, "queries": 90}, {"encrypted": False, "queries": 10}]
        dnssec = [{"validated": True, "queries": 20}, {"validated": False, "queries": 80}]
        card = ui.build_security_card(data_mod.calc_rate(enc, "encrypted"), data_mod.calc_rate(dnssec, "validated"))
        assert card is not None

        empty_card = ui.build_security_card(data_mod.calc_rate([], "encrypted"), data_mod.calc_rate([], "validated"))
        assert empty_card is not None

    @pytest.mark.parametrize(
        ("matching", "nonmatching", "expected"),
        [(-20, 120, 0.0), (120, -20, 100.0), (50, 50, 50.0)],
    )
    def test_security_progress_stays_inside_the_bar_range(self, matching: int, nonmatching: int, expected: float):
        # Issue #800: ``calc_rate`` answers whatever the API counted, and a count
        # outside 0-100 reached ``Progress`` unclamped. The bar clamps; the text
        # still reports the rate the API gave.
        items = [{"encrypted": True, "queries": matching}, {"encrypted": False, "queries": nonmatching}]
        val, total, pct = data_mod.calc_rate(items, "encrypted")
        section = ui._build_security_section("Encrypted DNS", val, total, pct)
        (progress,) = _find_all_components(section, "Progress")
        assert progress.value == expected

    def test_destinations_card(self):
        data = [
            {"name": "United States", "code": "US", "queries": 100, "domains": ["google.com"]},
            {},
        ]
        card = ui.build_destinations_card(data)
        assert card is not None
        assert ui.build_destinations_card(data, view_type="bar") is not None
        assert ui.build_destinations_card(data, view_type="pie") is not None

    def test_accumulate_domains_budgeted(self):
        # In-budget: labels fit exactly
        assert data_mod._accumulate_domains(["a.com", "b.com"], budget=60) == "a.com, b.com"
        # Overflow: stops before exceeding budget - exact value distinguishes from old join-then-slice
        long_list = ["verylongdomainnamethatisoverlimit.example.com"] * 100
        # Old implementation would return first 60 chars with partial second label
        # New implementation stops after first label because second would exceed budget
        assert (
            data_mod.destination_row({"domains": long_list})["sample_domains"]
            == "verylongdomainnamethatisoverlimit.example.com"
        )
        # Exact boundary cases
        assert data_mod.destination_row({"domains": ["a" * 30, "b" * 40]})["sample_domains"] == "a" * 30
        assert (
            data_mod.destination_row({"domains": ["a" * 28, "b" * 30]})["sample_domains"] == "a" * 28 + ", " + "b" * 30
        )
        # Empty list returns "-"
        assert data_mod.destination_row({"domains": []})["sample_domains"] == "-"
        # Single overlong label gets truncated to budget
        over = "x" * 100
        assert data_mod.destination_row({"domains": [over]})["sample_domains"] == over[:60]


class TestDashboardRenderCaps:
    """Render caps: paginated tables, rolled-up charts, and one byte ceiling per page.

    ``limit`` bounds what an analytics endpoint answers - up to 500 rows - and
    every one of those rows used to be serialized into the returned ``PrefabApp``:
    ``section="all"`` at ``limit=500`` embedded thousands of rows in the document
    handed back to the model (issue #789). These tests pin the caps that keep the
    rendered page bounded while its totals stay the caller's.
    """

    @pytest.fixture
    def mock_fat_endpoints(self, monkeypatch):
        """Every endpoint answers 500 rows, and the series answers 100 series x 250 points."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        async def fake_fetch(pid, endpoint, params=None):
            if endpoint == "status;series":
                return (
                    {
                        "meta": {"series": {"times": [f"2024-01-01T{i:05d}" for i in range(250)]}},
                        "data": [{"name": f"dev{i}", "queries": [1] * 250} for i in range(100)],
                    },
                    None,
                )
            return (
                {
                    "data": [
                        {
                            "domain": f"sub{i:04d}.example-domain.com",
                            "name": f"Name {i}",
                            "id": f"dev{i}",
                            "model": f"Model {i}",
                            "ip": f"10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}",
                            "company": f"corp{i % 5}",
                            "code": f"C{i % 50:02d}",
                            "domains": [f"sample{i}.example.com"],
                            "queries": 500 - (i % 400),
                        }
                        for i in range(500)
                    ]
                },
                None,
            )

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)

    def test_a_large_table_is_capped_and_paginated(self):
        data = [{"domain": f"d{i:03d}.example.com", "queries": 500 - i} for i in range(500)]
        table = _find_component(ui.build_domains_card("Resolved Domains", "desc", data), "DataTable")
        assert table is not None
        assert len(table.rows) == ui.MAX_TABLE_ROWS
        assert table.paginated is True
        assert table.page_size == ui.TABLE_PAGE_SIZE
        # The busiest rows are the ones that survive the cap.
        assert table.rows[0]["domain"] == "d000.example.com"
        assert table.rows[-1]["queries"] == 500 - (ui.MAX_TABLE_ROWS - 1)

    def test_a_table_inside_the_page_size_is_not_paginated(self):
        data = [{"domain": f"d{i}.example.com", "queries": 5} for i in range(3)]
        table = _find_component(ui.build_domains_card("Resolved Domains", "desc", data), "DataTable")
        assert table is not None
        assert table.paginated is False
        assert len(table.rows) == 3

    def test_bar_and_pie_charts_roll_up_their_categories(self):
        data = [{"domain": f"d{i:03d}.example.com", "queries": 500 - i} for i in range(500)]
        total = sum(item["queries"] for item in data)
        for view_type, comp_type in (("bar", "BarChart"), ("pie", "PieChart")):
            chart = _find_component(
                ui.build_domains_card("Resolved Domains", "desc", data, view_type=view_type), comp_type
            )
            assert chart is not None
            assert len(chart.data) == charts_module.CHART_MAX_CATEGORIES
            assert chart.data[-1]["label"] == charts_module.CHART_ROLLUP_LABEL
            # The rollup carries what the chart no longer draws, so the total holds.
            assert sum(row["queries"] for row in chart.data) == total

    def test_categories_within_the_budget_come_back_untouched(self):
        rows = [{"label": f"c{i}", "queries": i} for i in range(charts_module.CHART_MAX_CATEGORIES)]
        assert charts_module.cap_chart_categories(rows) is rows

    def test_categories_are_ranked_and_a_null_count_prices_at_zero(self):
        rows = [{"label": f"c{i}", "queries": 10} for i in range(charts_module.CHART_MAX_CATEGORIES)] + [
            {"label": "null", "queries": None}
        ]
        capped = charts_module.cap_chart_categories(rows)
        assert len(capped) == charts_module.CHART_MAX_CATEGORIES
        assert capped[0]["label"] == "c0"
        # One kept-budget row and the null row are the omitted set; null adds nothing.
        assert capped[-1] == {"label": charts_module.CHART_ROLLUP_LABEL, "queries": 10}

    def test_the_queries_card_caps_its_series(self):
        payload = {
            "meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}},
            "data": [{"name": f"dev{i}", "queries": [i + 1]} for i in range(100)],
        }
        prepared = data_mod.prepare_queries_series(payload)
        assert prepared is not None
        chart = _find_component(ui._build_queries_series_card(*prepared), "AreaChart")
        assert chart is not None
        assert len(chart.series) == charts_module.CHART_MAX_SERIES
        assert chart.series[-1].label == charts_module.CHART_ROLLUP_LABEL
        assert sum(row[f"series_{i}"] for row in chart.data for i in range(charts_module.CHART_MAX_SERIES)) == sum(
            range(1, 101)
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("view_type", ["table", "bar", "pie"])
    async def test_all_sections_at_the_maximum_limit_stay_under_the_result_ceiling(self, mock_fat_endpoints, view_type):
        """The reported shape: ``section="all"``, ``limit=500``, 500 rows per card (issue #789)."""
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all", limit=500, view_type=view_type)
        assert isinstance(res, ToolResult)
        wire = json.dumps(res.structured_content, separators=(",", ":")).encode("utf-8")
        assert len(wire) <= chart_ui_module.MAX_RESULT_BYTES


class TestDashboardFetchAndValidation:
    """Tests for data validation and fetching error paths."""

    def test_validate_card_data(self):
        data, err = data_mod._validate_card_data({"data": [{"x": 1}]}, "test", "p1")
        assert err is None
        assert data == [{"x": 1}]

        data, err = data_mod._validate_card_data(None, "test", "p1")
        assert data is None
        assert err["code"] == "no_data"

        data, err = data_mod._validate_card_data({"data": []}, "test", "p1")
        assert err is None
        assert data == []

    @pytest.mark.asyncio
    async def test_fetch_card_data_error_paths(self, clean_env, monkeypatch):
        # The profile id arrives resolved: a fetch failure is the only error this helper classifies.
        # 1. Fetch error from upstream
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        upstream_err = {"code": "http_error", "error": "failed"}
        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", AsyncMock(return_value=(None, upstream_err)))
        _data, err = await data_mod.fetch_card_data("abc123", "endpoint")
        assert err == upstream_err

        # 2. Empty data flows through (no no_data error) for consistent empty-state cards
        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", AsyncMock(return_value=({"data": []}, None)))
        _data, err = await data_mod.fetch_card_data("abc123", "endpoint")
        assert err is None
        assert _data == []

    @pytest.mark.asyncio
    async def test_fetch_analytics_payload_error_classification(self, monkeypatch):
        monkeypatch.setattr(
            "nextdns_mcp.transport.api_request_payload",
            AsyncMock(return_value={"code": "http_error", "error": "upstream"}),
        )
        payload, err = await data_mod._fetch_analytics_payload("p1", "status")
        assert payload is None and err["code"] == "http_error"

        monkeypatch.setattr(
            "nextdns_mcp.transport.api_request_payload", AsyncMock(return_value={"data": [{"ok": True}]})
        )
        payload, err = await data_mod._fetch_analytics_payload("p1", "status")
        assert payload == {"data": [{"ok": True}]} and err is None

    @pytest.mark.asyncio
    async def test_fetch_analytics_payload_caps_series_and_bounded_endpoints(self, monkeypatch):
        """`;series` runs under the series ceiling, every other endpoint under the dashboard cap."""
        stub = AsyncMock(return_value={"data": []})
        monkeypatch.setattr("nextdns_mcp.transport.api_request_payload", stub)

        payload, err = await data_mod._fetch_analytics_payload("p1", "status")
        assert payload == {"data": []} and err is None
        _, kwargs = stub.call_args
        assert kwargs["max_response_bytes"] == MAX_DASHBOARD_RESPONSE_BYTES

        stub.reset_mock()
        payload, err = await data_mod._fetch_analytics_payload("p1", "status;series")
        assert payload == {"data": []} and err is None
        _, kwargs = stub.call_args
        assert kwargs["max_response_bytes"] == MAX_SERIES_RESPONSE_BYTES


class TestDashboardToolsExecution:
    """Tests executing each public tool end-to-end with mock data."""

    @pytest.fixture
    def mock_all_endpoints(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple[str, dict[str, object]]] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append((endpoint, dict(params or {})))
            if endpoint == "status":
                return ({"data": [{"status": "default", "queries": 90}, {"status": "blocked", "queries": 10}]}, None)
            if endpoint == "status;series":
                return (
                    {
                        "meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}},
                        "data": [{"name": "blocked", "queries": [10]}],
                    },
                    None,
                )
            if endpoint == "destinations":
                return (
                    {"data": [{"company": "google", "queries": 50}, {"name": "US", "code": "US", "queries": 50}]},
                    None,
                )
            if endpoint == "encryption":
                return ({"data": [{"encrypted": True, "queries": 80}, {"encrypted": False, "queries": 20}]}, None)
            if endpoint == "dnssec":
                return ({"data": [{"validated": True, "queries": 30}, {"validated": False, "queries": 70}]}, None)
            return ({"data": [{"domain": "test.com", "name": "Test", "ip": "1.1.1.1", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        return calls

    @pytest.mark.asyncio
    async def test_analytics_dashboard_overview(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(profile_id="abc123", section="overview")
        assert isinstance(res, ToolResult)
        assert "Rendered NextDNS Dashboard" in res.content[0].text
        assert "$prefab" in res.structured_content

    @pytest.mark.asyncio
    async def test_overview_dashboard_domain_view_bar(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(profile_id="abc123", domain_view="bar")
        assert isinstance(res, ToolResult)
        assert "$prefab" in res.structured_content
        # Full-tree substring is insufficient – check resolved domains explicitly
        content = str(res.structured_content)
        assert content.count("BarChart") >= 2

    @pytest.mark.asyncio
    async def test_analytics_dashboard_all_sections(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all", view_type="bar")
        assert isinstance(res, ToolResult)
        assert "Rendered NextDNS Dashboard" in res.content[0].text
        assert "$prefab" in res.structured_content
        content = str(res.structured_content)
        # view_type is the global override: overview Resolved/Blocked + Reasons/
        # Devices/Root/GAFAM/Countries all render bars.
        assert content.count("BarChart") >= 7
        # Regression guard for #409: section="all" must fetch and render the countries card.
        requested = {(endpoint, params.get("type")) for endpoint, params in mock_all_endpoints}
        assert ("destinations", "countries") in requested
        assert ("destinations", "gafam") in requested
        rendered = json.dumps(res.structured_content, default=str)
        assert "Traffic Destination" in rendered
        assert "US" in rendered

    @pytest.mark.asyncio
    async def test_overview_dashboard_all_sections_single_stage_bounded_concurrency(self, monkeypatch):
        """Assert section='all' gathers all 12 calls in one stage with max concurrency <= 4."""

        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        current_concurrency = 0
        max_concurrency = 0
        active_endpoints: list[str] = []
        started_calls: list[str] = []
        extra_started_while_overview_active: list[str] = []

        overview_endpoints = {"status", "status;series", "domains"}
        extra_endpoints = {"reasons", "devices", "ips", "destinations", "encryption", "dnssec"}

        async def fake_fetch(pid, endpoint, params=None):
            nonlocal current_concurrency, max_concurrency
            current_concurrency += 1
            max_concurrency = max(max_concurrency, current_concurrency)
            active_endpoints.append(endpoint)
            started_calls.append(endpoint)

            if endpoint in extra_endpoints and any(ep in active_endpoints for ep in overview_endpoints):
                extra_started_while_overview_active.append(endpoint)

            # status finishes first so slot frees up immediately in the single gather stage
            sleep_time = 0.005 if endpoint == "status" else 0.02
            await asyncio.sleep(sleep_time)

            active_endpoints.remove(endpoint)
            current_concurrency -= 1

            if endpoint == "status":
                return ({"data": [{"status": "default", "queries": 90}, {"status": "blocked", "queries": 10}]}, None)
            if endpoint == "status;series":
                return (
                    {
                        "meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}},
                        "data": [{"name": "blocked", "queries": [10]}],
                    },
                    None,
                )
            if endpoint == "destinations":
                return (
                    {"data": [{"company": "google", "queries": 50}, {"name": "US", "code": "US", "queries": 50}]},
                    None,
                )
            if endpoint == "encryption":
                return ({"data": [{"encrypted": True, "queries": 80}, {"encrypted": False, "queries": 20}]}, None)
            if endpoint == "dnssec":
                return ({"data": [{"validated": True, "queries": 30}, {"validated": False, "queries": 70}]}, None)
            return ({"data": [{"domain": "test.com", "name": "Test", "ip": "1.1.1.1", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)

        res = await dash_module.overviewDashboard(profile_id="abc123", section="all")
        assert isinstance(res, ToolResult)
        assert "Rendered NextDNS Dashboard" in res.content[0].text
        assert "$prefab" in res.structured_content

        # Total calls must be exactly 12 (4 overview + 8 extra, including the countries card)
        assert len(started_calls) == 12
        # Upstream concurrency must never exceed 4
        assert max_concurrency <= data_mod.OVERVIEW_FANOUT_CONCURRENCY
        assert max_concurrency == 4
        # Single gather stage: extra calls must have started while overview calls were still in flight
        assert len(extra_started_while_overview_active) > 0

    @pytest.mark.asyncio
    async def test_overview_dashboard_all_sections_status_error(self, monkeypatch):
        """Assert section='all' surfaces status error payload."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        async def fake_status_err(pid, endpoint, params=None):
            if endpoint == "status":
                return None, {"code": "http_error", "error": "status error"}
            return ({"data": []}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_status_err)
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all")
        assert isinstance(res, dict)
        assert res["code"] == "http_error"

    @pytest.mark.asyncio
    async def test_overview_dashboard_overview_concurrency_bounded(self, monkeypatch):
        """Assert section='overview' issues only 4 calls and respects concurrency limit."""

        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        current_concurrency = 0
        max_concurrency = 0
        started_calls: list[str] = []

        async def fake_fetch(pid, endpoint, params=None):
            nonlocal current_concurrency, max_concurrency
            current_concurrency += 1
            max_concurrency = max(max_concurrency, current_concurrency)
            started_calls.append(endpoint)
            await asyncio.sleep(0.01)
            current_concurrency -= 1
            if endpoint == "status":
                return ({"data": [{"status": "default", "queries": 90}]}, None)
            if endpoint == "status;series":
                return ({"meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}}, "data": []}, None)
            return ({"data": [{"domain": "test.com", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        res = await dash_module.overviewDashboard(profile_id="abc123", section="overview")
        assert isinstance(res, ToolResult)
        assert len(started_calls) == 4
        assert max_concurrency <= data_mod.OVERVIEW_FANOUT_CONCURRENCY

    @pytest.mark.asyncio
    async def test_analytics_dashboard_status_fetch_error(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        async def fake_status_err(pid, endpoint, params=None):
            if endpoint == "status":
                return None, {"code": "http_error", "error": "status error"}
            return {}, None

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_status_err)
        res = await dash_module.overviewDashboard(profile_id="abc123")
        assert isinstance(res, dict)
        assert res["code"] == "http_error"

    @pytest.mark.asyncio
    async def test_gafam_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="gafam")
        assert isinstance(res, ToolResult)
        assert "GAFAM Dominance" in res.content[0].text
        assert "$prefab" in res.structured_content

    @pytest.mark.asyncio
    async def test_gafam_analytics_error(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            data_mod, "_fetch_analytics_payload", AsyncMock(return_value=(None, {"code": "err", "error": "fail"}))
        )
        res = await dash_module.overviewDashboard(section="gafam")
        assert isinstance(res, dict)
        assert res["code"] == "err"

    @pytest.mark.asyncio
    async def test_security_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="security")
        assert isinstance(res, ToolResult)
        assert "Security Analytics" in res.content[0].text
        assert "$prefab" in res.structured_content

    @pytest.mark.asyncio
    async def test_security_analytics_error_paths(self, clean_env, monkeypatch):
        # 1. Profile error
        res = await dash_module.overviewDashboard(section="security")
        assert isinstance(res, dict)
        assert res["code"] == "missing_profile_id"

        # 2. Encryption error
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            data_mod,
            "_fetch_analytics_payload",
            AsyncMock(side_effect=[(None, {"code": "enc_err", "error": "enc"}), ({}, None)]),
        )
        res = await dash_module.overviewDashboard(section="security")
        assert res["code"] == "enc_err"

        # 3. DNSSEC error
        monkeypatch.setattr(
            data_mod,
            "_fetch_analytics_payload",
            AsyncMock(side_effect=[({}, None), (None, {"code": "sec_err", "error": "sec"})]),
        )
        res = await dash_module.overviewDashboard(section="security")
        assert res["code"] == "sec_err"

    @pytest.mark.asyncio
    async def test_reasons_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="reasons")
        assert isinstance(res, ToolResult)
        assert "Reasons for Blocking" in res.content[0].text

    @pytest.mark.asyncio
    async def test_device_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="devices")
        assert isinstance(res, ToolResult)
        assert "Device Analytics" in res.content[0].text

    @pytest.mark.asyncio
    async def test_ip_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="ips")
        assert isinstance(res, ToolResult)
        assert "IP Analytics" in res.content[0].text

    @pytest.mark.asyncio
    async def test_root_domain_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="root_domains")
        assert isinstance(res, ToolResult)
        assert "Root Domains" in res.content[0].text

    @pytest.mark.asyncio
    async def test_analytics_dashboard_profile_error(self, clean_env):
        res = await dash_module.overviewDashboard()
        assert isinstance(res, dict)
        assert res["code"] == "missing_profile_id"

    @pytest.mark.asyncio
    async def test_analytics_dashboard_empty_series(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        async def fake_empty_series(pid, endpoint, params=None):
            if endpoint == "status;series":
                return ({}, None)
            return ({"data": [{"status": "default", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_empty_series)
        res = await dash_module.overviewDashboard(profile_id="abc123")
        assert isinstance(res, ToolResult)

    @pytest.mark.asyncio
    async def test_kpi_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="kpi")
        assert isinstance(res, ToolResult)
        assert "KPI Metrics" in res.content[0].text

    @pytest.mark.asyncio
    async def test_domains_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="domains")
        assert isinstance(res, ToolResult)
        assert "Resolved Domains" in res.content[0].text

    @pytest.mark.asyncio
    async def test_blocked_domains_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="blocked_domains")
        assert isinstance(res, ToolResult)
        assert "Blocked Domains" in res.content[0].text

    @pytest.mark.asyncio
    async def test_destination_analytics(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(section="countries")
        assert isinstance(res, ToolResult)
        assert "Traffic Destinations" in res.content[0].text

    @pytest.mark.asyncio
    async def test_tools_view_type_arguments(self, mock_all_endpoints):
        assert isinstance(await dash_module.overviewDashboard(section="domains", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(section="blocked_domains", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(section="reasons", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(section="devices", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(section="root_domains", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(section="countries", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(section="gafam", view_type="bar"), ToolResult)
        assert isinstance(await dash_module.overviewDashboard(view_type="bar"), ToolResult)

    @pytest.mark.asyncio
    async def test_overview_dashboard_invalid_args(self):
        res = await dash_module.overviewDashboard(section="domain")
        assert isinstance(res, dict)
        assert res["code"] == "invalid_argument"
        res = await dash_module.overviewDashboard(view_type="bars")
        assert isinstance(res, dict)
        assert res["code"] == "invalid_argument"
        res = await dash_module.overviewDashboard(domain_view="line")
        assert isinstance(res, dict)
        assert res["code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_specialized_tools_error_paths(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            data_mod, "_fetch_analytics_payload", AsyncMock(return_value=(None, {"code": "err", "error": "fail"}))
        )

        for sec in (
            "kpi",
            "domains",
            "blocked_domains",
            "reasons",
            "devices",
            "ips",
            "root_domains",
            "countries",
        ):
            res = await dash_module.overviewDashboard(section=sec)
            assert isinstance(res, dict)
            assert res["code"] == "err"

    def test_overview_interval_and_budget(self):
        # Short span <= 2 days uses 3600s
        assert data_mod._overview_interval("-1d", "now") == 3600
        # Long span > 2 days uses 86400s
        assert data_mod._overview_interval("-30d", "now") == 86400

    @pytest.mark.asyncio
    async def test_overview_dashboard_budget_error(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        res = await dash_module.overviewDashboard(from_time=1, to_time="now")
        assert isinstance(res, dict)
        assert res["code"] == "invalid_argument"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", sorted(tool_mod._WIDGET_DISPATCH))
    async def test_widget_sections_reject_invalid_time_ranges(self, clean_env, monkeypatch, section):
        """Every single-widget section must reject invalid ranges without an upstream call."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append((endpoint, params))
            return ({"data": [{"status": "default", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        bad_ranges = [
            ("2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z"),  # inverted: end before start
            ("definitely-not-a-time", "now"),  # unresolvable relative string
        ]
        for from_time, to_time in bad_ranges:
            calls.clear()
            res = await tool_mod.overviewDashboard(
                profile_id="abc123", from_time=from_time, to_time=to_time, section=section
            )
            assert isinstance(res, dict), section
            assert res["code"] == "invalid_argument", section
            assert calls == [], section

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", sorted(tool_mod._WIDGET_DISPATCH))
    async def test_widget_sections_still_dispatch_valid_ranges(self, clean_env, monkeypatch, section):
        """Valid ranges must still dispatch to the widget after validation."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append((endpoint, params))
            if endpoint == "status":
                return ({"data": [{"status": "default", "queries": 90}, {"status": "blocked", "queries": 10}]}, None)
            if endpoint == "status;series":
                return (
                    {
                        "meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}},
                        "data": [{"name": "blocked", "queries": [10]}],
                    },
                    None,
                )
            if endpoint == "destinations":
                return (
                    {"data": [{"company": "google", "queries": 50}, {"name": "US", "code": "US", "queries": 50}]},
                    None,
                )
            if endpoint == "encryption":
                return ({"data": [{"encrypted": True, "queries": 80}, {"encrypted": False, "queries": 20}]}, None)
            if endpoint == "dnssec":
                return ({"data": [{"validated": True, "queries": 30}, {"validated": False, "queries": 70}]}, None)
            return ({"data": [{"domain": "test.com", "name": "Test", "ip": "1.1.1.1", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        res = await tool_mod.overviewDashboard(profile_id="abc123", from_time="-1d", to_time="now", section=section)
        assert calls, section
        assert isinstance(res, ToolResult), section

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", sorted(tool_mod._WIDGET_DISPATCH))
    async def test_widget_sections_accept_ranges_past_the_series_budget(self, clean_env, monkeypatch, section):
        """A ten-year window is not an error for a single widget: it never asks for ``;series``.

        A widget's rows are capped by ``limit``, so the 2000-point budget that
        bounds a series response rejected calls whose response could never
        breach it, and named an ``interval`` the widget never sends.
        """
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append((endpoint, params))
            if endpoint == "destinations":
                return ({"data": [{"company": "google", "queries": 50}]}, None)
            if endpoint == "encryption":
                return ({"data": [{"encrypted": True, "queries": 80}, {"encrypted": False, "queries": 20}]}, None)
            if endpoint == "dnssec":
                return ({"data": [{"validated": True, "queries": 30}, {"validated": False, "queries": 70}]}, None)
            return ({"data": [{"domain": "test.com", "name": "Test", "ip": "1.1.1.1", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        res = await tool_mod.overviewDashboard(profile_id="abc123", from_time="-3650d", to_time="now", section=section)
        assert calls, section
        assert isinstance(res, ToolResult), section

    @pytest.mark.asyncio
    async def test_overview_pages_keep_the_series_budget(self, clean_env, monkeypatch):
        """The sections that fetch ``status;series`` still refuse a window past the 2000-point budget."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append((endpoint, params))
            return ({"data": []}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        for section in ("overview", "all"):
            calls.clear()
            res = await tool_mod.overviewDashboard(
                profile_id="abc123", from_time="-3650d", to_time="now", section=section
            )
            assert isinstance(res, dict), section
            assert res["code"] == "invalid_argument", section
            assert "2000-point series budget" in res["error"], section
            assert calls == [], section

    def test_prepare_queries_series_exception_handling(self, monkeypatch):
        monkeypatch.setattr(data_mod, "extract_series_fields", MagicMock(side_effect=ValueError("bad data")))
        assert data_mod.prepare_queries_series({"meta": {}}) is None

    def test_prepare_queries_series_unexpected_error_propagates(self, monkeypatch):
        # Only payload-shape errors degrade to a missing card; anything else
        # is a programming error and must surface (FUNC-5 / WORK-324).
        monkeypatch.setattr(
            data_mod, "extract_series_fields", MagicMock(side_effect=RuntimeError("unexpected failure"))
        )
        with pytest.raises(RuntimeError, match="unexpected failure"):
            data_mod.prepare_queries_series({"meta": {}})

    def test_empty_cards_rendering(self):
        # Format summary with empty items
        assert fmt.format_items_summary("Test", "items", [], 0) == "[Rendered Test (0 items, 0 queries)]"
        assert fmt.format_items_summary("GAFAM Dominance", None, [], 0) == "[Rendered GAFAM Dominance (0 queries)]"

        # Empty cards render fallback text
        assert ui.build_domains_card("Domains", "desc", [], view_type="table") is not None
        assert ui.build_domains_card("Domains", "desc", [], view_type="bar") is not None
        assert ui.build_reasons_card([], view_type="table") is not None
        assert ui.build_reasons_card([], view_type="bar") is not None
        assert ui.build_devices_card([], view_type="table") is not None
        assert ui.build_devices_card([], view_type="bar") is not None
        assert ui.build_devices_card([], view_type="pie") is not None
        assert ui.build_devices_card([{"name": "iPhone", "queries": 10}], view_type="table") is not None
        assert ui.build_ips_card([]) is not None
        assert ui.build_gafam_card([], [], view_type="table") is not None
        assert ui.build_gafam_card([], [], view_type="bar") is not None
        assert ui.build_destinations_card([], view_type="table") is not None
        assert ui.build_destinations_card([], view_type="bar") is not None
        assert ui.build_destinations_card([], view_type="pie") is not None
        assert ui.build_destinations_card([{"name": "US", "queries": 10}], view_type="table") is not None
        # Interval unparseable fallback
        assert data_mod._overview_interval("invalid", "now") == 86400

    @pytest.mark.asyncio
    async def test_overview_dashboard_bounds_concurrent_fanout(self, clean_env, monkeypatch):
        """overviewDashboard(section='all') must cap concurrent upstream analytics requests."""

        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        in_flight = 0
        max_in_flight = 0
        calls: list[str] = []

        async def _tracking_fetch(pid: str, endpoint: str, params=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            calls.append(endpoint)
            try:
                await asyncio.sleep(0.01)
                if endpoint == "status;series":
                    return {"data": [{"status": "default", "queries": [1, 2]}], "meta": {"pagination": {}}}, None
                if endpoint == "status":
                    return {"data": [{"status": "default", "queries": 3}]}, None
                return {"data": [{"domain": "example.com", "queries": 1}]}, None
            finally:
                in_flight -= 1

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", _tracking_fetch)
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all")
        assert isinstance(res, ToolResult)
        assert len(calls) == 12
        assert 1 <= max_in_flight <= data_mod.OVERVIEW_FANOUT_CONCURRENCY

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", ["overview", "all", "domains", "kpi"])
    async def test_a_page_over_the_result_ceiling_is_refused(self, mock_all_endpoints, monkeypatch, section):
        """Every page surface ends at the same gate: no oversized app reaches the caller."""
        monkeypatch.setattr(chart_ui_module, "MAX_RESULT_BYTES", 1)
        res = await dash_module.overviewDashboard(profile_id="abc123", section=section, limit=500)
        assert res["code"] == "response_too_large"
        assert res["max_result_bytes"] == 1
        assert res["result_bytes"] > 1

    def test_rendered_app_bytes_counts_raw_utf8_not_ascii_escapes(self):
        """The ceiling must charge what the stdio wire carries, not ``\\uXXXX`` escapes."""
        label = "münchen-例え.jp" * 5
        rows = [{"domain": label, "queries": 100} for _ in range(100)]
        table = ui._data_table([DataTableColumn(key="domain", header="Domain")], rows)
        with Column(gap=4, css_class="p-6", children=[Card(title="Non-ASCII", children=[table])]) as view:
            pass
        app = ui.build_page_app(view)
        measured = chart_ui_module.rendered_app_bytes(app)
        wire = len(json.dumps(app.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        escaped = len(json.dumps(app.to_json(), separators=(",", ":")).encode("utf-8"))
        assert measured == wire
        assert escaped > wire

    def test_model_facing_cap_claims_match_the_renderer(self):
        """Published descriptions state the totals the caps actually render."""
        dash_doc = inspect.getdoc(dash_module.overviewDashboard) or ""
        plot_doc = inspect.getdoc(plots_module.plotAnalytics) or ""
        assert "24 busiest entries" in dash_doc
        assert "25 series including" in dash_doc
        assert "at most 25 series" in plot_doc
