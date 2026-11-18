"""Limit validation and forwarding for overviewDashboard and plotAnalytics (issue #415).

``limit`` is bounded to 1..500 everywhere: the dashboard rejects anything
outside that window before any upstream request, ``plotAnalytics`` rejects
non-positive values as well as over-cap ones, and the GAFAM paths forward the
caller's limit instead of silently dropping it.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp.tools.base import ToolResult

from nextdns_mcp import charts as charts_module
from nextdns_mcp import client as client_module
from nextdns_mcp import utils as utils_module
from nextdns_mcp.tools import dashboard as dash_module
from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.dashboard import data as data_mod


class _StreamedResponse:
    """Async-CM stand-in for a streaming httpx response (the capped path)."""

    def __init__(self, status_code: int, body: bytes, request: httpx.Request) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers()
        self.request = request
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def aiter_bytes(self):
        yield self._body


@pytest.fixture
def mock_api_client(monkeypatch):
    """Replace the module-level api_client; ``.stream`` records the call."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    client = AsyncMock()
    client.stream_state = {"body": json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()}
    client.stream_calls: list[dict] = []

    def _stream(method, url, params=None, json=None, **kwargs):
        client.stream_calls.append({"method": method, "url": url, "params": params})
        return _StreamedResponse(200, client.stream_state["body"], httpx.Request(method, url))

    client.stream = _stream
    monkeypatch.setattr(client_module, "api_client", client)
    return client


class TestValidateDashboardLimit:
    """Unit tests for the shared 1..500 limit gate the dashboard and plots use."""

    @pytest.mark.parametrize("limit", [0, -1, -100, 501, 1000, 10_000_000, True])
    def test_out_of_range_limits_rejected(self, limit):
        err = utils_module.limit_range_error(limit)
        assert err is not None
        assert err["code"] == "invalid_argument"
        assert err["min_limit"] == 1
        assert err["max_limit"] == 500
        assert err["limit"] == limit

    @pytest.mark.parametrize("limit", [1, 10, 500])
    def test_boundary_limits_accepted(self, limit):
        assert utils_module.limit_range_error(limit) is None


class TestOverviewDashboardLimitValidation:
    """End-to-end rejection of bad limits through the public tool."""

    @pytest.fixture
    def recording_fetch(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        calls: list[dict] = []

        async def fake_fetch(pid, endpoint, params=None):
            calls.append({"endpoint": endpoint, "params": dict(params or {})})
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
                return ({"data": [{"company": "google", "queries": 50}]}, None)
            if endpoint == "encryption":
                return ({"data": [{"encrypted": True, "queries": 80}, {"encrypted": False, "queries": 20}]}, None)
            if endpoint == "dnssec":
                return ({"data": [{"validated": True, "queries": 30}, {"validated": False, "queries": 70}]}, None)
            return ({"data": [{"domain": "test.com", "name": "Test", "ip": "1.1.1.1", "queries": 10}]}, None)

        monkeypatch.setattr(data_mod, "_fetch_analytics_payload", fake_fetch)
        return calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, -5, 501, 10_000_000])
    @pytest.mark.parametrize("section", ["overview", "all", "gafam", "domains"])
    async def test_invalid_limit_rejected_before_any_request(self, recording_fetch, limit, section):
        res = await dash_module.overviewDashboard(profile_id="abc123", limit=limit, section=section)
        assert isinstance(res, dict)
        assert res["code"] == "invalid_argument"
        assert res["limit"] == limit
        assert recording_fetch == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [1, 500])
    async def test_boundary_limits_reach_upstream(self, recording_fetch, limit):
        res = await dash_module.overviewDashboard(profile_id="abc123", limit=limit, section="overview")
        assert isinstance(res, ToolResult)
        assert recording_fetch != []

    @pytest.mark.asyncio
    async def test_gafam_section_forwards_limit(self, recording_fetch):
        res = await dash_module.overviewDashboard(profile_id="abc123", limit=50, section="gafam")
        assert isinstance(res, ToolResult)
        destinations_calls = [c for c in recording_fetch if c["endpoint"] == "destinations"]
        assert len(destinations_calls) == 1
        assert destinations_calls[0]["params"]["limit"] == 50
        assert destinations_calls[0]["params"]["type"] == "gafam"

    @pytest.mark.asyncio
    async def test_combined_dashboard_forwards_limit_to_gafam(self, recording_fetch):
        res = await dash_module.overviewDashboard(profile_id="abc123", limit=50, section="all")
        assert isinstance(res, ToolResult)
        gafam_calls = [
            c for c in recording_fetch if c["endpoint"] == "destinations" and c["params"].get("type") == "gafam"
        ]
        assert len(gafam_calls) == 1
        assert gafam_calls[0]["params"]["limit"] == 50


class TestPlotAnalyticsLimitLowerBound:
    """``plotAnalytics`` rejects non-positive limits (issue #415)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, -1, -100, True])
    async def test_non_positive_limit_rejected(self, mock_api_client, monkeypatch, limit):
        """``True`` is refused with the lower bound, not answered as ``limit=1``."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(charts_module, "current_time", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        result = await plots_module._plot_analytics_series_impl("status", limit=limit)

        assert result["code"] == "invalid_argument"
        assert result["min_limit"] == 1
        assert result["limit"] == limit
        assert not mock_api_client.stream_calls

    @pytest.mark.asyncio
    async def test_limit_of_one_accepted(self, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(charts_module, "current_time", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        await plots_module._plot_analytics_series_impl("status", from_time="-1d", interval=3600, limit=1)

        assert mock_api_client.stream_calls != []

    def test_validate_interval_and_limit_lower_bound(self):
        err = plots_module._validate_interval_and_limit(3600, 0)
        assert err is not None
        assert err["code"] == "invalid_argument"
        assert err["min_limit"] == 1
        assert plots_module._validate_interval_and_limit(3600, 1) is None
