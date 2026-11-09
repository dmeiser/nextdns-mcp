"""Tests for the analytics plotting helpers and tool wrappers."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastmcp.tools.base import ToolResult

from nextdns_mcp import client as client_module
from nextdns_mcp import server, spec_tools
from nextdns_mcp.tools import plots as plots_module


def _make_response(json_data):
    """Build a mock httpx.Response returning ``json_data``."""
    response = MagicMock()
    response.status_code = 200
    response.content = b'{"dummy": true}'
    response.json.return_value = json_data
    return response


class TestNoMatplotlibDependency:
    """Regression tests (issue #165): matplotlib is replaced by Prefab and not imported."""

    def test_importing_server_does_not_import_matplotlib(self):
        # Importing the server (and thus every tool module, including the plot
        # tool module) in a fresh interpreter must not pull matplotlib in.
        import subprocess
        import sys

        code = "import sys\n"
        code += "import nextdns_mcp.server\n"
        code += "import nextdns_mcp.tools\n"
        code += "assert not any(m == 'matplotlib' or m.startswith('matplotlib.') for m in sys.modules)\n"
        code += "print('ok')\n"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"


class TestExtractSeriesLabel:
    """Tests for _extract_series_label."""

    def test_name_label(self):
        assert plots_module._extract_series_label({"name": "example.com"}, 0) == "example.com"

    def test_status_label(self):
        assert plots_module._extract_series_label({"status": "blocked"}, 0) == "blocked"

    def test_protocol_label(self):
        assert plots_module._extract_series_label({"protocol": "DoH"}, 0) == "DoH"

    def test_version_label(self):
        assert plots_module._extract_series_label({"version": "IPv4"}, 0) == "IPv4"

    def test_id_label(self):
        assert plots_module._extract_series_label({"id": "device1"}, 0) == "device1"

    def test_validated_true_label(self):
        assert plots_module._extract_series_label({"validated": True}, 0) == "validated"

    def test_validated_false_label(self):
        assert plots_module._extract_series_label({"validated": False}, 0) == "not_validated"

    def test_encrypted_true_label(self):
        assert plots_module._extract_series_label({"encrypted": True}, 0) == "encrypted"

    def test_encrypted_false_label(self):
        assert plots_module._extract_series_label({"encrypted": False}, 0) == "unencrypted"

    def test_fallback_index_label(self):
        assert plots_module._extract_series_label({"queries": []}, 3) == "series_3"


class TestSeriesColor:
    """Tests for series color determination."""

    def test_series_color(self):
        assert plots_module._series_color({"status": "blocked"}) == "#ef4444"
        assert plots_module._series_color({"status": "default"}) == "#3b82f6"
        assert plots_module._series_color({"name": "allowed"}) == "#3b82f6"
        assert plots_module._series_color({"name": "something_else"}) is None


class TestParseSeriesTimestamp:
    """Tests for _parse_series_timestamp."""

    def test_parses_z_timestamp(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00Z")
        assert ts.year == 2024
        assert ts.month == 1
        assert ts.day == 15
        assert ts.hour == 10
        assert ts.minute == 30
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timedelta(0)

    def test_parses_offset_timestamp(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00+00:00")
        assert ts.year == 2024

    def test_parses_microseconds_with_z(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00.123456Z")
        assert ts.microsecond == 123456
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timedelta(0)

    def test_parses_microseconds_fallback(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00.123456+00:00")
        assert ts.year == 2024
        assert ts.microsecond == 123456

    def test_parses_whole_second_without_timezone(self):
        # Regression (issue #186): the old single %f-bearing strptime fallback
        # could not match a whole-second timestamp, so it leaked a bare
        # ValueError from strptime.
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00")
        assert ts.year == 2024
        assert ts.hour == 10
        assert ts.minute == 30
        assert ts.second == 0
        # A zone-less value is anchored to UTC so it denotes the same instant
        # regardless of the host's local timezone.
        assert ts.utcoffset() == timedelta(0)

    def test_parses_whole_second_with_z_via_fallback_format(self, monkeypatch):
        # Force the strptime fallback path and confirm the whole-second format
        # is reachable when fromisoformat fails (issue #186). The datetime C
        # type is immutable, so the module-level name is swapped for a subclass.
        class _NoIsoFrom(datetime):
            @classmethod
            def fromisoformat(cls, value: str):
                raise ValueError("fromisoformat unavailable")

        monkeypatch.setattr(plots_module, "datetime", _NoIsoFrom)
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00Z")
        assert ts.hour == 10
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timedelta(0)

    def test_malformed_timestamp_raises_value_error(self):
        with pytest.raises(ValueError):
            plots_module._parse_series_timestamp("not-a-timestamp")


class TestBuildSeriesChart:
    """Tests for Prefab chart construction helpers."""

    def test_build_chart_point_validates_and_aligns(self):
        series_data = [
            {"name": "blocked", "queries": [10, 20]},
            {"name": "allowed", "queries": [5]},
        ]
        point0 = plots_module._build_chart_point("2024-01-15T10:00:00Z", 0, series_data)
        assert point0 == {"time": "2024-01-15T10:00:00Z", "series_0": 10, "series_1": 5}

        # Index beyond series_1 length defaults to 0
        point1 = plots_module._build_chart_point("2024-01-15T11:00:00Z", 1, series_data)
        assert point1 == {"time": "2024-01-15T11:00:00Z", "series_0": 20, "series_1": 0}

    def test_build_chart_point_raises_for_invalid_timestamp(self):
        with pytest.raises(ValueError):
            plots_module._build_chart_point("bad-timestamp", 0, [{"queries": [1]}])

    def test_build_chart_series_extracts_labels(self):
        series_data = [
            {"name": "blocked", "queries": [10]},
            {"status": "allowed", "queries": [5]},
            {"queries": [1]},
        ]
        series = plots_module._build_chart_series(series_data)
        assert len(series) == 3
        assert series[0].data_key == "series_0" and series[0].label == "blocked"
        assert series[1].data_key == "series_1" and series[1].label == "allowed"
        assert series[2].data_key == "series_2" and series[2].label == "series_2"

    def test_build_series_chart_returns_tool_result_with_prefab(self):
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [
            {"name": "blocked", "queries": [10, 20]},
            {"name": "allowed", "queries": [5, 8]},
        ]
        result = plots_module._build_series_chart("status", times, series_data)
        assert isinstance(result, ToolResult)
        assert len(result.content) == 1
        assert "NextDNS Analytics: status (43 total queries across 2 intervals)" == result.content[0].text
        assert isinstance(result.structured_content, dict)
        assert "$prefab" in result.structured_content
        assert "view" in result.structured_content


@pytest.fixture
def now():
    """A fixed clock so relative from/to values resolve deterministically."""
    return 1_700_000_000.0


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
    """Replace the module-level api_client with an AsyncMock.

    The plot fetch routes through the payload wrapper (issue #183), which
    issues uncapped requests via ``client.api_client.request``; the generated
    ``;series`` tools run under the response cap and therefore STREAM - so
    ``.stream`` returns a real async-CM over a canned body (``stream_state``)
    and records the call. Tests drive the uncapped path through ``.request``.
    """
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


@pytest.fixture
def sample_series_payload():
    """Return a sample time-series API payload."""
    return {
        "meta": {
            "series": {
                "times": ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"],
            }
        },
        "data": [
            {"name": "blocked", "queries": [10, 20]},
        ],
    }


class TestPlotAnalyticsSeriesImpl:
    """Tests for _plot_analytics_series_impl."""

    @pytest.mark.asyncio
    async def test_unsupported_metric_returns_error(self, clean_env):
        result = await plots_module._plot_analytics_series_impl("notametric")
        assert "error" in result
        assert "Unsupported metric" in result["error"]

    @pytest.mark.asyncio
    async def test_domains_metric_returns_error(self, clean_env):
        result = await plots_module._plot_analytics_series_impl("domains")
        assert "error" in result
        assert "Unsupported metric" in result["error"]

    @pytest.mark.asyncio
    async def test_interval_too_small_returns_error(self, clean_env):
        result = await plots_module._plot_analytics_series_impl("status", interval=30)
        assert "error" in result
        assert "interval must be at least 60" in result["error"]

    @pytest.mark.asyncio
    async def test_request_within_budget_forwarded_unchanged(self, clean_env, mock_api_client, monkeypatch):
        """A request that already fits the point budget is sent exactly as asked."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()
        # -1d at a 1-hour interval is 24 points, well inside the budget.
        await plots_module._plot_analytics_series_impl("status", from_time="-1d", interval=3600)
        params = mock_api_client.stream_calls[-1]["params"]
        assert params["interval"] == 3600
        assert params["from"] == "-1d"
        # The ;series endpoint takes no limit parameter, so none is invented.
        assert "limit" not in params

    @pytest.mark.asyncio
    async def test_the_plot_and_the_generated_series_tool_ask_for_the_same_window(
        self, clean_env, mock_api_client, monkeypatch
    ):
        """Both ``;series`` callers put identical query params on the wire (issue #267).

        Two paths read that endpoint now: ``plotAnalytics``, which builds the series
        query itself, and the generated tool, which passes the caller's arguments
        through. A chart and the numbers under it have to be answers to the same
        question, so this asserts the observable request each one actually issues.
        """
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()
        window = {
            # Absolute timestamps, so the request is deterministic without a clock patch.
            # `partials` is `all`, not `both`: the endpoint answers 400 for `both`,
            # and the tool now rejects that value by name instead of forwarding it.
            "from": 1_700_000_000 - 86_400,
            "to": 1_700_000_000,
            "interval": 3600,
            "alignment": "start",
            "timezone": "UTC",
            "partials": "all",
        }

        await plots_module._plot_analytics_series_impl(
            "status",
            profile_id="abc123",
            from_time=window["from"],
            to_time=window["to"],
            interval=window["interval"],
            alignment=window["alignment"],
            timezone=window["timezone"],
            partials=window["partials"],
        )
        plot_call = mock_api_client.stream_calls[-1] if mock_api_client.stream_calls else None

        route = next(r for r in spec_tools.spec_routes() if r.operation_id == "getAnalyticsStatusSeries")
        await spec_tools.SpecApiTool(route).run({"profile_id": "abc123", **window})
        # The generated call is capped, so it arrives on the STREAM path, not request.
        assert mock_api_client.stream_calls, "the capped series call must stream, not buffer"
        series_call = mock_api_client.stream_calls[-1]

        assert plot_call["url"] == series_call["url"] == "/profiles/abc123/analytics/status;series"
        assert plot_call["params"] == series_call["params"]
        assert plot_call["params"] == {
            "from": 1_700_000_000 - 86_400,
            "to": 1_700_000_000,
            "interval": 3600,
            "alignment": "start",
            "timezone": "UTC",
            "partials": "all",
        }

    @pytest.mark.asyncio
    async def test_issue_scenario_one_year_minute_interval_is_rejected(self, clean_env, mock_api_client, monkeypatch):
        """The issue scenario - a 1y range at a 1-minute interval - never reaches the API.

        Without the point budget the API would return ~525,600 points, all
        buffered by ``_api_request`` and then plotted (issue #267).
        """
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        result = await plots_module._plot_analytics_series_impl("status", from_time="-1y", interval=60)

        assert result["code"] == "invalid_argument"
        assert result["max_points"] == plots_module.PLOT_MAX_POINTS
        assert result["interval"] == 60
        assert not mock_api_client.stream_calls

    @pytest.mark.asyncio
    async def test_coarse_request_is_never_refined(self, clean_env, mock_api_client, monkeypatch):
        """A monthly interval over 10y is rejected, not silently refined into a denser series.

        The reviewer trace from issue #267: clamping the interval to a maximum
        moved 2592000s (monthly) down to 86400s, which would have *increased*
        the number of points returned and rendered.
        """
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        now = 1_700_000_000.0
        monkeypatch.setattr(plots_module, "_now", lambda: now)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        result = await plots_module._plot_analytics_series_impl("status", from_time="-10y", interval=2592000)

        assert result["code"] == "invalid_argument"
        assert result["max_interval"] == plots_module.PLOT_INTERVAL_MAX
        assert result["interval"] == 2592000
        assert not mock_api_client.stream_calls

    @pytest.mark.asyncio
    async def test_interval_at_ceiling_is_accepted(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()
        # -1y at the 86400s ceiling is 365 points, inside the budget.
        await plots_module._plot_analytics_series_impl("status", from_time="-1y", interval=86400)
        assert mock_api_client.stream_calls[-1]["params"]["interval"] == 86400

    @pytest.mark.parametrize(
        ("from_time", "to_time", "interval", "over_budget"),
        [
            ("-1y", "now", 60, True),
            ("-10y", "now", 2592000, False),  # 121 points: fits, and not a valid interval anyway
            ("-1d", "now", 3600, False),
            ("-1y", "now", 86400, False),
        ],
    )
    def test_series_budget_error(self, now, from_time, to_time, interval, over_budget):
        error = plots_module._series_budget_error(from_time, to_time, interval, now=now)
        assert (error is not None) == over_budget
        if over_budget:
            assert error["max_points"] == plots_module.PLOT_MAX_POINTS

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("now", 1_700_000_000.0),
            ("-1d", 1_700_000_000.0 - 86400),
            ("-10y", 1_700_000_000.0 - 10 * 31536000),
            ("-7d", 1_700_000_000.0 - 7 * 86400),
            ("-30m", 1_700_000_000.0 - 1800),
            ("-1w", 1_700_000_000.0 - 604800),
            (1_600_000_000, 1_600_000_000.0),
            ("1600000000", 1_600_000_000.0),
            ("2024-01-15T10:00:00Z", 1705312800.0),
        ],
    )
    def test_resolve_time_point(self, value, expected):
        assert plots_module._resolve_time_point(value, 1_700_000_000.0) == expected

    @pytest.mark.parametrize("value", ["not-a-time", "2024-13-45T99:99:99Z", True, "-1M", "-1D"])
    def test_resolve_time_point_unparseable_returns_none(self, value):
        assert plots_module._resolve_time_point(value, 1_700_000_000.0) is None

    def test_resolve_time_point_uppercase_unit_is_not_folded_onto_its_lowercase_twin(self, now):
        """``-1M`` is months upstream and must never be sized as the 60s of ``-1m``.

        Folding case here would make a months-long range look like a one-minute
        one, so the point budget would pass a request the API answers with tens
        of thousands of points. The helper refuses the value instead.
        """
        assert plots_module._resolve_time_point("-1m", now) == now - 60
        assert plots_module._resolve_time_point("-1M", now) is None

    @pytest.mark.asyncio
    async def test_uppercase_relative_unit_is_rejected_not_sized_as_minutes(
        self, clean_env, mock_api_client, monkeypatch
    ):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        result = await plots_module._plot_analytics_series_impl("status", from_time="-1M", to_time="now", interval=60)

        assert result["code"] == "invalid_argument"
        assert "Could not interpret the requested time range" in result["error"]
        assert not mock_api_client.stream_calls

    @pytest.mark.parametrize("tz", ["UTC", "Asia/Tokyo", "America/New_York"])
    def test_naive_iso_timestamp_is_read_as_utc(self, tz, monkeypatch):
        """A zone-less ISO value denotes the same instant on every host."""
        import time

        monkeypatch.setenv("TZ", tz)
        time.tzset()
        try:
            resolved = plots_module._resolve_time_point("2024-01-15T10:00:00", 1_700_000_000.0)
        finally:
            monkeypatch.undo()
            time.tzset()
        assert resolved == 1_705_312_800.0

    @pytest.mark.asyncio
    async def test_limit_over_cap_is_rejected(self, clean_env, mock_api_client, monkeypatch):
        """An over-cap ``limit`` is rejected, never clamped into the request.

        ``limit`` stays on the published tool surface, so a caller passing the
        old unbounded value gets a typed error naming the cap instead of a
        silently different request.
        """
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        result = await plots_module._plot_analytics_series_impl("status", limit=501)

        assert result["code"] == "invalid_argument"
        assert result["max_limit"] == plots_module.PLOT_LIMIT_MAX
        assert result["limit"] == 501
        assert not mock_api_client.stream_calls

    @pytest.mark.asyncio
    async def test_limit_at_cap_is_accepted_and_not_forwarded(self, clean_env, mock_api_client, monkeypatch):
        """A valid ``limit`` is accepted and never reaches the ``;series`` endpoint."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        await plots_module._plot_analytics_series_impl("status", from_time="-1d", limit=plots_module.PLOT_LIMIT_MAX)

        params = mock_api_client.stream_calls[-1]["params"]
        assert "limit" not in params

    @pytest.mark.asyncio
    async def test_uninterpretable_range_is_rejected(self, clean_env, mock_api_client, monkeypatch):
        """A range the tool cannot size is rejected, not forwarded unchecked.

        Failing open here would issue the very unbounded request the point
        budget exists to stop, so an unparseable ``from``/``to`` value is a
        typed error rather than a pass-through.
        """
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        monkeypatch.setattr(plots_module, "_now", lambda: 1_700_000_000.0)
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()

        result = await plots_module._plot_analytics_series_impl("status", from_time="-1 month", to_time="now")

        assert result["code"] == "invalid_argument"
        assert "Could not interpret the requested time range" in result["error"]
        assert not mock_api_client.stream_calls

    @pytest.mark.parametrize(
        ("from_time", "to_time", "interval"),
        [
            ("not-a-time", "now", 60),
            ("now", "not-a-time", 60),
        ],
    )
    def test_series_budget_error_unparseable_range_is_rejected(self, now, from_time, to_time, interval):
        error = plots_module._series_budget_error(from_time, to_time, interval, now=now)
        assert error is not None
        assert error["code"] == "invalid_argument"
        assert "-7d" in error["error"]
        assert error["from_time"] == from_time
        assert error["to_time"] == to_time

    @pytest.mark.parametrize(("from_time", "to_time"), [("now", "-1d"), ("-1d", "-1d"), ("now", "now")])
    def test_series_budget_error_non_positive_span_is_rejected(self, now, from_time, to_time):
        error = plots_module._series_budget_error(from_time, to_time, 60, now=now)
        assert error is not None
        assert error["code"] == "invalid_argument"
        assert "must end after it starts" in error["error"]

    def test_series_budget_error_names_the_range_it_resolved(self, now):
        """A bare number is absolute, and the error has to say so.

        Seen live: the model passed from_time=-86400 meaning "a day ago", the
        budget check read it as 1969 and rejected a 57-year span, and nothing
        in the message said which argument was at fault - so it retried three
        different intervals with the same broken range.
        """
        error = plots_module._series_budget_error(-86400, "now", 3600, now=now)
        assert error is not None
        assert "1969-12-31" in error["error"]
        assert "absolute Unix timestamp" in error["error"]
        assert error["resolved_from"].startswith("1969-12-31")

    @pytest.mark.asyncio
    async def test_no_profile_returns_error(self, clean_env):
        result = await plots_module._plot_analytics_series_impl("status")
        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.asyncio
    async def test_http_error_returns_payload(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        wrapped = AsyncMock(return_value={"error": "boom", "code": "http_error"})
        monkeypatch.setattr(plots_module, "_api_request_payload", wrapped)
        result = await plots_module._plot_analytics_series_impl("status")
        assert result["code"] == "http_error"

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_payload(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        wrapped = AsyncMock(return_value={"error": "boom", "code": "internal_error"})
        monkeypatch.setattr(plots_module, "_api_request_payload", wrapped)
        result = await plots_module._plot_analytics_series_impl("status")
        assert result["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_empty_data_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps({"meta": {"series": {"times": []}}, "data": []}).encode()
        result = await plots_module._plot_analytics_series_impl("status")
        assert "error" in result
        assert "No time-series data available" in result["error"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"data": [{"name": "x", "queries": [1]}], "meta": None},
            {"data": [{"name": "x", "queries": [1]}], "meta": {"series": None}},
            {"data": [{"name": "x", "queries": [1]}], "meta": {"series": {"times": None}}},
        ],
        ids=["meta-null", "series-null", "times-null"],
    )
    @pytest.mark.asyncio
    async def test_null_payload_fields_return_error_payload(self, payload, clean_env, mock_api_client, monkeypatch):
        # Regression (issue #286): ``dict.get(key, default)`` only falls back
        # when a key is absent, so a JSON ``null`` from the API used to make
        # ``meta.get(...)`` raise AttributeError out of the tool, breaking the
        # standardized {"error", "code"} contract every other failure path uses.
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps(payload).encode()
        result = await plots_module._plot_analytics_series_impl("status")
        assert result["code"] == "no_data"

    @pytest.mark.asyncio
    async def test_unexpected_payload_shape_returns_internal_error(self, clean_env, mock_api_client, monkeypatch):
        # A future upstream schema change must degrade to the same typed error
        # payload rather than escaping as a raw exception (issue #286).
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps(
            {
                "meta": ["not-a-mapping"],
                "data": [{"name": "x", "queries": [1]}],
            }
        ).encode()
        result = await plots_module._plot_analytics_series_impl("status")
        assert result["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_rendering_error_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps(
            {
                "meta": {"series": {"times": ["bad-timestamp"]}},
                "data": [{"name": "x", "queries": [1]}],
            }
        ).encode()
        result = await plots_module._plot_analytics_series_impl("status")
        assert "error" in result
        assert "Error rendering chart" in result["error"]

    @pytest.mark.asyncio
    async def test_success_returns_chart_result(self, clean_env, mock_api_client, sample_series_payload, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps(sample_series_payload).encode()

        result = await plots_module._plot_analytics_series_impl("status")

        assert isinstance(result, ToolResult)
        assert "NextDNS Analytics: status" in result.content[0].text
        assert isinstance(result.structured_content, dict)
        assert "$prefab" in result.structured_content
        assert "view" in result.structured_content


class TestPlotAnalyticsToolWrapper:
    """Smoke tests for the generic plotAnalytics wrapper."""

    @pytest.mark.asyncio
    async def test_plot_analytics_wrapper(self, clean_env):
        result = await server.plotAnalytics("status")
        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.asyncio
    async def test_plot_analytics_wrapper_with_default_profile(self, clean_env, monkeypatch, mock_api_client):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        wrapped = AsyncMock(return_value={"error": "boom", "code": "http_error"})
        monkeypatch.setattr("nextdns_mcp.tools.plots._api_request_payload", wrapped)
        result = await server.plotAnalytics("status")
        assert result["code"] == "http_error"


class TestPlotAnalyticsEndToEndRendering:
    """End-to-end coverage of the public ``plotAnalytics`` tool (issue #186).

    These drive the tool the way a user/agent does: the NextDNS API returns a
    series payload, the tool parses the timestamps and returns an interactive
    Prefab chart in a ToolResult.
    """

    @pytest.mark.asyncio
    async def test_renders_chart_for_non_microsecond_timestamps(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        # Real series payloads are not guaranteed to carry fractional seconds:
        # a mix of whole-second, timezone-offset, fractional and 'Z' stamps
        # must all render a chart rather than surfacing a parse error.
        _payload = {
            "meta": {
                "series": {
                    "times": [
                        "2024-01-15T10:00:00",
                        "2024-01-15T11:00:00Z",
                        "2024-01-15T12:00:00+00:00",
                        "2024-01-15T13:00:00.250",
                    ],
                },
            },
            "data": [
                {"status": "blocked", "queries": [12, 20, 15, 9]},
                {"status": "allowed", "queries": [88, 80, 85, 91]},
            ],
        }
        mock_api_client.stream_state["body"] = json.dumps(_payload).encode()

        result = await server.plotAnalytics("status")

        assert isinstance(result, ToolResult)
        assert "NextDNS Analytics: status (400 total queries across 4 intervals)" == result.content[0].text
        assert isinstance(result.structured_content, dict)
        assert "$prefab" in result.structured_content
        assert "view" in result.structured_content

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_returns_error_not_exception(self, clean_env, mock_api_client, monkeypatch):
        # A genuinely malformed stamp must not escape as an unhandled
        # ValueError from deep inside the tool: the user gets a structured
        # error payload back.
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.stream_state["body"] = json.dumps(
            {
                "meta": {"series": {"times": ["15 January 2024"]}},
                "data": [{"status": "blocked", "queries": [1]}],
            }
        ).encode()

        result = await server.plotAnalytics("status")

        assert isinstance(result, dict)
        assert result["code"] == "internal_error"
        assert "Error rendering chart" in result["error"]


class TestPlotFetchUsesSharedWrapper:
    """Regression (issue #183): the plot series fetch must route through the payload wrapper.

    Before the fix ``_fetch_series_payload`` called the raw
    ``client.api_client.get(...)`` directly, bypassing the transport's payload
    wrapper (``_api_request_payload``) and its error handling/logging - a
    parallel wrapper of its own stayed a second copy of that contract. (and any future retry/rate-limit/telemetry). These
    mock-level assertions pin the wrapped path so a raw-client regression is
    caught: the shared wrapper is awaited and the raw transport is not.
    """

    @pytest.mark.asyncio
    async def test_fetch_series_payload_uses_api_request(
        self, clean_env, monkeypatch, mock_api_client, sample_series_payload
    ):
        wrapped = AsyncMock(return_value=sample_series_payload)
        monkeypatch.setattr(plots_module, "_api_request_payload", wrapped)

        params = {"from": "-1d", "to": "now", "interval": 3600}
        payload, error = await plots_module._fetch_series_payload("/profiles/abc123/analytics/status;series", params)

        wrapped.assert_awaited_once_with(
            "GET", "/profiles/abc123/analytics/status;series", params=params, max_response_bytes=512000
        )
        # Routing moved off the raw client: the underlying transport must not be hit.
        mock_api_client.request.assert_not_awaited()
        assert error is None
        assert payload is sample_series_payload

    @pytest.mark.asyncio
    async def test_fetch_series_payload_surfaces_wrapper_error(self, clean_env, monkeypatch, mock_api_client):
        wrapped = AsyncMock(return_value={"error": "upstream failure", "code": "http_error", "status_code": 500})
        monkeypatch.setattr(plots_module, "_api_request_payload", wrapped)

        payload, error = await plots_module._fetch_series_payload("/profiles/abc123/analytics/status;series", {})

        assert payload is None
        assert error is not None
        assert error["code"] == "http_error"
        assert error["status_code"] == 500

    @pytest.mark.asyncio
    async def test_plot_analytics_series_impl_uses_api_request(self, clean_env, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        # Empty series so the impl returns before rendering; the fetch has already
        # run, which is what we are asserting on.
        wrapped = AsyncMock(return_value={"meta": {"series": {"times": []}}, "data": []})
        monkeypatch.setattr(plots_module, "_api_request_payload", wrapped)

        # Drive the impl end-to-end; the fetch must go through the shared
        # wrapper rather than the raw client.
        result = await plots_module._plot_analytics_series_impl("status")

        assert result["code"] == "no_data"
        wrapped.assert_awaited_once()
        args, kwargs = wrapped.await_args
        assert args[0] == "GET"
        assert args[1] == "/profiles/abc123/analytics/status;series"
        assert kwargs["params"]["interval"] == 3600


class TestRangeReportingAndProfileGuard:
    """What a rejected range says, and what happens when no profile comes back."""

    def test_an_unresolved_time_point_is_named_as_unresolved(self):
        """The budget error names the range it computed.

        A point that could not be parsed has to read as unresolved rather than as a
        timestamp invented from None, or the error blames the wrong argument.
        """
        assert plots_module._format_resolved(None) == "unresolved"
        assert plots_module._format_resolved(1_700_000_000).endswith("+00:00")

    @pytest.mark.asyncio
    async def test_a_plot_with_no_resolved_profile_does_not_request_a_series(self, clean_env, monkeypatch):
        """``(None, None)`` from the resolver would otherwise build /profiles//analytics/..."""
        monkeypatch.setattr(plots_module, "resolve_profile_id", lambda profile_id, **kwargs: (None, None))

        wrapped = AsyncMock(return_value={"meta": {"series": {"times": []}}, "data": []})
        monkeypatch.setattr(plots_module, "_api_request_payload", wrapped)

        result = await plots_module._plot_analytics_series_impl("status", profile_id="abc123")
        assert result["code"] == "internal_error"
        assert "profile resolution failed" in result["error"].lower()
        wrapped.assert_not_awaited()


class TestReporterIsTotal:
    """The error paths of the plotter answer as payloads, never as crashes.

    Two shapes used to escape as exceptions instead of typed errors: a time
    value outside the platform's convertible range (the reporter formats the
    very value it is reporting a refusal about), and a fetcher that returns
    neither payload nor error (the old code trusted its own contract and
    would have plotted None). Both are pinned here as payloads.
    """

    def test_a_time_point_beyond_the_platform_range_is_named_not_formatted(self):
        """``fromtimestamp`` raises on the value; the reporter names it instead.

        1e30 is a valid float but no platform can turn it into a date; an
        error path that crashes with OverflowError while describing a bad
        timestamp turns the caller's typed error into an internal one. The
        value must appear in the string exactly as the caller sent it.
        """
        assert plots_module._format_resolved(1e30) == "out of range (1e+30)"
        # The negative side overflows too on this platform.
        assert plots_module._format_resolved(-1e30).startswith("out of range (")

    @pytest.mark.asyncio
    async def test_a_fetch_returning_nothing_at_all_is_an_internal_error(self, clean_env, monkeypatch):
        """``(None, None)`` from the fetcher must not reach the renderer.

        The fetcher's contract is payload-or-error; a violation answers as
        the typed internal error naming the metric, rather than a TypeError
        on None inside the chart renderer several frames away from the truth.
        """
        monkeypatch.setattr(plots_module, "_fetch_series_payload", AsyncMock(return_value=(None, None)))

        result = await plots_module._plot_analytics_series_impl("status", profile_id="abc123")
        assert result["code"] == "internal_error"
        assert "status" in result["error"]
        assert "no payload" in result["error"].lower()


class TestUnresolvableExtremeTimes:
    """Time resolution is total: an extreme value answers None, never OverflowError.

    A 400-digit timestamp and a 400-digit relative offset both used to raise out
    of the resolution helpers - a crash where the typed "unresolved range"
    refusal is the contract (issue #267 range bounding).
    """

    def test_an_absolute_int_past_the_float_range_is_unresolved(self):
        assert plots_module._resolve_time_point(10**400, now=1_700_000_000.0) is None

    def test_a_relative_offset_past_the_float_range_is_unresolved(self):
        assert plots_module._resolve_time_point("-" + "9" * 400 + "d", now=1_700_000_000.0) is None

    def test_a_numeric_string_past_the_float_range_is_unresolved(self):
        assert plots_module._resolve_time_point("1" + "0" * 400, now=1_700_000_000.0) is None
