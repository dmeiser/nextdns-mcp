"""Tests for the analytics plotting helpers and tool wrappers."""

import struct
import threading
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import server
from nextdns_mcp.tools import plots as plots_module


def _png_is_complete(png: bytes) -> bool:
    """Return True if the PNG bytes end with a proper IEND chunk."""
    return png.endswith(b"IEND\xae\x42\x60\x82")


def _png_image_size(png: bytes) -> tuple[int, int]:
    """Return the (width, height) encoded in a PNG's IHDR chunk."""
    assert png.startswith(b"\x89PNG")
    length, chunk_type = struct.unpack(">I4s", png[8:16])
    assert chunk_type == b"IHDR" and length == 13
    width, height = struct.unpack(">II", png[16:24])
    return width, height


class TestNoEagerMatplotlibImport:
    """Regression tests (issue #165): importing the server must not import matplotlib."""

    def test_importing_server_does_not_import_matplotlib(self):
        # Importing the server (and thus every tool module, including the plot
        # tool module) in a fresh interpreter must not pull matplotlib in:
        # its ~2s cold import should only be paid by the plot tool path.
        import subprocess
        import sys

        # The child interpreter imports the server and the tools package (which
        # pulls in the plot tool module) and asserts matplotlib never made it
        # into sys.modules: the ~2s matplotlib import is only paid on the plot path.
        code = "import sys\n"
        code += "import nextdns_mcp.server\n"
        code += "import nextdns_mcp.tools\n"
        code += "assert not any(m == 'matplotlib' or m.startswith('matplotlib.') for m in sys.modules)\n"
        code += "print('ok')\n"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"

    def test_plot_path_still_renders_headless(self):
        # The lazy plot path must still render a complete PNG using the Agg
        # backend, so headless servers keep working after making the import lazy.
        import matplotlib

        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [{"name": "blocked", "queries": [1, 2]}]

        png = plots_module._render_series_chart("status", times, series_data)
        assert png.startswith(b"\x89PNG")
        assert _png_is_complete(png)
        assert matplotlib.get_backend().lower() == "agg"


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


