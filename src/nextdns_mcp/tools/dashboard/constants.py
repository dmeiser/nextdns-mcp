"""The dashboard's published section vocabulary, apart from the render path.

``DashboardSection`` is the ``section`` argument's contract and
``DASHBOARD_SECTIONS`` its materialized values - the one list the E2E sweep and
the actor harness read so they cannot drift from the published tool (#470).
They live in a module of their own so a consumer of the metadata alone imports
no Prefab UI component tree: ``tool`` pulls that tree in to render, and reading
a section name must not (issue #780).

SPDX-License-Identifier: MIT
"""

from typing import Literal, get_args

DashboardSection = Literal[
    "overview",
    "all",
    "kpi",
    "domains",
    "blocked_domains",
    "reasons",
    "devices",
    "ips",
    "root_domains",
    "countries",
    "gafam",
    "security",
]

# The section table the published tool accepts, read from the same Literal so the
# E2E sweep and the actor harness cannot drift from it (#470).
DASHBOARD_SECTIONS = get_args(DashboardSection)
