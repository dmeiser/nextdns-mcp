"""MCP server creation.

This module assembles the server and nothing else: the ``FastMCP`` instance,
the middleware chain whose order is load-bearing (see "Middleware order
invariants" in AGENT.md), the per-server ``ServerResources``, the lifespan,
and the HTTP routes. Every one of those implementations lives in its own
module and is wired in here:

- ``relaxation`` — ``StripExtraFieldsMiddleware``: drops top-level arguments the
  target tool's schema does not declare and coerces obvious string spellings of
  typed fields (see docs/troubleshooting.md for the relaxation contract).
- ``health`` — the ``/health`` readiness endpoint and its ``ReadinessProber``.
- ``compat`` — the OpenWebUI compatibility routes served beside ``/mcp``.
- ``lifespan`` — ``server_lifespan`` and ``ServerResources``, the explicitly
  owned per-server state released on shutdown.

The tool catalog is generated from the shipped NextDNS OpenAPI document
(``spec_tools``): one tool per operation, named by its ``operationId``. FastMCP's
own OpenAPI *transport* is deliberately not used — it sends through an
``httpx2.AsyncClient`` of its own, which would bypass this project's
``AccessControlledClient`` (profile allowlists, read-only mode, the
protected-profile denylist) and raise away the upstream body that the typed error
payloads are built from. What is used is FastMCP's OpenAPI *parser*, which turns
the document into the names, descriptions and parameter schemas; the request then
goes through this project's own client.

That distinction is the whole answer to the two historical objections to
spec-based generation here (issues #141 and #146): the earlier attempt used
``FastMCP.from_openapi``, whose generated tools were both stripped from the server
and incompatible with ``httpx.AsyncClient``, so the server went back to hand-written
tools. Parsing the document and owning the transport keeps the single source of truth
without either problem.

SPDX-License-Identifier: MIT
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp import FastMCP

from .call_log import CallLogMiddleware, call_log_path
from .compat import register_compat_routes
from .config import ServerConfig
from .health import register_health_route
from .lifespan import ServerResources, server_lifespan
from .relaxation import StripExtraFieldsMiddleware
from .spec_tools import endpoints_for_routes
from .tool_search import CallToolShapeMiddleware, build_search_transform
from .usage import server_instructions

logger = logging.getLogger(__name__)


def create_mcp_server(
    tool_names: Sequence[str] = (),
    endpoints: Mapping[str, tuple[str, str]] | None = None,
    config: ServerConfig | None = None,
) -> FastMCP:
    """Create and configure the NextDNS MCP server.

    The tools themselves are registered by ``src/nextdns_mcp/server.py`` — the
    generated ones and the three composite operations. What happens here is the machinery
    around them: disclosure, argument relaxation, the call log, the health
    endpoint, and ownership of the server's long-lived state — one
    ``ServerResources`` (the API client, DoH client, and download registry)
    is built here and attached to the instance, so every server owns its state
    explicitly and embedded servers never share it.

        Args:
        tool_names: The names of the tools registered after this call, so the
            initialize instructions can describe a small catalog directly.
        endpoints: ``tool name -> (method, path template)`` for the call log,
            built once from the parsed routes by the server, so the log and the
            tool surface are two views of one parse. ``None`` falls back to the
            shipped document's own parse; a caller registering tools from a
            modified document must pass the parse it registered from.
        config: The immutable configuration snapshot this server owns. A
            caller building a full server may take one snapshot and pass it
            here so the catalog and the resources agree; ``None`` snapshots
            the environment at creation.

    Returns:
        FastMCP: Configured MCP server instance
    """
    logger.info("Creating NextDNS MCP server...")
    snapshot = config if config is not None else ServerConfig.from_env()
    mcp, endpoints = _build_fastmcp_instance(tool_names, endpoints, snapshot)

    # Add middleware to strip unknown fields from tool arguments
    # This allows AI clients (like OpenAI) that send extra fields to work properly
    mcp.add_middleware(StripExtraFieldsMiddleware(tool_names=tool_names))

    _add_call_log_middleware(mcp, endpoints)

    logger.info("MCP server created successfully")
    mcp._nextdns_resources = ServerResources(config=snapshot)  # type: ignore[attr-defined]
    default_profile = snapshot.default_profile
    if default_profile:
        logger.info(f"Default profile: {default_profile}")

    register_health_route(mcp)
    register_compat_routes(mcp)

    return mcp


def _build_fastmcp_instance(
    tool_names: Sequence[str],
    endpoints: Mapping[str, tuple[str, str]] | None,
    config: ServerConfig,
) -> tuple[FastMCP, Mapping[str, tuple[str, str]]]:
    """Build the ``FastMCP`` instance with progressive disclosure wired.

    Constructing the instance and the shape middleware stay one step because
    the middleware must exist on the instance before any tool call is served.
    The resolved endpoint map is returned beside the instance so the caller can
    add the strip and call-log middlewares between/after the shape middleware
    without re-deriving the map (or deriving one that disagrees with it).
    """
    if endpoints is None:
        endpoints = endpoints_for_routes()

    transforms: list[Any] = []
    tool_search_mode = config.tool_search_mode
    search_transform = build_search_transform(tool_search_mode, endpoints=endpoints)
    if search_transform is not None:
        transforms.append(search_transform)
        logger.info(
            "Tool search enabled (%s): tools/list exposes search_tools + call_tool instead of the full catalog",
            tool_search_mode,
        )
    mcp = FastMCP(
        name="NextDNS MCP Server",
        instructions=server_instructions(tool_search_mode, tool_names),
        lifespan=server_lifespan,
        transforms=transforms,
    )

    # Under disclosure the caller reaches tools only through call_tool, and
    # live runs show three ways it gets that wrapper wrong: the whole search
    # answer where a name belongs (`name: result.result`), a name typed from
    # memory, and the tool's parameters sent beside `name` instead of inside
    # `arguments`. Each is refused by call_tool's own schema or stripped by the
    # filter below, so the step ends with no call at all. Added first, so it
    # runs before that filter (which would drop the misplaced parameters) and
    # before the call log (which then records the call that actually happened).
    if search_transform is not None:
        mcp.add_middleware(CallToolShapeMiddleware(transform=search_transform, endpoints=endpoints))

    return mcp, endpoints


def _add_call_log_middleware(mcp: FastMCP, endpoints: Mapping[str, tuple[str, str]]) -> None:
    """Attach the call log when a path is configured (see ``nextdns_mcp.call_log``).

    Record what this server actually executed. A read leaves no trace in the
    API, so a harness that verifies over REST cannot tell "the model read it"
    from "the model did nothing and the state was already right". The server
    can say, and it does so here. The path is read from the environment once
    and passed in, so the middleware cannot disagree with the "enabled" line
    logged beside it, and the endpoint map arrives from the server's single
    route parse rather than a registry this module would have to import and
    populate.
    """
    log_path = call_log_path()
    if log_path:
        mcp.add_middleware(CallLogMiddleware(path=log_path, endpoints=endpoints))
        logger.info("Call log enabled: %s", log_path)
