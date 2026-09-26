# ---
# Extra Field Relaxation for MCP Tool Arguments
#
# AI clients (like OpenAI) often send extra/unknown fields with tool calls.
# StripExtraFieldsMiddleware intercepts tool calls and filters arguments to
# only include fields defined in the tool's schema, operating at the MCP call
# level so that unknown fields are silently ignored (not rejected) while
# required/typed fields are still validated.
#
# See docs/troubleshooting.md for details.
"""MCP server creation.

The NextDNS MCP server is built directly from the grouped CRUD tools in
``src/nextdns_mcp/tools/``. The historical OpenAPI-spec-based tool generation
(``FastMCP.from_openapi``) was removed: the generated atomic tools were
immediately stripped from the server (see issue #146, dead OpenAPI loading),
and FastMCP 4.x's OpenAPI provider expects an ``httpx2.AsyncClient`` while
this project's ``AccessControlledClient`` subclasses ``httpx.AsyncClient``
(see issue #141), which broke ``uv run mypy src`` and emitted a
``FastMCPDeprecationWarning`` at startup.

SPDX-License-Identifier: MIT
"""

import logging
from typing import Any

import mcp.types
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import get_default_profile

logger = logging.getLogger(__name__)


class OpenApiSpecNotFound(FileNotFoundError):
    """Raised when the NextDNS OpenAPI spec file cannot be found."""


