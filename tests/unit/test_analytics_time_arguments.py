"""Analytics time and partials arguments are read as the caller means them.

Three live runs show the same loop: ``from_time=-86400`` is an absolute Unix
timestamp (1969), so the endpoint answers 200 with no rows and the model varies
the interval instead of the range - ~40 calls, no error to act on. ``now-1d``
answers 400 "`from` must be number". ``partials=false`` answers 400 enum that
names no allowed values. Each was checked against the live API.

SPDX-License-Identifier: MIT
"""

import pytest
from fastmcp import Client

from nextdns_mcp.server import build_mcp_server
from nextdns_mcp.utils import normalize_partials, normalize_time_value


class TestTimeValues:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            # A bare negative number is an offset, never an absolute time.
            (-86400, "-86400s"),
            (-1, "-1s"),
            ("-86400", "-86400s"),
            # The Grafana/Prometheus spelling, in the API's own relative form.
            ("now-1d", "-1d"),
            ("now - 7d", "-7d"),
            ("NOW-30m", "-30m"),
            ("now", "now"),
        ],
    )
    def test_a_shape_the_api_refuses_is_rewritten(self, given, expected):
        assert normalize_time_value(given) == expected

    @pytest.mark.parametrize(
        "given",
        ["-1d", "-1y", "1761500000", 1761500000, "2024-01-01T00:00:00Z", "now", None, True, "yesterday"],
    )
    def test_a_shape_the_api_accepts_is_left_alone(self, given):
        assert normalize_time_value(given) == given


class TestPartials:
    @pytest.mark.parametrize(
        ("given", "expected"), [("false", "none"), ("true", "all"), ("none", "none"), ("ALL", "all")]
    )
    def test_a_boolean_becomes_the_value_it_means(self, given, expected):
        assert normalize_partials(given) == expected

    def test_anything_else_is_left_for_the_tool_to_name(self):
        assert normalize_partials("sometimes") == "sometimes"
        assert normalize_partials(None) is None


class TestTheToolsSaySoThemselves:
    """The point is that the caller is told, not that the upstream was asked."""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    async def _call(self, tool: str, arguments: dict) -> dict:
        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(tool, arguments)
        return getattr(result, "structured_content", None) or {}

    @pytest.mark.asyncio
    async def test_series_without_a_start_time_is_refused_before_the_api(self):
        payload = await self._call("queryAnalytics", {"metric": "devices", "profile_id": "23228d", "series": True})
        assert "from_time is required when series=true" in str(payload.get("error", ""))
        assert payload.get("code") == "missing_required_argument"

    @pytest.mark.asyncio
    async def test_a_negative_range_is_not_read_as_1969(self):
        """The old behaviour: the budget error, or 200 with no rows, and nothing to correct."""
        payload = await self._call(
            "plotAnalytics", {"metric": "ips", "profile_id": "23228d", "from_time": -86400, "to_time": "now"}
        )
        error = str(payload.get("error", ""))
        assert "more than the 2000-point series budget" not in error
        assert "Requested range spans" not in error

    @pytest.mark.asyncio
    async def test_an_unknown_partials_names_the_values_that_work(self):
        payload = await self._call("plotAnalytics", {"metric": "ips", "profile_id": "23228d", "partials": "sometimes"})
        error = str(payload.get("error", ""))
        assert "partials must be one of: none, all" in error

    @pytest.mark.asyncio
    async def test_a_boolean_partials_is_not_an_error(self):
        payload = await self._call(
            "queryAnalytics",
            {"metric": "dnssec", "profile_id": "23228d", "series": True, "from_time": "-1d", "partials": "false"},
        )
        assert "partials" not in str(payload.get("error", ""))
