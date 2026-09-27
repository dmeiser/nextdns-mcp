"""Shared analytics metric definitions for the grouped tools.

SPDX-License-Identifier: MIT
"""

from typing import Literal, get_args

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
# The literals are the single source of truth for which metrics the analytics
# tools accept: FastMCP builds each tool's JSON schema from them, and the
# runtime metric sets below are derived from the same literals so the
# advertised schema and runtime validation can never drift apart (issue #292).
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

AnalyticsMetric = Literal[
    "status",
    "domains",
    "queryTypes",
    "reasons",
    "ips",
    "dnssec",
    "encryption",
    "ipVersions",
    "protocols",
    "devices",
    "destinations",
]

# Runtime metric sets derived from the typed literals (issue #292).
PLOT_METRICS = frozenset(get_args(PlotMetric))
ANALYTICS_METRICS = frozenset(get_args(AnalyticsMetric))

# Analytics metrics that do not support series=true (issue #292).
NON_SERIES_METRICS = frozenset({"domains"})