class TestParseSeriesTimestamp:
    """Tests for _parse_series_timestamp."""

    def test_parses_z_timestamp(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00Z")
        assert ts.year == 2024
        assert ts.month == 1
        assert ts.day == 15
        assert ts.hour == 10
        assert ts.minute == 30

    def test_parses_offset_timestamp(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00+00:00")
        assert ts.year == 2024

    def test_parses_microseconds_fallback(self):
        ts = plots_module._parse_series_timestamp("2024-01-15T10:30:00.123456+00:00")
        assert ts.year == 2024


class TestRenderSeriesChart:
    """Tests for _render_series_chart."""

    def test_renders_png_bytes(self):
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [
            {"name": "blocked", "queries": [10, 20]},
            {"name": "allowed", "queries": [5, 8]},
        ]
        png = plots_module._render_series_chart("status", times, series_data)
        assert isinstance(png, bytes)
        assert png.startswith(b"\x89PNG")

    def test_renders_with_default_label(self):
        times = ["2024-01-15T10:00:00Z"]
        series_data = [{"queries": [1]}]
        png = plots_module._render_series_chart("reasons", times, series_data)
        assert isinstance(png, bytes)
        assert png.startswith(b"\x89PNG")

    def test_renders_complete_png_with_labels(self):
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [
            {"name": "blocked", "queries": [10, 20]},
            {"name": "allowed", "queries": [5, 8]},
        ]
        png = plots_module._render_series_chart("status", times, series_data)
        assert _png_is_complete(png)
        width, height = _png_image_size(png)
        assert width > 0 and height > 0


class TestRenderSeriesChartFigureSafety:
    """Regression tests: rendering exceptions must not leak figure state."""

    def _figure_registry_size(self) -> int:
        # Figures registered with pyplot's global state. Rendering uses the
        # object-oriented Figure API, so this registry must stay unchanged
        # whether a render succeeds or raises.
        import matplotlib

        return len(matplotlib._pylab_helpers.Gcf.figs)

    def test_render_exception_after_figure_creation_does_not_leak(self, monkeypatch):
        # Force an exception inside the try block, after the Figure has been
        # created and the axes added, so only the finally fig.clear() can
        # keep global figure state from leaking. Patch the plots module's own
        # global, which is where _render_series_chart resolves the name.
        import nextdns_mcp.tools.plots as plots_module

        def _raise(series, index):
            raise RuntimeError("boom")

        monkeypatch.setattr(plots_module, "_extract_series_label", _raise)
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        series_data = [{"name": "blocked", "queries": [1, 2]}]

        before = self._figure_registry_size()
        with pytest.raises(RuntimeError, match="boom"):
            plots_module._render_series_chart("status", times, series_data)
        after = self._figure_registry_size()
        assert after == before

    def test_failed_render_does_not_register_figures(self):
        # Corrupt a timestamp so the render raises before figure creation.
        times = ["not-a-timestamp", "2024-01-15T11:00:00Z"]
        series_data = [{"name": "blocked", "queries": [1, 2]}]

        before = self._figure_registry_size()
        with pytest.raises(ValueError):
            plots_module._render_series_chart("status", times, series_data)
        after = self._figure_registry_size()
        assert after == before

    def test_successful_render_does_not_register_figures(self):
        before = self._figure_registry_size()
        times = ["2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z"]
        plots_module._render_series_chart("status", times, [{"name": "blocked", "queries": [1, 2]}])
        after = self._figure_registry_size()
        assert after == before


class TestConcurrentRenderIndependence:
    """Concurrent renders (as under asyncio.to_thread) must not corrupt each other."""

    def test_concurrent_renders_are_independent(self):
        n_threads = 8
        iterations = 5
        errors: list[str] = []

        def worker(worker_index: int) -> None:
            try:
                for i in range(iterations):
                    times = [f"2024-01-15T{10 + i}:00:00Z", f"2024-01-15T{11 + i}:00:00Z"]
                    series_data = [
                        {
                            "name": f"series-{worker_index}",
                            "queries": [worker_index * 10 + i, worker_index * 10 + i + 1],
                        },
                    ]
                    png = plots_module._render_series_chart(f"metric-{worker_index}", times, series_data)
                    if not png.startswith(b"\x89PNG") or not _png_is_complete(png):
                        errors.append(f"worker {worker_index}: corrupt PNG")
            except Exception as e:  # noqa: BLE001
                errors.append(f"worker {worker_index}: {e!r}")

        threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_no_global_figures_accumulate_under_concurrency(self):
        import matplotlib

        def worker(worker_index: int) -> None:
            for i in range(3):
                times = [f"2024-01-15T{10 + i}:00:00Z", f"2024-01-15T{11 + i}:00:00Z"]
                plots_module._render_series_chart("status", times, [{"queries": [1, 2]}])

        def count() -> int:
            return len(matplotlib._pylab_helpers.Gcf.figs)

        before = count()
        threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        after = count()
        assert after == before


@pytest.fixture
def mock_api_client(monkeypatch):
    """Patch the module-level api_client.get used by plotting helpers."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    client = AsyncMock()
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
    async def test_no_profile_returns_error(self, clean_env):
        result = await plots_module._plot_analytics_series_impl("status")
        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.asyncio
    async def test_http_error_returns_payload(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.get.side_effect = httpx.HTTPError("boom")
        result = await plots_module._plot_analytics_series_impl("status")
        assert result["code"] == "http_error"

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_payload(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.get.side_effect = RuntimeError("unexpected")
        result = await plots_module._plot_analytics_series_impl("status")
        assert result["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_empty_data_returns_error(self, clean_env, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        response = MagicMock()
        response.json.return_value = {"meta": {"series": {"times": []}}, "data": []}
        mock_api_client.get.return_value = response
        result = await plots_module._plot_analytics_series_impl("status")
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
        result = await plots_module._plot_analytics_series_impl("status")
        assert "error" in result
        assert "Error rendering chart" in result["error"]

    @pytest.mark.asyncio
    async def test_success_returns_image_content(self, clean_env, mock_api_client, sample_series_payload, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        response = MagicMock()
        response.json.return_value = sample_series_payload
        mock_api_client.get.return_value = response

        result = await plots_module._plot_analytics_series_impl("status")

        assert hasattr(result, "type") or isinstance(result, dict)
        if isinstance(result, dict):
            # FastMCP Image.to_image_content may return a dict-like ImageContent on some versions
            assert result.get("type") == "image"
        else:
            assert result.type == "image"


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
        mock_api_client.get.side_effect = httpx.HTTPError("boom")
        result = await server.plotAnalytics("status")
        assert result["code"] == "http_error"
