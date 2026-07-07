# ---
# Extra Field Relaxation for MCP Tool Arguments
#
# AI clients (like OpenAI) often send extra/unknown fields with tool calls.
# We use a complementary two-layer approach to handle this:
#
# 1. StripExtraFieldsMiddleware: Intercepts tool calls and filters arguments
#    to only include fields defined in the tool's schema. This operates at the
#    MCP call level, preventing most validation errors from unknown fields.
#
# 2. allow_extra_fields_component_fn: Configures OpenAPI-imported Pydantic models
#    with "extra": "ignore" (via strict_input_validation=False), ensuring that any
#    extra fields that reach model validation are silently ignored.
#
# These mechanisms work together at different layers to ensure:
# - Unknown fields are silently ignored (not rejected)
# - Required/typed fields are still validated
# - Works with both OpenAPI-imported and custom @mcp_server.tool() decorated tools
#
# See docs/troubleshooting.md for details.
# ruff: noqa: E402
"""NextDNS MCP Server - FastMCP-based implementation using OpenAPI spec.

SPDX-License-Identifier: MIT
"""

import logging
import os
from pathlib import Path  # noqa: F401
from typing import Any

from dotenv import load_dotenv

# Load environment variables from .env file first
load_dotenv()

# Disable FastMCP automatic update checks to prevent startup delays and hangs in offline/CI environments
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from fastmcp import FastMCP  # noqa: F401

from .client import api_client
from .config import (
    NEXTDNS_BASE_URL,
    get_http_timeout,
    validate_configuration,
)
from .openapi import create_mcp_server
from .tools.analytics import queryAnalytics
from .tools.doh import dohLookup
from .tools.lists import manageLists
from .tools.logs import manageLogs
from .tools.plots import plotAnalytics
from .tools.profiles import manageProfiles
from .tools.rewrites import manageRewrites
from .tools.settings import manageSettings
from .usage import nextdns_usage_guide

logger = logging.getLogger(__name__)

# Replicate the original server.py log message on import for backward compatibility.
logger.info(f"Creating HTTP client for {NEXTDNS_BASE_URL}")


# Create the MCP server instance
mcp_server = create_mcp_server(api_client)

# Backward-compatible alias
mcp = mcp_server

# Register the grouped MCP tools
manageProfiles = mcp_server.tool()(manageProfiles)
manageSettings = mcp_server.tool()(manageSettings)
manageLists = mcp_server.tool()(manageLists)
manageRewrites = mcp_server.tool()(manageRewrites)
manageLogs = mcp_server.tool()(manageLogs)
queryAnalytics = mcp_server.tool()(queryAnalytics)
plotAnalytics = mcp_server.tool()(plotAnalytics)
dohLookup = mcp_server.tool()(dohLookup)

# Register the usage guide prompt
nextdns_usage_guide = mcp_server.prompt(
    name="nextdns-usage-guide",
    description="Comprehensive guide for using the NextDNS MCP server tools",
)(nextdns_usage_guide)


def get_mcp_run_options() -> dict[str, Any]:
    """Build MCP server run options based on environment configuration.

    Returns:
        Dictionary of options to pass to mcp.run() via **kwargs.
        Empty dict for stdio (default), or dict with transport/host/port for HTTP.
    """
    transport_mode = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport_mode == "http":
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        logger.info(f"  Transport: HTTP streamable on {host}:{port}")
        logger.info(f"  MCP endpoint: http://{host}:{port}/mcp")
        return {"transport": "http", "host": host, "port": port}

    # Default: stdio transport
    logger.info("  Transport: stdio")
    return {}


# Re-exports for backward compatibility
from .client import (  # noqa: F401
    AccessControlledClient,
    create_access_denied_response,
    create_nextdns_client,
    extract_profile_id_from_url,
    is_write_operation,
)
from .config import get_api_key  # noqa: F401
from .coercion import (  # noqa: F401
    OptionalProfileId,
    ProfileId,
    _coerce_dict,
    _coerce_json_arg,
    _coerce_list,
    _coerce_profile_id,
    _coerce_string,
    _coerce_string_to_bool,
    _coerce_string_to_number,
    _is_integer,
    _try_parse_float,
    coerce_json_types,
)
from .openapi import (  # noqa: F401
    StripExtraFieldsMiddleware,
    allow_extra_fields_component_fn,
    build_route_mappings,
    get_openapi_tool_names,
    load_openapi_spec,
)
from .tools.analytics import AnalyticsMetric, _query_analytics_impl  # noqa: F401
from .tools.doh import (  # noqa: F401
    _build_doh_metadata,
    _dohLookup_impl,
    _get_target_profile,
    _validate_record_type,
    doh_lookup,
)
from .tools.lists import (  # noqa: F401
    ListOperation,
    ListType,
    _LIST_PATHS,
    _LIST_UPDATEABLE_TYPES,
    _lists_add,
    _lists_get,
    _lists_remove,
    _lists_replace,
    _lists_update,
    _manage_lists_impl,
)
from .tools.logs import LogOperation, _manage_logs_impl  # noqa: F401
from .tools.plots import (  # noqa: F401
    PlotMetric,
    _PLOT_ANALYTICS_METRICS,
    _extract_series_label,
    _parse_series_timestamp,
    _plot_analytics_series_impl,
    _render_series_chart,
)
from .tools.profiles import ProfileOperation, _manage_profiles_impl  # noqa: F401
from .tools.rewrites import RewriteOperation, _manage_rewrites_impl  # noqa: F401
from .tools.settings import SettingsCategory, _SETTINGS_PATHS, _manage_settings_impl  # noqa: F401
from .utils import (  # noqa: F401
    SAFE_ENTRY_ID_PATTERN,
    SAFE_PROFILE_ID_PATTERN,
    _api_request,
    _build_query_params,
    _validate_entry_id,
    _validate_profile_id,
    is_safe_entry_id,
    is_safe_profile_id,
)

if __name__ == "__main__":  # pragma: no cover
    # Note: This block is excluded from unit test coverage because:
    # 1. It only executes when running `python -m nextdns_mcp.server` directly
    # 2. When pytest imports the module, __name__ != "__main__"
    # 3. The mcp_server.run() call starts a blocking event loop unsuitable for unit tests
    # The options building logic IS tested via tests/unit/test_mcp_run_options.py
    logger.info("Starting NextDNS MCP Server...")
    logger.info(f"  Base URL: {NEXTDNS_BASE_URL}")
    logger.info(f"  Timeout: {get_http_timeout()}s")
    validate_configuration()
    mcp_server.run(**get_mcp_run_options())
