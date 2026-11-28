"""Regression coverage for the chart error and color boundaries (SEC-7).

A failed chart render must not echo an upstream or internal exception, and a
caller-supplied color must never reach Prefab's style injector unvalidated.

SPDX-License-Identifier: MIT
"""

import logging

import pytest

from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.dashboard import components as components_module

DEFAULT_BAR_COLOR = "#3b82f6"
MARKER = "chart-test-marker"


def _find_component(node, comp_type):
    """First component of ``comp_type`` in a builder's component tree, or None."""
    if getattr(node, "type", None) == comp_type:
        return node
    for child in getattr(node, "children", None) or []:
        found = _find_component(child, comp_type)
        if found is not None:
            return found
    return None


def _raise_marker(payload):
    """Stand in for an upstream failure carrying a sensitive message."""
    raise ValueError(MARKER)


@pytest.mark.parametrize("color", ["#3b82f6", "#ABCDEF", "#000000", "#ffffff"])
def test_bar_chart_passes_valid_hex_colors_unchanged(color):
    chart = components_module._bar_chart([{"label": "example.com", "queries": 1}], "Queries", color)
    assert chart.series[0].color == color


@pytest.mark.parametrize(
    "color",
    ["blue", "#3b82f", "#3b82f60", "#gggggg", "#3b82f6;background:red", "", None],
)
def test_bar_chart_falls_back_for_invalid_colors(color):
    chart = components_module._bar_chart([{"label": "example.com", "queries": 1}], "Queries", color)
    assert chart.series[0].color == DEFAULT_BAR_COLOR


def test_build_domains_card_validates_caller_color():
    card = components_module.build_domains_card(
        "Domains",
        "description",
        [("example.com", 5)],
        view_type="bar",
        color="blue;background:red",
    )
    bar_chart = _find_component(card, "BarChart")
    assert bar_chart is not None
    assert bar_chart.series[0].color == DEFAULT_BAR_COLOR


def test_queries_series_failure_log_omits_exception_text(monkeypatch, caplog):
    monkeypatch.setattr(components_module, "extract_series_fields", _raise_marker)
    with caplog.at_level(logging.WARNING, logger=components_module.logger.name):
        assert components_module._build_queries_series_card({}) is None
    assert MARKER not in caplog.text


def test_plot_render_error_is_static_for_logs_and_callers(monkeypatch, caplog):
    monkeypatch.setattr(plots_module, "extract_series_fields", _raise_marker)
    with caplog.at_level(logging.ERROR, logger=plots_module.logger.name):
        result = plots_module._plot_rendered_series("status", "abc123", {})
    assert result == {"error": "Error rendering chart", "code": "internal_error"}
    assert MARKER not in caplog.text
