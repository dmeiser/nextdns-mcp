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

What is common to both surfaces but is not a chart - the ceiling on the whole
rendered ``PrefabApp`` they answer with - is guarded here too, by
``bounded_app_result``.

SPDX-License-Identifier: MIT
"""

import json
from typing import Any

from fastmcp.tools.base import ToolResult
from prefab_ui.app import PrefabApp, Theme
from prefab_ui.components.charts import AreaChart, ChartSeries

from .errors import ErrorCode, error_payload
from .utils import sanitize_label

# Byte ceiling on one rendered analytics result, however many cards it holds.
# Both hand-written analytics surfaces answer with a whole ``PrefabApp`` in
# ``structured_content``, and that document goes straight into the calling model's
# context: the caps in ``charts`` bound what a single chart can hold, this bounds
# what a page of them can cost. It is the same size the generative-UI sandbox
# puts on a generated view (``MAX_RESULT_BYTES`` in ``generative_sandbox``) - one
# rendered app is one rendered app, whatever produced it. A page that still
# breaches it is refused with a typed payload rather than trimmed: a silently
# shorter dashboard is one the caller cannot tell was cut (issue #789).
MAX_RESULT_BYTES = 256 * 1024


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
    """Return a human-readable label for a time-series data entry.

    Series names come from the upstream API, so the label is sanitized here -
    the single extraction point every chart series passes through.
    """
    for key in ("name", "status", "protocol", "version", "id"):
        value = series.get(key)
        if value is not None and value != "":
            return sanitize_label(value)
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


def rendered_app_bytes(app: PrefabApp) -> int:
    """UTF-8 size of a rendered app as the MCP wire carries it.

    Measured on ``PrefabApp.to_json`` - the document ``structured_content``
    holds once the tool result is built - serialized exactly as the stdio server
    writes it: compact JSON with raw UTF-8, no ``\\uXXXX`` escapes. Escaping
    non-ASCII labels charges up to six bytes per character and overstates the
    ceiling decision; the MCP wire does not pay those bytes.
    """
    return len(json.dumps(app.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bounded_app_result(app: PrefabApp, summary: str, surface: str) -> ToolResult | dict[str, Any]:
    """Wrap a rendered app in a ToolResult, or refuse it when it breaches the byte ceiling.

    Both analytics surfaces end here, so the ceiling is one gate rather than a
    check each surface remembers to write. Over it, the answer is the typed
    ``response_too_large`` payload the transport already uses for an oversized
    upstream document, naming the size and the limit; the caller lowers ``limit``
    or renders one section instead of getting a page that was quietly cut down.
    """
    size = rendered_app_bytes(app)
    if size > MAX_RESULT_BYTES:
        return error_payload(
            ErrorCode.RESPONSE_TOO_LARGE,
            (
                f"Rendered {surface} is {size:,} bytes, over the {MAX_RESULT_BYTES:,}-byte result ceiling; "
                "request fewer rows or series, or render one section at a time"
            ),
            result_bytes=size,
            max_result_bytes=MAX_RESULT_BYTES,
        )
    return ToolResult(content=summary, structured_content=app)
