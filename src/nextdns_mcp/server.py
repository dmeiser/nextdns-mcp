"""NextDNS MCP Server - FastMCP-based implementation built from grouped CRUD tools.

SPDX-License-Identifier: MIT
"""

import logging
import os
from typing import Any

# Disable FastMCP automatic update checks to prevent startup delays and hangs in offline/CI environments
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from fastmcp import FastMCP

from .client import get_api_client
from .config import (
    NEXTDNS_BASE_URL,
    configure_logging,
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

# Grouped MCP tools to register on the server instance
_GROUPED_TOOLS = (
    manageProfiles,
    manageSettings,
    manageLists,
    manageRewrites,
    manageLogs,
    queryAnalytics,
    plotAnalytics,
    dohLookup,
)

_mcp_server: FastMCP | None = None


def build_mcp_server() -> FastMCP:
    """Create the MCP server and register the grouped tools and usage prompt.

    Returns:
        FastMCP: Configured MCP server instance with all tools registered
    """
    logger.info(f"Creating HTTP client for {NEXTDNS_BASE_URL}")
    server = create_mcp_server()
    for tool in _GROUPED_TOOLS:
        server.tool()(tool)
    server.prompt(name="nextdns-usage-guide", description="Comprehensive guide for using the NextDNS MCP server tools")(
        nextdns_usage_guide
    )
    return server


def get_mcp_server() -> FastMCP:
    """Return the shared MCP server instance, creating it on first use."""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = build_mcp_server()
    return _mcp_server


def __getattr__(name: str) -> Any:
    """Lazily expose the ``mcp_server``/``mcp`` server and ``api_client`` singletons for backward compatibility."""
    if name in ("mcp_server", "mcp"):
        return get_mcp_server()
    if name == "api_client":
        return get_api_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_loopback_host(host: str) -> bool:
    """Return True if host binds to loopback interfaces only.

    Args:
        host: Hostname or IP address to check.

    Returns:
        True when the bind is restricted to loopback (127.0.0.1, ::1, localhost).
    """
    return host in ("127.0.0.1", "::1", "localhost")


def get_mcp_run_options() -> dict[str, Any]:
    """Build MCP server run options based on environment configuration.

    Returns:
        Dictionary of options to pass to mcp.run() via **kwargs.
        Empty dict for stdio (default), or dict with transport/host/port for HTTP.

        HTTP mode binds to 127.0.0.1 by default (loopback-only, no authentication
        is built in). Binding to any other interface is an explicit opt-in via
        MCP_HOST and requires the operator to provide reverse-proxy/auth
        protection before exposing the port.
    """
    transport_mode = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport_mode == "http":
        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_PORT", "8000"))
        logger.info(f"  Transport: HTTP streamable on {host}:{port}")
        logger.info(f"  MCP endpoint: http://{host}:{port}/mcp")
        if not _is_loopback_host(host):
            logger.warning(
                f"  SECURITY: binding to {host} exposes the MCP endpoint on all "
                "reachable interfaces with NO authentication. This is an explicit "
                "opt-in. Put a reverse proxy with authentication in front, or keep "
                "the bind loopback-only (127.0.0.1) for local use."
            )
        return {"transport": "http", "host": host, "port": port}

    # Default: stdio transport
    logger.info("  Transport: stdio")
    return {}


def _run_server() -> None:
    """Validate configuration and start the MCP server (blocking)."""
    configure_logging()
    logger.info("Starting NextDNS MCP Server...")
    logger.info(f"  Base URL: {NEXTDNS_BASE_URL}")
    logger.info(f"  Timeout: {get_http_timeout()}s")
    validate_configuration()
    get_mcp_server().run(**get_mcp_run_options())


if __name__ == "__main__":  # pragma: no cover
    # Note: This block is excluded from unit test coverage because:
    # 1. It only executes when running `python -m nextdns_mcp.server` directly
    # 2. When pytest imports the module, __name__ != "__main__"
    # 3. The mcp_server.run() call starts a blocking event loop unsuitable for unit tests
    # The options building logic IS tested via tests/unit/test_mcp_run_options.py
    # Load .env only for the real entrypoint (issue #177): calling load_dotenv() at
    # import time made importing server.py from any working directory walk upward and
    # load whatever .env it found into the importing process.
    from dotenv import load_dotenv

    load_dotenv()
    _run_server()
