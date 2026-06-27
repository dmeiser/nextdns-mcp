"""Tests for the analytics plotting helpers and tool wrappers."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextdns_mcp import server


class TestExtractSeriesLabel:
    """Tests for _extract_series_label."""

    def test_name_label(self):
        assert server._extract_series_label({"name": "example.com"}, 0) == "example.com"

    def test_status_label(self):
        assert server._extract_series_label({"status": "blocked"}, 0) == "blocked"

    def test_protocol_label(self):
        assert server._extract_series_label({"protocol": "DoH"}, 0) == "DoH"

    def test_version_label(self):
        assert server._extract_series_label({"version": "IPv4"}, 0) == "IPv4"

    def test_id_label(self):
        assert server._extract_series_label({"id": "device1"}, 0) == "device1"

    def test_validated_true_label(self):
        assert server._extract_series_label({"validated": True}, 0) == "validated"

    def test_validated_false_label(self):
        assert server._extract_series_label({"validated": False}, 0) == "not_validated"

    def test_encrypted_true_label(self):
        assert server._extract_series_label({"encrypted": True}, 0) == "encrypted"

    def test_encrypted_false_label(self):
        assert server._extract_series_label({"encrypted": False}, 0) == "unencrypted"

    def test_fallback_index_label(self):
        assert server._extract_series_label({"queries": []}, 3) == "series_3"


class TestParseSeriesTimestamp:
    """Tests for _parse_series_timestamp."""

    def test_parses_z_timestamp(self):
        ts = server._parse_series_timestamp("2024-01-15T10:30:00Z")
        assert ts.year == 2024
        assert ts.month == 1
        assert ts.day == 15
        assert ts.hour == 10
        assert ts.minute == 30

    def test_parses_offset_timestamp(self):
        ts = server._parse_series_timestamp("2024-01-15T10:30:00+00:00")
        assert ts.year == 2024

    def test_parses_microseconds_fallback(self):
        ts = server._parse_series_timestamp("2024-01-15T10:30:00.123456+00:00")
        assert ts.year == 2024


class TestRenderSeriesChart:
    """Tests for _render_series_chart."""

    def test_renders_png_bytes(self):
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [
            {"name": "blocked", "queries": [10, 20]},
            {"name": "allowed", "queries": [5, 8]},
        ]
        png = server._render_series_chart("status", times, series_data)
        assert isinstance(png, bytes)
        assert png.startswith(b"\x89PNG")

    def test_renders_with_default_label(self):
        times = ["2024-01-15T10:00:00Z"]
        series_data = [{"queries": [1]}]
        png = server._render_series_chart("reasons", times, series_data)
        assert isinstance(png, bytes)
        assert png.startswith(b"\x89PNG")


@pytest.fixture
def mock_api_client(monkeypatch):
    """Patch the module-level api_client.get used by plotting helpers."""
    client = AsyncMock()
    monkeypatch.setattr(server, "api_client", client)
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
        result = await server._plot_analytics_series_impl("notametric")
        assert "error" in result
        assert "Unsupported metric" in result["error"]

    @pytest.mark.asyncio
    async def test_interval_too_small_returns_error(self, clean_env):
        result = await server._plot_analytics_series_impl("status", interval=30)
        assert "error" in result
        assert "interval must be at least 60" in result["error"]

    @pytest.mark.asyncio
    async def test_no_profile_returns_error(self, clean_env):
        result = await server._plot_analytics_series_impl("status")
        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.asyncio
    async def test_http_error_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.get.side_effect = httpx.HTTPError("boom")
        result = await server._plot_analytics_series_impl("status")
        assert "error" in result
        assert "HTTP error" in result["error"]

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.get.side_effect = RuntimeError("unexpected")
        result = await server._plot_analytics_series_impl("status")
        assert "error" in result
        assert "Unexpected error" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_data_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        response = MagicMock()
        response.json.return_value = {"meta": {"series": {"times": []}}, "data": []}
        mock_api_client.get.return_value = response
        result = await server._plot_analytics_series_impl("status")
        assert "error" in result
        assert "No time-series data available" in result["error"]

    @pytest.mark.asyncio
    async def test_rendering_error_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        response = MagicMock()
        response.json.return_value = {
            "meta": {"series": {"times": ["bad-timestamp"]}},
            "data": [{"name": "x", "queries": [1]}],
        }
        mock_api_client.get.return_value = response
        result = await server._plot_analytics_series_impl("status")
        assert "error" in result
        assert "Error rendering chart" in result["error"]

    @pytest.mark.asyncio
    async def test_success_returns_image_content(self, clean_env, mock_api_client, sample_series_payload, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        response = MagicMock()
        response.json.return_value = sample_series_payload
        mock_api_client.get.return_value = response

        result = await server._plot_analytics_series_impl("status")

        assert hasattr(result, "type") or isinstance(result, dict)
        if isinstance(result, dict):
            # FastMCP Image.to_image_content may return a dict-like ImageContent on some versions
            assert result.get("type") == "image"
        else:
            assert result.type == "image"


class TestPlotAnalyticsToolWrappers:
    """Smoke tests for each plotting wrapper to cover the await return line."""

    @pytest.mark.asyncio
    async def test_plot_analytics_series_wrapper(self, clean_env):
        result = await server.plotAnalyticsSeries("status")
        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.parametrize(
        "tool_name",
        [
            "plotAnalyticsStatus",
            "plotAnalyticsDomains",
            "plotAnalyticsDevices",
            "plotAnalyticsProtocols",
            "plotAnalyticsQueryTypes",
            "plotAnalyticsIPVersions",
            "plotAnalyticsDNSSEC",
            "plotAnalyticsEncryption",
            "plotAnalyticsReasons",
        ],
    )
    @pytest.mark.asyncio
    async def test_per_metric_wrapper(self, clean_env, tool_name):
        tool = getattr(server, tool_name)
        result = await tool()
        assert "error" in result
        assert "No profile_id provided" in result["error"]
