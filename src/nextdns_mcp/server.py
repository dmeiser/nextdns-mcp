"""NextDNS MCP Server - FastMCP-based implementation built from the NextDNS OpenAPI document.

SPDX-License-Identifier: MIT
"""

import inspect
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, NamedTuple

from fastmcp import FastMCP
from fastmcp.apps.config import AppConfig, ResourceCSP
from fastmcp.server.server import app_config_to_meta_dict
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.mime import UI_MIME_TYPE
from mcp.types import ToolAnnotations
from prefab_ui.renderer import get_renderer_html

from .catalog import (  # noqa: F401
    enabled_generative_ui_tool_names,
    generative_ui_content_tool_names,
    generative_ui_data_tool_names,
    generative_ui_tool_names,
    register_generative_ui_provider,
)
from .client import get_api_client
from .config import (
    configure_logging,
    get_api_base,
    get_http_timeout,
    validate_configuration,
)
from .errors import StateOwnershipError
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
from .typed_error_tool import TypedArgumentTool

logger = logging.getLogger(__name__)


# Operations the server performs itself instead of calling one API endpoint for:
# `dohLookup` resolves a name through a DNS-over-HTTPS resolver, and `plotAnalytics`
# alongside `overviewDashboard` render interactive Prefab UI views from analytics queries.
class _CompositeTool(NamedTuple):
    """One composite operation, and the FastMCP `app` flag it is registered with.

    `app` marks which composites carry an interactive Prefab view beside their
    payload; ``None`` keeps a tool plain request/response. ``build_mcp_server``
    turns the flag into an ``AppConfig`` pointing at the bundled renderer resource
    instead of letting FastMCP synthesize a CDN-backed one. It is named per entry
    rather than left to a positional slot so the difference between the chart and
    the lookup is readable where the catalog is declared.
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

# The composite tools point at this server-owned resource instead of FastMCP's
# synthesized CDN renderer. The bundled document needs no external origin, and
# generated Prefab Python executes in the guarded Deno sandbox, never here.
_COMPOSITE_RENDERER_URI = "ui://nextdns/renderer.html"


def _register_composite_renderer(server: FastMCP) -> None:
    """Publish the self-contained renderer the app-flagged composite tools point at."""

    @server.resource(
        _COMPOSITE_RENDERER_URI,
        name="NextDNS Prefab Renderer",
        mime_type=UI_MIME_TYPE,
        app=AppConfig(
            csp=ResourceCSP(
                resource_domains=[],
                connect_domains=[],
                frame_domains=[],
                base_uri_domains=[],
            )
        ),
    )
    def _bundled_renderer() -> str:
        return get_renderer_html(mode="bundled")


class CompositeTool(TypedArgumentTool):
    """A composite tool whose argument failures keep the typed JSON error contract.

    A ``Literal`` enum or type violation raises from the signature's pydantic
    model before ``_with_error_flag`` can answer, so the caller would get a
    bare message with no structured content and the middleware reporting
    ``ignored_arguments``/``resolved_from`` would never see a result to
    annotate. ``TypedArgumentTool`` converts that refusal into the flagged
    typed payload; the signature's own model still applies its coercions (an
    integer ``profile_id`` becomes a string) and the published schema,
    enums included, is untouched.
    """


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


class McpServerOwner:
    """Owns one lazily built FastMCP server for one explicit lifetime.

    Explicit owner for the server instance that used to live in the
    module-global ``_mcp_server`` singleton. ``configure()`` publishes one
    owner into the calling context, so repeated ``configure()`` calls and the
    ``mcp_server``/``mcp`` compatibility attributes return that same
    instance; embedded servers go through :func:`build_mcp_server` and never
    touch it. Two owners in one process hold two independent servers.
    """

    def __init__(self) -> None:
        self._server: FastMCP | None = None

    def get(self) -> FastMCP:
        """Return this owner's server, building it on first use."""
        if self._server is None:
            self._server = build_mcp_server()
        return self._server


# The owner whose server the current context uses, published by configure()
# (the entrypoint path). No module-level default: an absent owner fails
# loudly instead of silently handing every caller one process-global server.
mcp_server_owner: ContextVar[McpServerOwner | None] = ContextVar("nextdns_mcp_server_owner", default=None)


def current_mcp_server_owner() -> McpServerOwner:
    """Return the owner of the MCP server instance for the current context.

    Raises:
        StateOwnershipError: When no owner is active. The entrypoint path
            publishes one via :func:`configure`; embedded servers build their
            own with :func:`build_mcp_server`.
    """
    owner = mcp_server_owner.get()
    if owner is None:
        raise StateOwnershipError(
            "No MCP server owner is active in this context. The entrypoint publishes one via "
            "server.configure(); embedded servers build their own with build_mcp_server()."
        )
    return owner


