"""Interactive analytics dashboard with specialized analytics widgets for NextDNS.

The package is split by responsibility, one concern per module:

- ``data`` - transport calls, the bounded fan-out the pages need, KPI/rate math, and
  the shapers that turn one analytics item into one table row.
- ``components`` - the Prefab UI cards, charts, tables and page views.
- ``formatters`` - the text a model reads back beside each rendered card.
- ``tool`` - the per-section implementations, the ``section`` dispatch, and the one
  published wrapper, ``overviewDashboard``.

Import the surface from here (``from nextdns_mcp.tools.dashboard import
overviewDashboard``); names beginning with ``_`` inside the submodules are
package-internal. Shared chart and time-resolution primitives are not here -
the pure ones live in ``nextdns_mcp.charts`` and the Prefab chart builders in
``nextdns_mcp.chart_ui``, neither of which this package or ``tools.plots`` owns.

SPDX-License-Identifier: MIT
"""

from .tool import DASHBOARD_SECTIONS, DashboardSection, overviewDashboard

__all__ = ["DASHBOARD_SECTIONS", "DashboardSection", "overviewDashboard"]
