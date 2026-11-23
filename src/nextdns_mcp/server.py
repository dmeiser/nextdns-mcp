"""NextDNS MCP Server - FastMCP-based implementation built from the NextDNS OpenAPI document.

SPDX-License-Identifier: MIT
"""

import inspect
import logging
import os
import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, NamedTuple

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import ToolAnnotations

from .catalog import (  # noqa: F401
    enabled_generative_ui_tool_names,
    generative_ui_content_tool_names,
    generative_ui_data_tool_names,
    generative_ui_tool_names,
    register_generative_ui_provider,
)
from .client import get_api_client
from .config import (
    NEXTDNS_BASE_URL,
    configure_logging,
    get_http_timeout,
    validate_configuration,
)
from .openapi import create_mcp_server
from .run_options import (  # noqa: F401
    _is_loopback_host,
    _parse_port,
    _resolve_transport,
    get_mcp_run_options,
)
from .spec_tools import (
    endpoints_for_routes,
    register_spec_tools,
    spec_routes,
    spec_tool_names,
)
from .tools.dashboard import overviewDashboard
from .tools.doh import dohLookup
from .tools.plots import plotAnalytics
from .transport import is_error_payload

logger = logging.getLogger(__name__)


# Operations the server performs itself instead of calling one API endpoint for:
# `dohLookup` resolves a name through a DNS-over-HTTPS resolver, and `plotAnalytics`
# alongside `overviewDashboard` render interactive Prefab UI views from analytics queries.
class _CompositeTool(NamedTuple):
    """One composite operation, and the FastMCP `app` flag it is registered with.

    `app` is handed straight to ``FastMCP.tool(app=...)``: ``True`` publishes the tool
    as an interactive app, so its Prefab UI view reaches the caller beside the payload;
    ``None`` keeps it a plain request/response tool. It is named per entry rather than
    left to a positional slot so the difference between the chart and the lookup is
    readable where the catalog is declared.
    """

    tool: Callable[..., Any]
    app: bool | None


_COMPOSITE_TOOLS = (
    _CompositeTool(plotAnalytics, app=True),
    _CompositeTool(dohLookup, app=None),
    _CompositeTool(overviewDashboard, app=True),
)

# None of the composite tools mutate anything: dohLookup answers a DNS question and
# plotAnalytics renders a chart from analytics already collected. The generated
# SpecApiTool derives these hints from the endpoint method; the composites have
# no endpoint, so the hints are stated here — without them a caller scanning for
# destructive tools gets no signal either way.
_COMPOSITE_ANNOTATIONS = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


def _with_error_flag(tool_fn: Any) -> Any:
    """Wrap a composite tool so its typed error payloads surface as ``isError``.

    The composites answer failures as typed payload dicts, exactly like the
    generated tools - but a bare dict return is wrapped by FastMCP as a
    SUCCESSFUL result, so every one of those failure payloads used to reach
    callers with ``isError=False``. The generated SpecApiTool sets the flag
    explicitly via ``transport.is_error_payload``; this wrapper applies the same rule
    to the three composite tools (plotAnalytics, dohLookup, overviewDashboard), so a refusal or failure never masquerades
    as a normal answer.
    """

    @wraps(tool_fn)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        payload = await tool_fn(*args, **kwargs)
        if is_error_payload(payload):
            return ToolResult(structured_content=payload, is_error=True)
        return payload

    return _wrapped


_mcp_server: FastMCP | None = None


def published_tool_names() -> list[str]:
    """Every tool name this server publishes, before any of them is registered.

    The ``initialize`` instructions describe the catalog, and they are written when
    the server is constructed — before the tools exist — so the names have to be
    derivable without registering anything.
    """
    names = [
        *spec_tool_names(),
        *(composite.tool.__name__ for composite in _COMPOSITE_TOOLS),
        *enabled_generative_ui_tool_names(),
    ]
    return names


def build_mcp_server() -> FastMCP:
    """Create the MCP server: one tool per NextDNS API operation, plus the three composite tools (plotAnalytics, dohLookup, overviewDashboard).

    The catalog is generated from the shipped OpenAPI document by ``spec_tools`` — a
    tool per operation, named by its ``operationId``, with the spec's own description
    and parameter schemas. Registered on top of it are the three operations this server
    performs itself (``plotAnalytics``, ``dohLookup``, ``overviewDashboard``), which the document cannot
    describe because there is no endpoint to describe.

    Returns:
        FastMCP: Configured MCP server instance with all tools registered
    """
    logger.info(f"Creating HTTP client for {NEXTDNS_BASE_URL}")
    # One parse of the shipped document feeds all three views of it: the tool
    # surface, the call-log endpoint map, and the published-name list in the
    # initialize instructions (spec_tool_names hits the process-wide route
    # cache). Each used to parse the YAML independently.
    routes = spec_routes()
    server = create_mcp_server(tool_names=published_tool_names(), endpoints=endpoints_for_routes(routes))
    register_spec_tools(server, routes)
    for composite in _COMPOSITE_TOOLS:
        # FastMCP publishes only the first text section of a docstring as the tool
        # description, so the accepted values and the worked example further down
        # would never reach a caller — and for these three tools that is exactly the
        # part a caller needs (a chart's interval range, a lookup's record types).
        server.tool(
            description=inspect.getdoc(composite.tool) or "",
            annotations=_COMPOSITE_ANNOTATIONS,
            app=composite.app,
        )(_with_error_flag(composite.tool))
    register_generative_ui_provider(server)
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