@contextmanager
def mcp_server_owned(owner: McpServerOwner) -> Iterator[McpServerOwner]:
    """Publish ``owner`` as the current context's MCP server owner for the block."""
    token = mcp_server_owner.set(owner)
    try:
        yield owner
    finally:
        mcp_server_owner.reset(token)


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
    logger.info(f"Creating HTTP client for {get_api_base()}")
    # One parse of the shipped document feeds all three views of it: the tool
    # surface, the call-log endpoint map, and the published-name list in the
    # initialize instructions (spec_tool_names hits the process-wide route
    # cache). Each used to parse the YAML independently.
    routes = spec_routes()
    server = create_mcp_server(tool_names=published_tool_names(), endpoints=endpoints_for_routes(routes))
    register_spec_tools(server, routes)
    _register_composite_renderer(server)
    for composite in _COMPOSITE_TOOLS:
        # FastMCP publishes only the first text section of a docstring as the tool
        # description, so the accepted values and the worked example further down
        # would never reach a caller — and for these three tools that is exactly the
        # part a caller needs (a chart's interval range, a lookup's record types).
        #
        # Registered as CompositeTool (not through server.tool, which builds a
        # plain FunctionTool) so enum/type violations answer the typed error
        # contract instead of raising out of pydantic argument validation. The
        # app config is folded into meta["ui"] with FastMCP's own helper, so
        # the tools advertise the renderer _register_composite_renderer above
        # publishes, byte-identically to server.tool(app=...).
        server.add_tool(
            CompositeTool.from_function(
                _with_error_flag(composite.tool),
                name=composite.tool.__name__,
                description=inspect.getdoc(composite.tool) or "",
                annotations=_COMPOSITE_ANNOTATIONS,
                meta=(
                    {"ui": app_config_to_meta_dict(AppConfig(resource_uri=_COMPOSITE_RENDERER_URI))}
                    if composite.app
                    else None
                ),
            )
        )
    register_generative_ui_provider(server)
    return server


def get_mcp_server() -> FastMCP:
    """Return the current owner's shared MCP server instance, creating it on first use."""
    return current_mcp_server_owner().get()


def configure() -> FastMCP:
    """Perform this process's server side effects explicitly and return the server.

    Importing this module has no side effects (issue #190): it neither builds
    a FastMCP instance nor mutates the environment. Callers that actually
    serve traffic - the ``__main__`` entrypoint and the tests - call this to

    1. default ``FASTMCP_CHECK_FOR_UPDATES`` to ``"off"`` in this process's
       environment, which disables FastMCP's automatic update check - a call
       that delays startup and can hang in offline/CI environments. FastMCP
       builds its settings object at import time and reads this variable only
       then, so the default must be applied before ``fastmcp`` is first
       imported to take effect: the ``python -m nextdns_mcp.server``
       entrypoint re-executes through the package entrypoint to arrange that,
       and ``configure()`` repeats the default here for direct callers. An
       explicit value the operator set is always preserved, and nothing in
       this function writes to FastMCP's live settings object (issue #643) -
       that hidden second side channel is gone, and
    2. publish this context's owner of the shared server instance, building
       the server on purpose instead of at import time.

    The first ``configure()`` call in a context publishes an owner and later
    calls return that owner's server, so calling this repeatedly is cheap and
    returns the same instance. Embedded servers that need their own instance
    build one explicitly with :func:`build_mcp_server` instead.

    Returns:
        FastMCP: The shared, configured MCP server instance.
    """
    os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
    try:
        owner = current_mcp_server_owner()
    except StateOwnershipError:
        owner = McpServerOwner()
        mcp_server_owner.set(owner)
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
    logger.info(f"  Base URL: {get_api_base()}")
    logger.info(f"  Timeout: {get_http_timeout()}s")
    validate_configuration()
    configure().run(**get_mcp_run_options())


def _reexec_through_package_entrypoint() -> None:  # pragma: no cover
    # Note: excluded from coverage because os.execvpe replaces the running
    # process image, so this call can never return inside the process that is
    # measuring coverage. The decision to re-exec is covered in-process (with
    # this function stubbed) and the re-exec target is covered end-to-end in
    # tests/unit/test_server_dotenv.py.
    os.execvpe(sys.executable, [sys.executable, "-m", "nextdns_mcp", *sys.argv[1:]], os.environ)


def _run_module_entrypoint() -> None:
    """Run the server from the ``python -m nextdns_mcp.server`` entrypoint.

    Importing this module pulls fastmcp in at module import time, so FastMCP's
    settings object is already built before this function runs. When
    ``FASTMCP_CHECK_FOR_UPDATES`` was not exported by the parent shell, a
    value from ``.env`` (or the ``"off"`` default) cannot reach those settings
    in this process, so re-exec through the package entrypoint, which applies
    it before fastmcp is first imported (issue #643). A shell-exported value
    already took effect at import time and runs here in-process.
    """
    from dotenv import load_dotenv

    shell_exported_update_check = "FASTMCP_CHECK_FOR_UPDATES" in os.environ
    load_dotenv()
    if not shell_exported_update_check:
        os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
        _reexec_through_package_entrypoint()
    _run_server()


if __name__ == "__main__":  # pragma: no cover
    # Note: This block is excluded from unit test coverage because:
    # 1. It only executes when running `python -m nextdns_mcp.server` directly
    # 2. When pytest imports the module, __name__ != "__main__"
    # 3. The mcp_server.run() call starts a blocking event loop unsuitable for unit tests
    # The entrypoint logic IS tested via tests/unit/test_server_dotenv.py,
    # which calls _run_module_entrypoint() directly and end-to-end in a
    # subprocess; the options building logic IS tested via
    # tests/unit/test_mcp_run_options.py
    # Load .env only for the real entrypoint (issue #177): calling load_dotenv() at
    # import time made importing server.py from any working directory walk upward and
    # load whatever .env it found into the importing process.
    _run_module_entrypoint()
