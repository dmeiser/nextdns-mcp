"""Analytics time and partials arguments are read as the caller means them.

Three live runs show the same loop: ``from_time=-86400`` is an absolute Unix
timestamp (1969), so the endpoint answers 200 with no rows and the model varies
the interval instead of the range - ~40 calls, no error to act on. ``now-1d``
answers 400 "`from` must be number". ``partials=false`` answers 400 enum that
names no allowed values. Each was checked against the live API.

Two paths reach those endpoints now: ``plotAnalytics``, which builds the series
query itself, and the generated ``;series`` tools, which pass the caller's
arguments through. Both are covered here, because both have to end up sending the
spellings the API reads.

SPDX-License-Identifier: MIT
"""

import pytest
from fastmcp import Client

from nextdns_mcp import spec_tools
from nextdns_mcp.server import build_mcp_server
from nextdns_mcp.utils import _query_bool, normalize_partials, normalize_time_value


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


class TestQueryBooleans:
    """The analytics endpoints read `true`/`false`; a raw Python bool would go
    out as True/False and be refused, so the query builder renders it first."""

    @pytest.mark.parametrize(("given", "expected"), [(True, "true"), (False, "false")])
    def test_a_boolean_is_sent_in_the_spelling_the_api_reads(self, given, expected):
        assert _query_bool(given) == expected

    @pytest.mark.parametrize("given", ["all", 3600, 3600.5, None])
    def test_every_other_value_passes_through_unchanged(self, given):
        assert _query_bool(given) is given


class TestTheToolsSaySoThemselves:
    """The caller is told in the schema, and the two spellings the API misreads are fixed.

    The grouped tool used to fill in a default window for a series request that named
    none. The spec says ``from`` is required for every ``;series`` endpoint, so the
    generated tool publishes it as required and the caller names it — which is what
    these tests check on the published schema, together with what reaches the wire for
    the spellings upstream reads wrongly.
    """

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    @pytest.fixture
    def sent(self, monkeypatch):
        """Capture the query parameters the generated tool puts on the wire."""
        captured: dict = {}

        async def fake(method, url, params=None, json_body=None):
            captured["url"] = url
            captured["params"] = params or {}
            return {"data": []}

        monkeypatch.setattr(spec_tools, "_api_request_payload", fake)
        return captured

    async def _call(self, tool: str, arguments: dict) -> dict:
        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(tool, arguments)
        return getattr(result, "structured_content", None) or {}

    def test_the_series_endpoint_asks_for_a_start_time(self):
        """A series request without a start time answers nothing, so the schema asks.

        Replacing that with a tool-invented default hid the requirement from the
        caller; the spec states it, and FastMCP publishes it as ``required``.
        """
        schema = spec_tools.spec_routes()
        series = next(route for route in schema if route.operation_id == "getAnalyticsEncryptionSeries")
        assert "from" in series.flat_param_schema["required"]

    def test_the_window_argument_names_the_relative_spelling(self):
        """ "Last day" has no absolute spelling, so the offset has to be written down.

        A caller told only about timestamps answers a request for the last day by
        asking for timestamps instead of acting.
        """
        series = next(
            route for route in spec_tools.spec_routes() if route.operation_id == "getAnalyticsEncryptionSeries"
        )
        window = series.flat_param_schema["properties"]["from"]["description"]
        assert "-1d" in window
        assert "epoch seconds" in window

    @pytest.mark.asyncio
    async def test_a_window_the_caller_named_is_left_alone(self, sent):
        await self._call(
            "getAnalyticsEncryptionSeries",
            {"profile_id": "aa11aa", "from": "-7d", "interval": 3600},
        )
        assert sent["url"].endswith("/analytics/encryption;series")
        assert sent["params"]["from"] == "-7d"

    @pytest.mark.asyncio
    async def test_a_bare_negative_window_is_not_sent_as_1969(self, sent):
        """``from=-86400`` is an offset, and upstream reads the number as 1969.

        It answers 200 with no rows, so there is no error to act on: across three
        live runs a model retried that call about 40 times.
        """
        await self._call(
            "getAnalyticsEncryptionSeries",
            {"profile_id": "aa11aa", "from": -86400, "interval": 3600},
        )
        assert sent["params"]["from"] == "-86400s"

    @pytest.mark.asyncio
    async def test_the_grafana_spelling_is_sent_in_the_spelling_the_api_uses(self, sent):
        await self._call(
            "getAnalyticsEncryptionSeries",
            {"profile_id": "aa11aa", "from": "now-1d", "interval": 3600},
        )
        assert sent["params"]["from"] == "-1d"

    @pytest.mark.asyncio
    async def test_a_boolean_partials_is_sent_as_the_value_it_means(self, sent):
        """``partials=false`` answered 400 with an enum naming no allowed values."""
        await self._call(
            "getAnalyticsEncryptionSeries",
            {"profile_id": "aa11aa", "from": "-1d", "interval": 3600, "partials": False},
        )
        assert sent["params"]["partials"] == "none"

    @pytest.mark.asyncio
    async def test_a_negative_range_is_not_read_as_1969(self):
        """The old behaviour: the budget error, or 200 with no rows, and nothing to correct."""
        payload = await self._call(
            "plotAnalytics", {"metric": "ips", "profile_id": "aa11aa", "from_time": -86400, "to_time": "now"}
        )
        error = str(payload.get("error", ""))
        assert "more than the 2000-point series budget" not in error
        assert "Requested range spans" not in error

    @pytest.mark.asyncio
    async def test_an_unknown_partials_names_the_values_that_work(self):
        payload = await self._call("plotAnalytics", {"metric": "ips", "profile_id": "aa11aa", "partials": "sometimes"})
        error = str(payload.get("error", ""))
        assert "partials must be one of: none, all" in error
