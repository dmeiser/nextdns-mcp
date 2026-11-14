"""Unit tests for NextDNS interactive analytics dashboard and specialized UI tools."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools.base import ToolResult

from nextdns_mcp.tools import dashboard as dash_module
from nextdns_mcp.transport import MAX_SERIES_RESPONSE_BYTES


class TestDashboardHelpers:
    """Tests for unit formatting and component builder helpers."""

    def test_wrap_card_result_uses_documented_composition(self):
        import inspect

        src = inspect.getsource(dash_module._wrap_card_result)
        assert "children.append" not in src
        assert "children=[card]" in src

    def test_time_params(self):
        assert dash_module._time_params(None, None) == {}
        p = dash_module._time_params("-7d", "now")
        assert p["from"] == "-7d" and p["to"] == "now"

    def test_build_analytics_params(self):
        p = dash_module._build_analytics_params("-1d", "now", limit=5, extra_key="val", none_key=None)
        assert p["from"] == "-1d"
        assert p["to"] == "now"
        assert p["limit"] == 5
        assert p["extra_key"] == "val"
        assert "none_key" not in p

    def test_format_share(self):
        assert dash_module._format_share(25, 100) == "25.0%"
        assert dash_module._format_share(0, 0) == "0.0%"

    def test_item_and_blocked_queries(self):
        assert dash_module._item_queries({"queries": 42}) == 42
        assert dash_module._item_queries({}) == 0
        assert dash_module._item_queries({"queries": "abc"}) == 0
        assert dash_module._item_queries({"queries": {"nested": 1}}) == 0
        assert dash_module._item_queries({"queries": [1, 2]}) == 0
        assert dash_module._item_queries({"queries": "123"}) == 123
        assert dash_module._blocked_queries({"status": "blocked", "queries": 10}) == 10
        assert dash_module._blocked_queries({"status": "allowed", "queries": 10}) == 0

    def test_calculate_status_kpis(self):
        data = [
            {"status": "default", "queries": 80},
            {"status": "blocked", "queries": 20},
        ]
        kpis = dash_module._calculate_status_kpis(data)
        assert kpis["total"] == 100
        assert kpis["blocked"] == 20
        assert kpis["rate"] == 20.0

        empty_kpis = dash_module._calculate_status_kpis([])
        assert empty_kpis["total"] == 0
        assert empty_kpis["rate"] == 0.0

    def test_build_kpi_row(self):
        kpis = {"total": 1000, "blocked": 100, "rate": 10.0}
        row = dash_module._build_kpi_row(kpis)
        assert len(row.children) == 3

    def test_build_queries_series_card(self):
        empty_card = dash_module._build_queries_series_card({})
        assert empty_card is None

        valid_payload = {
            "meta": {"series": {"times": ["2024-01-01T00:00:00Z"]}},
            "data": [{"name": "blocked", "queries": [5]}],
        }
        card = dash_module._build_queries_series_card(valid_payload)
        assert card is not None

    def test_build_domains_card(self):
        data = [{"domain": "example.com", "queries": 50}, {}]
        card = dash_module._build_domains_card("Title", "Desc", data)
        assert card is not None
        assert dash_module._build_domains_card("Title", "Desc", data, view_type="bar") is not None
        assert dash_module._build_domains_card("Title", "Desc", data, view_type="pie") is not None

    def test_reasons_card(self):
        data = [{"name": "List A", "queries": 50}, {"id": "list-b", "queries": 50}]
        card = dash_module._build_reasons_card(data)
        assert card is not None
        assert dash_module._build_reasons_card(data, view_type="table") is not None
        assert dash_module._build_reasons_card(data, view_type="pie") is not None

        empty_row = dash_module._reason_row({}, 0)
        assert empty_row["name"] == "Unknown"
        assert empty_row["share"] == "0.0%"

    def test_devices_card(self):
        data = [
            {"id": "__UNIDENTIFIED__", "queries": 100},
            {"id": "dev1", "name": "iPhone", "model": "16 Pro", "queries": 200},
            {"id": "dev2"},
            {},
        ]
        card = dash_module._build_devices_card(data)
        assert card is not None
        assert dash_module._build_devices_card(data, view_type="bar") is not None
        assert dash_module._build_devices_card(data, view_type="pie") is not None

    def test_location_string_and_ips_card(self):
        assert dash_module._location_string({"city": "City", "country": "Country"}) == "City, Country"
        assert dash_module._location_string({"city": "City"}) == "City"
        assert dash_module._location_string({"country": "Country"}) == "Country"
        assert dash_module._location_string({}) == "-"

        data = [
            {
                "ip": "1.2.3.4",
                "network": {"isp": "ISP"},
                "geo": {"city": "Paris", "country": "France"},
                "queries": 10,
            },
            {},
        ]
        card = dash_module._build_ips_card(data)
        assert card is not None

    def test_gafam_card(self):
        data = [{"company": "google", "queries": 100}, {}]
        card = dash_module._build_gafam_card(data)
        assert card is not None
        assert dash_module._build_gafam_card(data, view_type="bar") is not None
        assert dash_module._build_gafam_card(data, view_type="table") is not None

    def test_security_card(self):
        enc = [{"encrypted": True, "queries": 90}, {"encrypted": False, "queries": 10}]
        dnssec = [{"validated": True, "queries": 20}, {"validated": False, "queries": 80}]
        card = dash_module._build_security_card(enc, dnssec)
        assert card is not None

        empty_card = dash_module._build_security_card([], [])
        assert empty_card is not None

    def test_destinations_card(self):
        data = [
            {"name": "United States", "code": "US", "queries": 100, "domains": ["google.com"]},
            {},
        ]
        card = dash_module._build_destinations_card(data)
        assert card is not None
        assert dash_module._build_destinations_card(data, view_type="bar") is not None
        assert dash_module._build_destinations_card(data, view_type="pie") is not None

    def test_accumulate_domains_budgeted(self):
        # In-budget: labels fit exactly
        assert dash_module._accumulate_domains(["a.com", "b.com"], budget=60) == "a.com, b.com"
        # Overflow: stops before exceeding budget - exact value distinguishes from old join-then-slice
        long_list = ["verylongdomainnamethatisoverlimit.example.com"] * 100
        # Old implementation would return first 60 chars with partial second label
        # New implementation stops after first label because second would exceed budget
        assert dash_module._destination_row({"domains": long_list})["sample_domains"] == "verylongdomainnamethatisoverlimit.example.com"
        # Exact boundary cases
        assert dash_module._destination_row({"domains": ["a" * 30, "b" * 40]})["sample_domains"] == "a" * 30
        assert dash_module._destination_row({"domains": ["a" * 28, "b" * 30]})["sample_domains"] == "a" * 28 + ", " + "b" * 30
        # Empty list returns "-"
        assert dash_module._destination_row({"domains": []})["sample_domains"] == "-"
        # Single overlong label gets truncated to budget
        over = "x" * 100
        assert dash_module._destination_row({"domains": [over]})["sample_domains"] == over[:60]


class TestDashboardFetchAndValidation:
    """Tests for data validation and fetching error paths."""

    @pytest.mark.asyncio
    async def test_resolve_profile_errors(self, clean_env, monkeypatch):
        _res, err = await dash_module._resolve_profile(None)
        assert err is not None
        assert "No profile_id provided" in err["error"]

        monkeypatch.setattr(dash_module, "resolve_profile_id", lambda *a, **kw: (None, None))
        _res2, err = await dash_module._resolve_profile("any")
        assert err is not None
        assert err["code"] == "internal_error"

    def test_validate_card_data(self):
        data, err = dash_module._validate_card_data({"data": [{"x": 1}]}, "test", "p1")
        assert err is None
        assert data == [{"x": 1}]

        data, err = dash_module._validate_card_data(None, "test", "p1")
        assert data is None
        assert err["code"] == "no_data"

        data, err = dash_module._validate_card_data({"data": []}, "test", "p1")
        assert err is None
        assert data == []

    @pytest.mark.asyncio
    async def test_fetch_card_data_error_paths(self, clean_env, monkeypatch):
        # 1. Profile resolution error
        _pid0, _data0, err = await dash_module._fetch_card_data(None, "endpoint")
        assert err is not None

        # 2. Fetch error from upstream
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        upstream_err = {"code": "http_error", "error": "failed"}
        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", AsyncMock(return_value=(None, upstream_err)))
        _pid, _data, err = await dash_module._fetch_card_data("abc123", "endpoint")
        assert err == upstream_err

        # 3. Empty data flows through (no no_data error) for consistent empty-state cards
        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", AsyncMock(return_value=({"data": []}, None)))
        _pid, _data, err = await dash_module._fetch_card_data("abc123", "endpoint")
        assert err is None
        assert _data == []

    @pytest.mark.asyncio
    async def test_fetch_analytics_payload_error_classification(self, monkeypatch):
        monkeypatch.setattr(
            "nextdns_mcp.transport._api_request_payload",
            AsyncMock(return_value={"code": "http_error", "error": "upstream"}),
        )
        payload, err = await dash_module._fetch_analytics_payload("p1", "status")
        assert payload is None and err["code"] == "http_error"

        monkeypatch.setattr(
            "nextdns_mcp.transport._api_request_payload", AsyncMock(return_value={"data": [{"ok": True}]})
        )
        payload, err = await dash_module._fetch_analytics_payload("p1", "status")
        assert payload == {"data": [{"ok": True}]} and err is None

    @pytest.mark.asyncio
    async def test_fetch_analytics_payload_series_cap_only_for_series(self, monkeypatch):
        stub = AsyncMock(return_value={"data": []})
        monkeypatch.setattr("nextdns_mcp.transport._api_request_payload", stub)

        payload, err = await dash_module._fetch_analytics_payload("p1", "status")
        assert payload == {"data": []} and err is None
        _, kwargs = stub.call_args
        assert kwargs["max_response_bytes"] is None

        stub.reset_mock()
        payload, err = await dash_module._fetch_analytics_payload("p1", "status;series")
        assert payload == {"data": []} and err is None
        _, kwargs = stub.call_args
        assert kwargs["max_response_bytes"] == MAX_SERIES_RESPONSE_BYTES

    @pytest.mark.asyncio
    async def test_fetch_extra_payloads_binds_fields_by_name(self, monkeypatch):
        """Each named field carries its own endpoint's payload (WORK-140 oracle)."""
        tagged = {
            "reasons": [{"tag": "reasons"}],
            "devices": [{"tag": "devices"}],
            "ips": [{"tag": "ips"}],
            "domains": [{"tag": "root_domains"}],
            "destinations": [{"tag": "gafam"}],
            "encryption": [{"tag": "encryption"}],
            "dnssec": [{"tag": "dnssec"}],
        }

        async def fake_fetch(profile_id, endpoint, params=None):
            return {"data": tagged[endpoint]}, None

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_fetch)
        payloads = await dash_module._fetch_extra_payloads("p1", {"limit": 1}, {})
        assert isinstance(payloads, dash_module.ExtraDashboardPayloads)
        assert payloads.reasons == [{"tag": "reasons"}]
        assert payloads.devices == [{"tag": "devices"}]
        assert payloads.ips == [{"tag": "ips"}]
        assert payloads.root_domains == [{"tag": "root_domains"}]
        assert payloads.gafam == [{"tag": "gafam"}]
        assert payloads.encryption == [{"tag": "encryption"}]
        assert payloads.dnssec == [{"tag": "dnssec"}]


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

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_fetch)
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
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all", domain_view="bar")
        assert isinstance(res, ToolResult)
        assert "Rendered NextDNS Dashboard" in res.content[0].text
        assert "$prefab" in res.structured_content
        content = str(res.structured_content)
        # At least 6 BarCharts: overview Resolved/Blocked + Reasons/Devices/Root/GAFAM
        assert content.count("BarChart") >= 6
        # Regression guard for #409: section="all" must fetch and render the countries card.
        requested = {(endpoint, params.get("type")) for endpoint, params in mock_all_endpoints}
        assert ("destinations", "countries") in requested
        assert ("destinations", "gafam") in requested
        rendered = json.dumps(res.structured_content, default=str)
        assert "Traffic Destination" in rendered
        assert "US" in rendered

    @pytest.mark.asyncio
    async def test_analytics_dashboard_status_fetch_error(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        async def fake_status_err(pid, endpoint, params=None):
            if endpoint == "status":
                return None, {"code": "http_error", "error": "status error"}
            return {}, None

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_status_err)
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
            dash_module, "_fetch_analytics_payload", AsyncMock(return_value=(None, {"code": "err", "error": "fail"}))
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
            dash_module,
            "_fetch_analytics_payload",
            AsyncMock(side_effect=[(None, {"code": "enc_err", "error": "enc"}), ({}, None)]),
        )
        res = await dash_module.overviewDashboard(section="security")
        assert res["code"] == "enc_err"

        # 3. DNSSEC error
        monkeypatch.setattr(
            dash_module,
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

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_empty_series)
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
    async def test_specialized_tools_error_paths(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            dash_module, "_fetch_analytics_payload", AsyncMock(return_value=(None, {"code": "err", "error": "fail"}))
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
        assert dash_module._overview_interval("-1d", "now") == 3600
        # Long span > 2 days uses 86400s
        assert dash_module._overview_interval("-30d", "now") == 86400

    @pytest.mark.asyncio
    async def test_overview_dashboard_budget_error(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        res = await dash_module.overviewDashboard(from_time=1, to_time="now")
        assert isinstance(res, dict)
        assert res["code"] == "invalid_argument"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", sorted(dash_module._WIDGET_DISPATCH))
    async def test_widget_sections_reject_invalid_time_ranges(self, clean_env, monkeypatch, section):
        """Every single-widget section must reject invalid ranges without an upstream call."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append((endpoint, params))
            return ({"data": [{"status": "default", "queries": 10}]}, None)

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_fetch)
        bad_ranges = [
            ("2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z"),  # inverted: end before start
            ("definitely-not-a-time", "now"),  # unresolvable relative string
            ("-3650d", "now"),  # over the series point budget
        ]
        for from_time, to_time in bad_ranges:
            calls.clear()
            res = await dash_module.overviewDashboard(
                profile_id="abc123", from_time=from_time, to_time=to_time, section=section
            )
            assert isinstance(res, dict), section
            assert res["code"] == "invalid_argument", section
            assert calls == [], section

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", sorted(dash_module._WIDGET_DISPATCH))
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

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_fetch)
        res = await dash_module.overviewDashboard(profile_id="abc123", from_time="-1d", to_time="now", section=section)
        assert calls, section
        assert isinstance(res, ToolResult), section

    def test_build_queries_series_card_exception_handling(self, monkeypatch):
        monkeypatch.setattr(dash_module, "_validate_series_payload", MagicMock(side_effect=ValueError("bad data")))
        assert dash_module._build_queries_series_card({"meta": {}}) is None

    def test_empty_cards_rendering(self):
        # Format summary with empty items
        assert dash_module._format_items_summary("Test", "items", [], 0) == "[Rendered Test (0 items, 0 queries)]"
        assert dash_module._format_gafam_summary([], 0) == "[Rendered GAFAM Dominance (0 queries)]"

        # Empty cards render fallback text
        assert dash_module._build_domains_card("Domains", "desc", [], view_type="table") is not None
        assert dash_module._build_domains_card("Domains", "desc", [], view_type="bar") is not None
        assert dash_module._build_reasons_card([], view_type="table") is not None
        assert dash_module._build_reasons_card([], view_type="bar") is not None
        assert dash_module._build_devices_card([], view_type="table") is not None
        assert dash_module._build_devices_card([], view_type="bar") is not None
        assert dash_module._build_devices_card([], view_type="pie") is not None
        assert dash_module._build_devices_card([{"name": "iPhone", "queries": 10}], view_type="table") is not None
        assert dash_module._build_ips_card([]) is not None
        assert dash_module._build_gafam_card([], view_type="table") is not None
        assert dash_module._build_gafam_card([], view_type="bar") is not None
        assert dash_module._build_destinations_card([], view_type="table") is not None
        assert dash_module._build_destinations_card([], view_type="bar") is not None
        assert dash_module._build_destinations_card([], view_type="pie") is not None
        assert dash_module._build_destinations_card([{"name": "US", "queries": 10}], view_type="table") is not None
        # Interval unparseable fallback
        assert dash_module._overview_interval("invalid", "now") == 86400

    @pytest.mark.asyncio
    async def test_overview_dashboard_bounds_concurrent_fanout(self, clean_env, monkeypatch):
        """overviewDashboard(section='all') must cap concurrent upstream analytics requests."""
        import asyncio

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

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", _tracking_fetch)
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all")
        assert isinstance(res, ToolResult)
        assert len(calls) == 12
        assert 1 <= max_in_flight <= dash_module.OVERVIEW_FANOUT_CONCURRENCY


class TestFailClosedNoneGuards:
    """Request paths fail closed on ``(None, None)`` instead of narrowing with ``assert``.

    ``python -O`` strips asserts, so a bare ``assert`` on the profile id or the
    fetched card data turns into an unhandled ``TypeError``/network round-trip
    instead of the typed ``internal_error`` payload (fail-closed contract in
    ``AGENT.md``; convention from ``tests/unit/test_plotting.py``).
    """

    CARD_SECTIONS = (
        "kpi",
        "domains",
        "blocked_domains",
        "reasons",
        "devices",
        "ips",
        "root_domains",
        "countries",
        "gafam",
    )

    @pytest.mark.asyncio
    async def test_resolver_none_is_internal_error_and_skips_fetch(self, clean_env, monkeypatch):
        """``(None, None)`` from the resolver must not build ``/profiles/None/...`` URLs."""
        monkeypatch.setattr(dash_module, "_resolve_profile", AsyncMock(return_value=(None, None)))

        wrapped = AsyncMock(return_value=({}, None))
        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", wrapped)

        res = await dash_module.overviewDashboard(profile_id="abc123", section="security")
        assert isinstance(res, dict)
        assert res["code"] == "internal_error"
        assert "no id" in res["error"].lower()
        wrapped.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_overview_resolver_none_is_internal_error(self, clean_env, monkeypatch):
        monkeypatch.setattr(dash_module, "_resolve_profile", AsyncMock(return_value=(None, None)))

        res = await dash_module.overviewDashboard(profile_id="abc123", section="overview")
        assert isinstance(res, dict)
        assert res["code"] == "internal_error"
        assert "no id" in res["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("section", CARD_SECTIONS)
    async def test_card_fetch_none_is_internal_error(self, clean_env, monkeypatch, section):
        """``(None, None, None)`` from the card fetcher must not reach the renderer."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(dash_module, "_fetch_card_data", AsyncMock(return_value=(None, None, None)))

        res = await dash_module.overviewDashboard(section=section)
        assert isinstance(res, dict)
        assert res["code"] == "internal_error"
        assert "no data" in res["error"].lower()


class TestWidgetDispatchTypingContract:
    """Regression tests verifying _WIDGET_DISPATCH is typed and mypy guards handler calls."""

    def test_widget_dispatch_runtime_annotations_and_signatures(self):
        """Verify _WIDGET_DISPATCH is annotated with _WidgetHandler and handlers match signature."""
        import inspect
        from typing import get_type_hints

        hints = get_type_hints(dash_module)
        assert "_WIDGET_DISPATCH" in hints
        assert hints["_WIDGET_DISPATCH"] == dict[str, dash_module._WidgetHandler]

        expected_params = ["profile_id", "from_time", "to_time", "limit", "view_type"]
        for section, handler in dash_module._WIDGET_DISPATCH.items():
            sig = inspect.signature(handler)
            assert list(sig.parameters.keys()) == expected_params, (
                f"Handler for section '{section}' does not match expected parameters {expected_params}"
            )

    def test_widget_dispatch_mypy_catches_signature_and_arity_mismatches(self, tmp_path):
        """Verify mypy enforces _WidgetHandler call site and dispatch table type checks."""
        import mypy.api

        # 1. Valid call and valid handler definition must pass mypy
        valid_code = """
from nextdns_mcp.tools.dashboard import _WIDGET_DISPATCH

async def call_valid() -> None:
    await _WIDGET_DISPATCH["kpi"]("p123", "-7d", "now", 5, "auto")
"""
        valid_file = tmp_path / "valid.py"
        valid_file.write_text(valid_code)
        stdout, stderr, exit_status = mypy.api.run(["--config-file", "", str(valid_file)])
        assert exit_status == 0, f"Valid call failed mypy:\n{stdout}\n{stderr}"

        # 2. Too many positional arguments at the call site must fail mypy
        extra_arg_code = """
from nextdns_mcp.tools.dashboard import _WIDGET_DISPATCH

async def call_extra() -> None:
    await _WIDGET_DISPATCH["kpi"]("p123", "-7d", "now", 5, "auto", "extra_6th_arg")
"""
        extra_arg_file = tmp_path / "extra_arg.py"
        extra_arg_file.write_text(extra_arg_code)
        stdout, stderr, exit_status = mypy.api.run(["--config-file", "", str(extra_arg_file)])
        assert exit_status != 0, "mypy unexpectedly passed call with 6 positional arguments"
        assert 'Too many arguments for "__call__" of "_WidgetHandler"' in stdout

        # 3. Mismatched handler parameter names in dispatch table must fail mypy
        bad_param_code = """
from typing import Any, Literal
from fastmcp.tools.base import ToolResult
from nextdns_mcp.coercion import OptionalProfileId
from nextdns_mcp.tools.dashboard import _WidgetHandler

async def bad_param_handler(
    profile_id: OptionalProfileId = None,
    fromtime: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return {}

table: dict[str, _WidgetHandler] = {
    "kpi": bad_param_handler,
}
"""
        bad_param_file = tmp_path / "bad_param.py"
        bad_param_file.write_text(bad_param_code)
        stdout, stderr, exit_status = mypy.api.run(["--config-file", "", str(bad_param_file)])
        assert exit_status != 0, "mypy unexpectedly accepted handler with renamed parameter"
        assert "expected" in stdout and "_WidgetHandler" in stdout

        # 4. Incompatible return type in dispatch table must fail mypy
        bad_ret_code = """
from typing import Literal
from nextdns_mcp.coercion import OptionalProfileId
from nextdns_mcp.tools.dashboard import _WidgetHandler

async def bad_return_handler(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> int:
    return 123

table: dict[str, _WidgetHandler] = {
    "security": bad_return_handler,
}
"""
        bad_ret_file = tmp_path / "bad_ret.py"
        bad_ret_file.write_text(bad_ret_code)
        stdout, stderr, exit_status = mypy.api.run(["--config-file", "", str(bad_ret_file)])
        assert exit_status != 0, "mypy unexpectedly accepted handler returning int"
        assert "expected" in stdout and "_WidgetHandler" in stdout


class TestDashboardParamForwarding:
    """The fake must see the parameters, not just the payloads.

    A section that drops or misroutes ``limit``, the time window, ``status``,
    ``root`` or ``type`` used to stay green: the fakes recorded payloads but
    never the outgoing params. These tests assert the requests the fake
    actually received, per section, plus the component types each section
    emits.
    """

    @pytest.fixture
    def recorded(self, monkeypatch):
        """Record ``(endpoint, params)`` per fetch and answer every endpoint."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[tuple[str, dict]] = []

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
            return ({"data": [{"domain": "test.com", "queries": 10}]}, None)

        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", fake_fetch)
        return calls

    @staticmethod
    def _by_endpoint(calls):
        out: dict[str, list[dict]] = {}
        for endpoint, params in calls:
            out.setdefault(endpoint, []).append(params)
        return out

    @staticmethod
    def _component_types(res):
        """Every prefab component type in the structured view tree."""
        types: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                node_type = node.get("type")
                if isinstance(node_type, str):
                    types.add(node_type)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(res.structured_content)
        return types

    @pytest.mark.asyncio
    async def test_overview_sends_the_window_and_the_status_split(self, recorded):
        res = await dash_module.overviewDashboard(profile_id="abc123", from_time="-1d", to_time="now", limit=25)
        assert isinstance(res, ToolResult)
        by_ep = self._by_endpoint(recorded)
        assert set(by_ep) == {"status", "status;series", "domains"}
        assert by_ep["status"] == [{"from": "-1d", "to": "now"}]
        assert by_ep["status;series"] == [{"from": "-1d", "to": "now", "interval": 3600}]
        assert by_ep["domains"] == [
            {"from": "-1d", "to": "now", "limit": 25, "status": "default"},
            {"from": "-1d", "to": "now", "limit": 25, "status": "blocked"},
        ]

    @pytest.mark.asyncio
    async def test_all_sends_every_extra_section_its_own_params(self, recorded):
        res = await dash_module.overviewDashboard(
            profile_id="abc123", from_time="-1d", to_time="now", limit=25, section="all"
        )
        assert isinstance(res, ToolResult)
        by_ep = self._by_endpoint(recorded)
        window = {"from": "-1d", "to": "now"}
        assert by_ep["reasons"] == [{**window, "limit": 25}]
        assert by_ep["devices"] == [{**window, "limit": 25}]
        assert by_ep["ips"] == [{**window, "limit": 25}]
        assert by_ep["domains"] == [
            {**window, "limit": 25, "status": "default"},
            {**window, "limit": 25, "status": "blocked"},
            {**window, "limit": 25, "root": "true"},
        ]
        assert by_ep["destinations"] == [
            {**window, "type": "gafam"},
            {**window, "type": "countries"},
        ]
        assert by_ep["encryption"] == [dict(window)]
        assert by_ep["dnssec"] == [dict(window)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("section", "endpoint", "params"),
        [
            ("kpi", "status", {"from": "-1d", "to": "now"}),
            ("domains", "domains", {"from": "-1d", "to": "now", "limit": 25, "status": "default"}),
            ("blocked_domains", "domains", {"from": "-1d", "to": "now", "limit": 25, "status": "blocked"}),
            ("reasons", "reasons", {"from": "-1d", "to": "now", "limit": 25}),
            ("devices", "devices", {"from": "-1d", "to": "now", "limit": 25}),
            ("ips", "ips", {"from": "-1d", "to": "now", "limit": 25}),
            ("root_domains", "domains", {"from": "-1d", "to": "now", "limit": 25, "root": "true"}),
            ("countries", "destinations", {"from": "-1d", "to": "now", "limit": 25, "type": "countries"}),
            ("gafam", "destinations", {"from": "-1d", "to": "now", "type": "gafam"}),
        ],
        ids=["kpi", "domains", "blocked_domains", "reasons", "devices", "ips", "root_domains", "countries", "gafam"],
    )
    async def test_widget_sends_its_section_params(self, recorded, section, endpoint, params):
        res = await dash_module.overviewDashboard(
            profile_id="abc123", from_time="-1d", to_time="now", limit=25, section=section
        )
        assert isinstance(res, ToolResult)
        assert [(ep, dict(p)) for ep, p in recorded] == [(endpoint, params)]

    @pytest.mark.asyncio
    async def test_security_sends_the_window_without_a_limit(self, recorded):
        res = await dash_module.overviewDashboard(
            profile_id="abc123", from_time="-1d", to_time="now", limit=25, section="security"
        )
        assert isinstance(res, ToolResult)
        assert [(ep, dict(p)) for ep, p in recorded] == [
            ("encryption", {"from": "-1d", "to": "now"}),
            ("dnssec", {"from": "-1d", "to": "now"}),
        ]

    @pytest.mark.asyncio
    async def test_overview_emits_the_series_chart_and_the_domain_table(self, recorded):
        res = await dash_module.overviewDashboard(profile_id="abc123", section="overview")
        assert {"AreaChart", "Card", "DataTable", "Heading"} <= self._component_types(res)

    @pytest.mark.asyncio
    async def test_domain_card_view_types_follow_view_type(self, recorded):
        table = await dash_module.overviewDashboard(profile_id="abc123", section="domains", view_type="table")
        bar = await dash_module.overviewDashboard(profile_id="abc123", section="domains", view_type="bar")
        pie = await dash_module.overviewDashboard(profile_id="abc123", section="domains", view_type="pie")
        assert "DataTable" in self._component_types(table) and "BarChart" not in self._component_types(table)
        assert "BarChart" in self._component_types(bar) and "DataTable" not in self._component_types(bar)
        assert "PieChart" in self._component_types(pie) and "DataTable" not in self._component_types(pie)
