"""The metric set the plot tool accepts.

SPDX-License-Identifier: MIT

Every analytics *endpoint* documents its own metric in the OpenAPI spec, which the
generated tools publish directly. Charting is the one operation with no endpoint to
describe, so the metrics it can draw are declared here: the literal is what FastMCP
turns into the tool's schema, and the runtime set is derived from the same literal so
the advertised schema and the validation can never drift apart (issue #292).
"""

from typing import Literal, get_args

PlotMetric = Literal[
    "status",
    "devices",
    "protocols",
    "queryTypes",
    "ipVersions",
    "dnssec",
    "encryption",
    "reasons",
    "ips",
]

# Derived from the typed literal (issue #292), so the two cannot disagree.
PLOT_METRICS = frozenset(get_args(PlotMetric))