class StripExtraFieldsMiddleware(Middleware):
    """Middleware that strips unknown fields and coerces types in tool arguments.

    AI clients (like OpenAI) often send extra/unknown fields with tool calls
    that don't match the tool's input schema. Docker MCP CLI also passes values
    as strings (e.g., "true" instead of true). This middleware:
    1. Filters arguments to only include fields defined in the tool's parameter schema
    2. Coerces string values to proper types (booleans, integers, floats)

    Example:
        A tool with parameters {"domain": str, "enabled": bool} receiving
        {"domain": "example.com", "enabled": "true", "extra_field": "ignored"}
        will have arguments filtered and coerced to {"domain": "example.com", "enabled": true}
    """

    def _get_schema_property_types(self, prop_schema: dict[str, Any]) -> set[str]:
        """Extract JSON Schema type(s) from a property schema.

        Handles ``type`` (string or list) and ``anyOf``/``oneOf`` subschemas.
        """
        types: set[str] = set()
        if not isinstance(prop_schema, dict):
            return types

        type_value = prop_schema.get("type")
        if isinstance(type_value, str):
            types.add(type_value)
        elif isinstance(type_value, list):
            types.update(type_value)

        for sub_key in ("anyOf", "oneOf"):
            for subschema in prop_schema.get(sub_key, []):
                if isinstance(subschema, dict):
                    sub_type = subschema.get("type")
                    if isinstance(sub_type, str):
                        types.add(sub_type)
                    elif isinstance(sub_type, list):
                        types.update(sub_type)

        return types

    def _coerce_string_value(self, s: str, schema_types: set[str]) -> Any:
        """Coerce a string based on the expected JSON Schema types.

        Only coerces to bool, int, or float when the schema explicitly expects
        that type. Identifier-like strings (e.g. profile IDs, entry IDs) declared
        as ``string`` are left untouched.
        """
        sl = s.lower()
        if "boolean" in schema_types and sl in ("true", "false"):
            return sl == "true"
        if "integer" in schema_types and (s.isdigit() or (s.startswith("-") and s[1:].isdigit())):
            return int(s)
        if "number" in schema_types and s.replace(".", "", 1).replace("-", "", 1).isdigit():
            try:
                return float(s)
            except ValueError:
                return s
        return s

    def _coerce_value(self, value: Any, prop_schema: dict[str, Any] | None = None) -> Any:
        """Coerce a value using its property schema.

        Top-level values are coerced according to the declared schema. Nested
        dict/list values are recursed without schema context to avoid coercing
        identifier-like strings inside opaque objects.
        """
        if prop_schema is None:
            prop_schema = {}
        if isinstance(value, str):
            schema_types = self._get_schema_property_types(prop_schema)
            return self._coerce_string_value(value, schema_types)
        if isinstance(value, list):
            items_schema = prop_schema.get("items") if isinstance(prop_schema, dict) else None
            return [self._coerce_value(item, items_schema) for item in value]
        if isinstance(value, dict):
            return {k: self._coerce_value(v, None) for k, v in value.items()}
        return value

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next: CallNext[mcp.types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Filter tool arguments to only include known parameters and coerce types."""
        tool_name = context.message.name
        arguments = context.message.arguments

        if arguments and context.fastmcp_context:
            try:
                # Get the tool's parameter schema
                fastmcp_server = context.fastmcp_context.fastmcp
                tool = await fastmcp_server.get_tool(tool_name)
                if tool is None:
                    # Tool not found, pass through
                    return await call_next(context)
                known_params = set(tool.parameters.get("properties", {}).keys())

                # Filter arguments to only include known parameters
                original_keys = set(arguments.keys())
                filtered_args = {k: v for k, v in arguments.items() if k in known_params}

                # Log if any fields were stripped (for debugging)
                stripped_keys = original_keys - known_params
                if stripped_keys:
                    logger.debug(f"Tool '{tool_name}': Stripped unknown fields: {stripped_keys}")

                # Coerce string values to proper types based on the parameter schema
                properties = tool.parameters.get("properties", {})
                coerced_args = {k: self._coerce_value(v, properties.get(k)) for k, v in filtered_args.items()}
                if coerced_args != filtered_args:
                    logger.debug(f"Tool '{tool_name}': Coerced types in arguments")

                # Update the arguments in place
                context.message.arguments = coerced_args
            except Exception as e:  # noqa: BLE001
                # If we can't get the tool schema, proceed with original arguments
                # This should rarely happen, but we don't want to break the flow
                logger.warning(f"Could not filter arguments for tool '{tool_name}': {e}")

        return await call_next(context)


def create_mcp_server(client: Any = None) -> FastMCP:
    """Create and configure the NextDNS MCP server.

    The server exposes only the grouped CRUD tools registered by
    ``src/nextdns_mcp/server.py`` plus the usage-guide prompt. No tools are
    generated from the OpenAPI spec anymore (see module docstring and issue
    #141/#146 for the rationale).

    Returns:
        FastMCP: Configured MCP server instance
    """
    logger.info("Creating NextDNS MCP server...")
    mcp = FastMCP(name="NextDNS MCP Server")

    # Add middleware to strip unknown fields from tool arguments
    # This allows AI clients (like OpenAI) that send extra fields to work properly
    mcp.add_middleware(StripExtraFieldsMiddleware())

    logger.info("MCP server created successfully")
    default_profile = get_default_profile()
    if default_profile:
        logger.info(f"Default profile: {default_profile}")

    _register_health_endpoint(mcp)

    return mcp


def _register_health_endpoint(mcp: FastMCP) -> None:
    """Register the minimal ``/health`` endpoint on the FastMCP HTTP app.

    AGENT.md mandates a ``GET /health`` that returns ``200 OK`` with the JSON body
    ``{"status": "ok"}``. It is registered with FastMCP's ``custom_route`` so it is
    served alongside the ``/mcp`` streamable-HTTP endpoint when the server runs in
    HTTP transport mode; in stdio mode there is no HTTP surface, so the route is
    simply not reachable. The handler is intentionally a constant response: it
    confirms the HTTP server is up without touching the NextDNS API.

    Args:
        mcp: The FastMCP server to attach the route to.
    """

    async def health_check(_request: Request) -> JSONResponse:
        """Return a constant ``{"status": "ok"}`` health payload."""
        return JSONResponse({"status": "ok"})

    mcp.custom_route("/health", methods=["GET"])(health_check)
