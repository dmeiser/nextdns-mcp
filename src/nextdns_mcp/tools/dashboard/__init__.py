"""Interactive analytics dashboard with specialized analytics widgets for NextDNS.

The package is split by responsibility, one concern per module:

- ``data`` - transport calls, the bounded fan-out the pages need, KPI/rate math, and
  the shapers that turn one analytics item into one table row.
- ``components`` - the Prefab UI cards, charts, tables and page views.
- ``formatters`` - the text a model reads back beside each rendered card.
- ``constants`` - the published section vocabulary (``DashboardSection``,
  ``DASHBOARD_SECTIONS``), kept free of the component tree so metadata
  consumers never import the render path (issue #780).
- ``tool`` - the per-section implementations, the ``section`` dispatch, and the one
  published wrapper, ``overviewDashboard``.

Import the surface from here (``from nextdns_mcp.tools.dashboard import
overviewDashboard``); ``overviewDashboard`` resolves lazily so reading the
section metadata above does not load the Prefab UI component tree. Names
beginning with ``_`` inside the submodules are
module-internal, while helpers shared between submodules cross under public
names (issue #637). Shared chart and time-resolution primitives are not here -
the pure ones live in ``nextdns_mcp.charts`` and the Prefab chart builders in
``nextdns_mcp.chart_ui``, neither of which this package or ``tools.plots`` owns.

SPDX-License-Identifier: MIT
"""

from typing import Any

from .constants import DASHBOARD_SECTIONS, DashboardSection

__all__ = ["DASHBOARD_SECTIONS", "DashboardSection", "overviewDashboard"]


def __getattr__(name: str) -> Any:
    """Resolve ``overviewDashboard`` on first access (PEP 562).

    The wrapper lives in ``tool``, whose import chain loads the Prefab UI
    component tree; importing it eagerly here would put that tree into every
    process that only reads ``DASHBOARD_SECTIONS`` (issue #780). The package
    surface is unchanged - ``from nextdns_mcp.tools.dashboard import
    overviewDashboard`` works exactly as before.
    """
    if name == "overviewDashboard":
        from .tool import overviewDashboard

        return overviewDashboard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
