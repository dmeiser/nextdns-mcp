"""NextDNS MCP Server - FastMCP-based implementation built from grouped CRUD tools.

SPDX-License-Identifier: MIT
"""

import logging
import os
import sys
from typing import Any

from fastmcp import FastMCP

from .client import get_api_client
from .config import (
    NEXTDNS_BASE_URL,
    ConfigurationError,
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


def _sync_fastmcp_update_check() -> None:
    """Mirror ``FASTMCP_CHECK_FOR_UPDATES`` into FastMCP's live settings object.

    ``fastmcp/__init__.py`` builds its settings at package import time, and
    importing this module pulls FastMCP in transitively (via ``.tools.plots``
    and ``.openapi``). Setting the variable after that import would be
    silently ignored, so the value is applied to the live settings too. The
    variable is read (not hardcoded) so an operator who opted into update
    checks keeps them, and an unset variable falls back to the same ``"off"``
    default ``configure()`` applies, so calling this helper on its own cannot
    raise ``KeyError`` (issue #291).
    """
    fastmcp_settings = getattr(sys.modules.get("fastmcp"), "settings", None)
    if fastmcp_settings is not None:
        fastmcp_settings.check_for_updates = os.environ.get("FASTMCP_CHECK_FOR_UPDATES", "off")


def configure() -> FastMCP:
    """Perform this process's server side effects explicitly and return the server.

    Importing this module has no side effects (issue #190): it neither builds
    a FastMCP instance nor mutates the environment. Callers that actually
    serve traffic - the ``__main__`` entrypoint and the tests - call this to

    1. disable FastMCP's automatic update check, which delays startup and can
       hang in offline/CI environments, and
    2. build the shared server instance on purpose instead of at import time.

    The server is created on first use and cached, so calling this repeatedly
    is cheap and returns the same instance.

    Returns:
        FastMCP: The shared, configured MCP server instance.
    """
    os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
    _sync_fastmcp_update_check()
    return get_mcp_server()


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


# The only transports the server supports. Anything else is a configuration
# error (issue #294) rather than a silent downgrade to stdio.
_VALID_TRANSPORTS = frozenset({"stdio", "http"})


def _resolve_transport() -> str:
    """Resolve ``MCP_TRANSPORT`` to a supported transport name.

    The value is stripped and lowercased so an env-file trailing space or a
    case variant (``HTTP``) still resolves. A value that does not match the
    recognized set fails loudly instead of silently downgrading to stdio, which
    is the single most common way an operator ends up with a server that speaks
    no HTTP and no warning.

    Returns:
        str: The resolved transport name (``stdio`` or ``http``).

    Raises:
        ConfigurationError: If MCP_TRANSPORT is not a recognized transport.
    """
    raw = os.getenv("MCP_TRANSPORT", "stdio")
    mode = raw.strip().lower()
    if mode not in _VALID_TRANSPORTS:
        raise ConfigurationError(f"Invalid MCP_TRANSPORT: {raw!r}. Expected one of: {sorted(_VALID_TRANSPORTS)}.")
    return mode


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
    transport_mode = _resolve_transport()

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
    configure().run(**get_mcp_run_options())


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
