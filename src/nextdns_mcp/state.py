"""Shared resolution for explicitly owned server state (issue #642).

The server's mutable state - the API client, the DoH client, and the download
registry - belongs to a per-server ``ServerResources`` owner (see
``nextdns_mcp.openapi``). A state holder is reachable from a request in two
ways, and both are explicit:

- a caller published an owner into the current context with the owning
  module's ``*_owned()`` context manager (standalone scripts and tests), or
- FastMCP is serving an MCP request: the active tool ``Context`` reads the
  lifespan of the server that owns the tool, which stays the child's own
  lifespan when the server is mounted under a parent. Outside an active
  ``Context``, every request adapter also binds a transport-level context
  (``fastmcp_request_ctx``) in the request's own task. Both carry a lifespan
  context, and the lifespan yields the server's ``ServerResources`` under
  :data:`RESOURCES_LIFESPAN_KEY`.

There is deliberately no process-global fallback. The lifespan's own task does
not propagate context variables to request tasks, so a module-level context
var alone cannot carry per-server state into served traffic - the lifespan
context is the channel FastMCP itself provides for exactly this.

SPDX-License-Identifier: MIT
"""

from contextvars import ContextVar
from typing import Any

from fastmcp.server.context import Context
from fastmcp.server.dependencies import fastmcp_request_ctx, get_context

from .config import ServerConfig

# Key under which the server lifespan yields its ServerResources in the
# lifespan context FastMCP hands to every served request.
RESOURCES_LIFESPAN_KEY = "nextdns_resources"

# The snapshot a server was created with, published by its lifespan for
# same-task callers. Served requests reach the same object through the
# lifespan context below; no process-global default exists.
server_config: ContextVar[ServerConfig | None] = ContextVar("nextdns_server_config", default=None)


def current_server_config() -> ServerConfig | None:
    """The config of the server serving this call, or ``None`` outside a server.

    Resolution order: the context published by a server lifespan, then the
    resources of the FastMCP server serving the current request. Standalone
    callers get ``None`` and keep using the process-environment getters.
    """
    config = server_config.get()
    if config is not None:
        return config
    resources = request_resources()
    if resources is not None:
        return resources.config
    return None


def _resources_in(lifespan_context: Any) -> Any | None:
    """The resources a lifespan context yields, if it yields ours."""
    if isinstance(lifespan_context, dict):
        return lifespan_context.get(RESOURCES_LIFESPAN_KEY)
    return None


def request_resources() -> Any | None:
    """The ``ServerResources`` of the FastMCP server serving this request.

    The active tool ``Context`` is authoritative: its lifespan context is the
    owning server's own result even when that server is mounted, where the
    transport request context belongs to the parent instead. Callers outside
    an active ``Context`` fall back to the transport request context.
    """
    context: Context | None = None
    try:
        context = get_context()
    except RuntimeError:
        pass
    if context is not None:
        resources = _resources_in(context.lifespan_context)
        if resources is not None:
            return resources

    request_ctx = fastmcp_request_ctx.get()
    if request_ctx is None:
        return None
    return _resources_in(request_ctx.lifespan_context)
