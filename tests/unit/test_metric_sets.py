"""Regression tests for issue #292: the runtime metric sets are derived from
the typed literals, so the metrics FastMCP advertises in the registered tool
schema and the metrics the runtime validator accepts are one truth."""

from typing import get_args

import pytest

from nextdns_mcp.errors import ErrorCode
from nextdns_mcp.server import build_mcp_server
from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.metrics import ANALYTICS_METRICS, PLOT_METRICS, PlotMetric


async def _advertised_plot_metrics() -> list[str]:
    """Return the metric enum exactly as FastMCP advertises it for plotAnalytics."""
    server = build_mcp_server()
    tool = await server.get_tool("plotAnalytics")
    assert tool is not None
    return list(tool.parameters["properties"]["metric"]["enum"])


@pytest.mark.asyncio
async def test_advertised_schema_equals_runtime_metric_set():
    # The metrics the plotAnalytics schema advertises must be exactly the set
    # the runtime validator accepts, so a schema-advertised metric can never
    # come back as unsupported_metric (issue #292).
    assert set(await _advertised_plot_metrics()) == set(PLOT_METRICS)


@pytest.mark.asyncio
async def test_every_advertised_metric_passes_runtime_validation(monkeypatch):
    # Every metric FastMCP advertises must be accepted by the real, unpatched
    # runtime validator (issue #292).
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
    for metric in await _advertised_plot_metrics():
        resolved_profile, error = plots_module._validate_plot_params(metric, 3600, None)
        assert error is None, metric
        assert resolved_profile == "abc123", metric


def test_metric_absent_from_schema_is_rejected(monkeypatch):
    # A metric FastMCP does not advertise is still rejected by the validator.
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
    resolved_profile, error = plots_module._validate_plot_params("notametric", 3600, None)
    assert resolved_profile is None
    assert error["code"] == ErrorCode.UNSUPPORTED_METRIC


def test_every_plot_metric_is_an_analytics_metric():
    # Every plottable metric must also be a valid queryAnalytics metric, so
    # a metric can never be plottable-in-name-only (issue #292).
    assert set(get_args(PlotMetric)) <= set(ANALYTICS_METRICS)
