"""Unit tests for NextDNS interactive analytics dashboard and specialized UI tools."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools.base import ToolResult

from nextdns_mcp.tools import dashboard as dash_module


class TestDashboardHelpers:
    """Tests for unit formatting and component builder helpers."""

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
        assert data is None
        assert err["code"] == "no_data"

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

        # 3. Empty data returns no_data error
        monkeypatch.setattr(dash_module, "_fetch_analytics_payload", AsyncMock(return_value=({"data": []}, None)))
        _pid, _data, err = await dash_module._fetch_card_data("abc123", "endpoint")
        assert err["code"] == "no_data"

    @pytest.mark.asyncio
    async def test_fetch_analytics_payload_error_classification(self, monkeypatch):
        monkeypatch.setattr(
            dash_module, "_api_request_payload", AsyncMock(return_value={"code": "http_error", "error": "upstream"})
        )
        payload, err = await dash_module._fetch_analytics_payload("p1", "status")
        assert payload is None and err["code"] == "http_error"

        monkeypatch.setattr(dash_module, "_api_request_payload", AsyncMock(return_value={"data": [{"ok": True}]}))
        payload, err = await dash_module._fetch_analytics_payload("p1", "status")
        assert payload == {"data": [{"ok": True}]} and err is None


class TestDashboardToolsExecution:
    """Tests executing each public tool end-to-end with mock data."""

    @pytest.fixture
    def mock_all_endpoints(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        async def fake_fetch(pid, endpoint, params=None):
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

    @pytest.mark.asyncio
    async def test_analytics_dashboard_all_sections(self, mock_all_endpoints):
        res = await dash_module.overviewDashboard(profile_id="abc123", section="all")
        assert isinstance(res, ToolResult)
        assert "Rendered NextDNS Dashboard" in res.content[0].text
        assert "$prefab" in res.structured_content

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
        res = await dash_module.gafamAnalytics()
        assert isinstance(res, ToolResult)
        assert "GAFAM Dominance" in res.content[0].text
        assert "$prefab" in res.structured_content

    @pytest.mark.asyncio
    async def test_gafam_analytics_error(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            dash_module, "_fetch_analytics_payload", AsyncMock(return_value=(None, {"code": "err", "error": "fail"}))
        )
        res = await dash_module.gafamAnalytics()
        assert isinstance(res, dict)
        assert res["code"] == "err"

    @pytest.mark.asyncio
    async def test_security_analytics(self, mock_all_endpoints):
        res = await dash_module.securityAnalytics()
        assert isinstance(res, ToolResult)
        assert "Security Analytics" in res.content[0].text
        assert "$prefab" in res.structured_content

    @pytest.mark.asyncio
    async def test_security_analytics_error_paths(self, clean_env, monkeypatch):
        # 1. Profile error
        res = await dash_module.securityAnalytics()
        assert isinstance(res, dict)
        assert res["code"] == "missing_profile_id"

        # 2. Encryption error
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            dash_module,
            "_fetch_analytics_payload",
            AsyncMock(side_effect=[(None, {"code": "enc_err", "error": "enc"}), ({}, None)]),
        )
        res = await dash_module.securityAnalytics()
        assert res["code"] == "enc_err"

        # 3. DNSSEC error
        monkeypatch.setattr(
            dash_module,
            "_fetch_analytics_payload",
            AsyncMock(side_effect=[({}, None), (None, {"code": "sec_err", "error": "sec"})]),
        )
        res = await dash_module.securityAnalytics()
        assert res["code"] == "sec_err"

    @pytest.mark.asyncio
    async def test_reasons_analytics(self, mock_all_endpoints):
        res = await dash_module.reasonsAnalytics()
        assert isinstance(res, ToolResult)
        assert "Reasons for Blocking" in res.content[0].text

    @pytest.mark.asyncio
    async def test_device_analytics(self, mock_all_endpoints):
        res = await dash_module.deviceAnalytics()
        assert isinstance(res, ToolResult)
        assert "Device Analytics" in res.content[0].text

    @pytest.mark.asyncio
    async def test_ip_analytics(self, mock_all_endpoints):
        res = await dash_module.ipAnalytics()
        assert isinstance(res, ToolResult)
        assert "IP Analytics" in res.content[0].text

    @pytest.mark.asyncio
    async def test_root_domain_analytics(self, mock_all_endpoints):
        res = await dash_module.rootDomainAnalytics()
        assert isinstance(res, ToolResult)
        assert "Root Domain Analytics" in res.content[0].text

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
        res = await dash_module.kpiAnalytics()
        assert isinstance(res, ToolResult)
        assert "KPI Metrics" in res.content[0].text

    @pytest.mark.asyncio
    async def test_domains_analytics(self, mock_all_endpoints):
        res = await dash_module.domainsAnalytics()
        assert isinstance(res, ToolResult)
        assert "Resolved Domains" in res.content[0].text

    @pytest.mark.asyncio
    async def test_blocked_domains_analytics(self, mock_all_endpoints):
        res = await dash_module.blockedDomainsAnalytics()
        assert isinstance(res, ToolResult)
        assert "Blocked Domains" in res.content[0].text

    @pytest.mark.asyncio
    async def test_destination_analytics(self, mock_all_endpoints):
        res = await dash_module.countryAnalytics()
        assert isinstance(res, ToolResult)
        assert "Traffic Destinations" in res.content[0].text

    @pytest.mark.asyncio
    async def test_tools_view_type_arguments(self, mock_all_endpoints):
        assert isinstance(await dash_module.domainsAnalytics(view_type="bar"), ToolResult)
        assert isinstance(await dash_module.blockedDomainsAnalytics(view_type="bar"), ToolResult)
        assert isinstance(await dash_module.reasonsAnalytics(view_type="bar"), ToolResult)
        assert isinstance(await dash_module.deviceAnalytics(view_type="bar"), ToolResult)
        assert isinstance(await dash_module.rootDomainAnalytics(view_type="bar"), ToolResult)
        assert isinstance(await dash_module.countryAnalytics(view_type="bar"), ToolResult)
        assert isinstance(await dash_module.gafamAnalytics(view_type="bar"), ToolResult)

    @pytest.mark.asyncio
    async def test_specialized_tools_error_paths(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(
            dash_module, "_fetch_analytics_payload", AsyncMock(return_value=(None, {"code": "err", "error": "fail"}))
        )

        for tool_fn in (
            dash_module.kpiAnalytics,
            dash_module.domainsAnalytics,
            dash_module.blockedDomainsAnalytics,
            dash_module.reasonsAnalytics,
            dash_module.deviceAnalytics,
            dash_module.ipAnalytics,
            dash_module.rootDomainAnalytics,
            dash_module.countryAnalytics,
        ):
            res = await tool_fn()
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

    def test_build_queries_series_card_exception_handling(self, monkeypatch):
        monkeypatch.setattr(dash_module, "_validate_series_payload", MagicMock(side_effect=ValueError("bad data")))
        assert dash_module._build_queries_series_card({"meta": {}}) is None

    def test_empty_cards_rendering(self):
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
