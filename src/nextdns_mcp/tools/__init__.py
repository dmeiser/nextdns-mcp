"""NextDNS MCP custom tool implementations.

SPDX-License-Identifier: MIT
"""

from .analytics import AnalyticsMetric, _query_analytics_impl, queryAnalytics
from .doh import _build_doh_metadata, _dohLookup_impl, _get_target_profile, _validate_record_type, doh_lookup, dohLookup
from .lists import (
    _LIST_PATHS,
    _LIST_UPDATEABLE_TYPES,
    ListOperation,
    ListType,
    _lists_add,
    _lists_get,
    _lists_remove,
    _lists_replace,
    _lists_update,
    _manage_lists_impl,
    manageLists,
)
from .logs import LogOperation, _manage_logs_impl, manageLogs
from .plots import (
    _PLOT_ANALYTICS_METRICS,
    PlotMetric,
    _extract_series_label,
    _parse_series_timestamp,
    _plot_analytics_series_impl,
    _render_series_chart,
    plotAnalytics,
)
from .profiles import ProfileOperation, _manage_profiles_impl, manageProfiles
from .rewrites import RewriteOperation, _manage_rewrites_impl, manageRewrites
from .settings import _SETTINGS_PATHS, SettingsCategory, _manage_settings_impl, manageSettings

__all__ = [
    "AnalyticsMetric",
    "ListOperation",
    "ListType",
    "LogOperation",
    "PlotMetric",
    "ProfileOperation",
    "RewriteOperation",
    "SettingsCategory",
    "_LIST_PATHS",
    "_LIST_UPDATEABLE_TYPES",
    "_PLOT_ANALYTICS_METRICS",
    "_SETTINGS_PATHS",
    "_build_doh_metadata",
    "_dohLookup_impl",
    "_extract_series_label",
    "_get_target_profile",
    "_lists_add",
    "_lists_get",
    "_lists_remove",
    "_lists_replace",
    "_lists_update",
    "_manage_lists_impl",
    "_manage_logs_impl",
    "_manage_profiles_impl",
    "_manage_rewrites_impl",
    "_manage_settings_impl",
    "_parse_series_timestamp",
    "_plot_analytics_series_impl",
    "_query_analytics_impl",
    "_render_series_chart",
    "_validate_record_type",
    "doh_lookup",
    "dohLookup",
    "manageLists",
    "manageLogs",
    "manageProfiles",
    "manageRewrites",
    "manageSettings",
    "plotAnalytics",
    "queryAnalytics",
]
