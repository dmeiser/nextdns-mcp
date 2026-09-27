"""Regression tests for issue #292: the runtime metric sets are derived from
the typed literals, so the advertised schema and runtime validation share one
truth and cannot drift apart."""

from typing import get_args

from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.metrics import ANALYTICS_METRICS, NON_SERIES_METRICS, PlotMetric


def test_plot_metrics_match_schema_literal():
    # The runtime validator's metric set must equal the metrics FastMCP
    # advertises in the plotAnalytics schema (issue #292).
    assert set(get_args(PlotMetric)) == set(plots_module._PLOT_ANALYTICS_METRICS)


def test_every_plot_metric_is_an_analytics_metric():
    # Every plottable metric must also be a valid queryAnalytics metric, so
    # a metric can never be plottable-in-name-only (issue #292).
    assert set(get_args(PlotMetric)) <= set(ANALYTICS_METRICS)


def test_non_series_metrics_contain_domains():
    # The series gate must be driven by a set that actually contains the
    # metric its own Literal accepts (issue #292).
    assert "domains" in NON_SERIES_METRICS
