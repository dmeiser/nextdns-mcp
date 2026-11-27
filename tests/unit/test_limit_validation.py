"""Limit validation and forwarding for overviewDashboard (issue #415).

``limit`` is bounded to 1..500 on the dashboard, which rejects anything outside
that window before any upstream request, and the GAFAM paths forward the
caller's limit instead of silently dropping it. ``plotAnalytics`` publishes no
``limit`` at all: the ``;series`` endpoints consume none, so the plot tool has
no limit to validate (its bounds live in test_plotting.py).
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp.tools.base import ToolResult

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
def mock_api_client(monkeypatch, request):
    """Replace the module-level api_client; ``.stream`` records the call."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    client = AsyncMock()
    client.stream_state = {"body": json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()}
    client.stream_calls: list[dict] = []

    def _stream(method, url, params=None, json=None, **kwargs):
        client.stream_calls.append({"method": method, "url": url, "params": params})
        return _StreamedResponse(200, client.stream_state["body"], httpx.Request(method, url))

    client.stream = _stream
    # The client lives behind an explicit owner now (issue #642): the mock
    # becomes the owner's lazy build instead of patching a module attribute.
    monkeypatch.setattr(client_module, "create_nextdns_client", lambda: client)
    owner_cm = client_module.api_client_owned(client_module.ApiClientOwner())
    owner_cm.__enter__()
    request.addfinalizer(lambda: owner_cm.__exit__(None, None, None))
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

    @pytest.mark.parametrize("limit", ["10", "", "abc", None, [10], {"limit": 10}, 10.5j])
    def test_non_numeric_limits_rejected_with_typed_payload(self, limit):
        """A non-numeric limit returns the typed payload, not a ``TypeError`` (issue #596)."""
        err = utils_module.limit_range_error(limit)
        assert err is not None
        assert err["code"] == "invalid_argument"
        assert err["min_limit"] == 1
        assert err["max_limit"] == 500
        assert err["limit"] == limit

    @pytest.mark.parametrize("limit", [1, 10, 500])
    def test_boundary_limits_accepted(self, limit):
        assert utils_module.limit_range_error(limit) is None

    @pytest.mark.parametrize("limit", [float("nan"), float("inf"), float("-inf"), 1.0, 42.5, 500.0])
    def test_float_limits_rejected(self, limit):
        """A float never survives the range check (issue #759).

        ``nan`` compares False against both bounds, so a bare ``<``/``>`` gate
        accepts it, and ``42.5`` is inside the band but is not a count.
        """
        err = utils_module.limit_range_error(limit)
        assert err is not None
        assert err["code"] == "invalid_argument"
        assert err["min_limit"] == 1
        assert err["max_limit"] == 500


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


class TestPlotIntervalValidation:
    """The interval band and type are checked before a series request is built (issue #759)."""

    @pytest.mark.parametrize("interval", [float("nan"), float("inf"), 60.0, 3600.5])
    def test_rejects_non_integer_interval(self, interval):
        """``nan`` fails both interval bounds and a fractional window is unanswerable (issue #759)."""
        err = plots_module._validate_interval(interval)
        assert err is not None
        assert err["code"] == "invalid_argument"
        assert err["minimum_interval"] == 60
        assert err["max_interval"] == plots_module.PLOT_INTERVAL_MAX

    @pytest.mark.parametrize("interval", [60, 3600, plots_module.PLOT_INTERVAL_MAX])
    def test_accepts_integer_band(self, interval):
        """The inclusive integer bounds are unchanged."""
        assert plots_module._validate_interval(interval) is None
