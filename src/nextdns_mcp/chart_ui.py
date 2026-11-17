"""The Prefab UI chart builders both hand-written analytics surfaces render.

This module is the UI half of the split of the old ``charts`` module (issue
#550): everything here constructs a Prefab component, and ``charts`` - the
range-resolution, payload-shape and series-budget primitives - constructs
nothing. The split exists because ``spec_tools``, the door every generated tool
stands behind, reads only the budgets from ``charts``; a ``prefab_ui`` import in
that module's dependency chain pulled the whole component tree into every
process that never renders a chart.

Like ``charts``, neither analytics surface owns this module: ``tools.plots``
plots one series, ``tools.dashboard`` renders many, and both build their
time-series chart through ``build_timeseries_area_chart`` here. The dashboard
package may not construct an ``AreaChart`` itself - ``tests/unit/test_dashboard_boundaries.py``
holds that line.

SPDX-License-Identifier: MIT
"""

from typing import Any

from prefab_ui.app import Theme
from prefab_ui.components.charts import AreaChart, ChartSeries

DASHBOARD_THEME = Theme(
    accent=220,
    css="""
:root, .dark {
  --chart-1: #3b82f6;
  --chart-2: #10b981;
  --chart-3: #f59e0b;
  --chart-4: #ef4444;
  --chart-5: #8b5cf6;
}
""",
)


def _series_boolean_label(series: dict[str, Any], index: int) -> str:
    """Label a status-shaped entry by its boolean flag, or by position when it has neither."""
    if "validated" in series:
        return "validated" if series["validated"] else "not_validated"
    if "encrypted" in series:
        return "encrypted" if series["encrypted"] else "unencrypted"
    return f"series_{index}"


def _extract_series_label(series: dict[str, Any], index: int) -> str:
    """Return a human-readable label for a time-series data entry."""
    for key, value in ((key, series.get(key)) for key in ("name", "status", "protocol", "version", "id")):
        if value is not None and value != "":
            return str(value)
    return _series_boolean_label(series, index)


_STATUS_COLORS = {
    "blocked": "#ef4444",
    "default": "#3b82f6",
    "allowed": "#3b82f6",
}


def _series_color(series: dict[str, Any]) -> str | None:
    """Color blocked queries red and allowed/default queries blue."""
    for key in ("status", "name"):
        val = str(series.get(key) or "").lower()
        if val in _STATUS_COLORS:
            return _STATUS_COLORS[val]
    return None


def build_chart_series(series_data: list[dict[str, Any]]) -> list[ChartSeries]:
    """Build the series metadata list for the Prefab LineChart."""
    return [
        ChartSeries(
            dataKey=f"series_{idx}",
            label=_extract_series_label(series, idx),
            color=_series_color(series),
        )
        for idx, series in enumerate(series_data)
    ]


def build_timeseries_area_chart(
    data: list[dict[str, Any]],
    series: list[ChartSeries],
) -> AreaChart:
    """Build the time-series ``AreaChart`` both hand-written surfaces render.

    ``tools.plots`` and the dashboard's queries card each carried their own copy
    of the same component kwargs - smooth curve, no dots, 260px tall, legend,
    tooltip and grid on, x-axis the ``time`` key of ``build_chart_data`` rows -
    alongside the shared data and series builders (issue #439). Copies of
    a configuration can only drift, and a drift here would show up as the two
    surfaces rendering the same series differently; this module, which neither
    surface owns, is where that configuration lives.
    """
    return AreaChart(
        data=data,
        series=series,
        xAxis="time",
        curve="smooth",
        showDots=False,
        height=260,
        showLegend=True,
        showTooltip=True,
        showGrid=True,
    )
