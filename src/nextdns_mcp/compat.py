"""OpenWebUI compatibility routes served beside the MCP endpoint.

These routes exist for compatibility with third-party clients (e.g. OpenWebUI's
automatic model-discovery probe) rather than for the MCP protocol itself. They
are registered separately from the health endpoint so health and compat
concerns stay independent and independently testable.

SPDX-License-Identifier: MIT
"""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse


def register_compat_routes(mcp: FastMCP) -> None:
    """Register the OpenWebUI compatibility endpoints on the FastMCP HTTP app.

    Args:
        mcp: The FastMCP server to attach the routes to.
    """

    async def v1_models(_request: Request) -> JSONResponse:
        """Answer OpenWebUI's automatic model-discovery probe to prevent noisy 404 logs."""
        return JSONResponse({"object": "list", "data": []})

    mcp.custom_route("/v1/models", methods=["GET"])(v1_models)
