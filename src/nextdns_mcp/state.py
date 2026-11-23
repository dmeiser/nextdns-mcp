"""Shared resolution for explicitly owned server state (issue #642).

The server's mutable state - the API client, the DoH client, and the download
registry - belongs to a per-server ``ServerResources`` owner (see
``nextdns_mcp.openapi``). A state holder is reachable from a request in two
ways, and both are explicit:

- a caller published an owner into the current context with the owning
  module's ``*_owned()`` context manager (standalone scripts and tests), or
- FastMCP is serving an MCP request: every request adapter binds a per-request
  context (``fastmcp_request_ctx``) in the request's own task, carrying the
  lifespan context the server lifespan yielded - and the lifespan yields the
  server's ``ServerResources`` under :data:`RESOURCES_LIFESPAN_KEY`.

There is deliberately no process-global fallback. The lifespan's own task does
not propagate context variables to request tasks, so a module-level context
var alone cannot carry per-server state into served traffic - the lifespan
context is the channel FastMCP itself provides for exactly this.

SPDX-License-Identifier: MIT
"""

from typing import Any

from fastmcp.server.dependencies import fastmcp_request_ctx

# Key under which the server lifespan yields its ServerResources in the
# lifespan context FastMCP hands to every served request.
RESOURCES_LIFESPAN_KEY = "nextdns_resources"


def request_resources() -> Any | None:
    """The ``ServerResources`` of the FastMCP server serving this request.

    Returns:
        The resources the current FastMCP request's server lifespan owns, or
        ``None`` when no FastMCP request is being served in this context.
    """
    request_ctx = fastmcp_request_ctx.get()
    if request_ctx is None:
        return None
    lifespan_context = request_ctx.lifespan_context
    if isinstance(lifespan_context, dict):
        return lifespan_context.get(RESOURCES_LIFESPAN_KEY)
    return None
