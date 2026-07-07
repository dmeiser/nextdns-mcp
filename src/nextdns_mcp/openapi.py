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
"""OpenAPI spec loading, middleware, and MCP server creation.

SPDX-License-Identifier: MIT
"""

import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import mcp.types
import yaml
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.openapi import RouteMap
from fastmcp.server.providers.openapi.routing import DEFAULT_ROUTE_MAPPINGS
from fastmcp.tools import ToolResult

from .config import EXCLUDED_ROUTES, get_default_profile

logger = logging.getLogger(__name__)


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
            except Exception as e:
                # If we can't get the tool schema, proceed with original arguments
                # This should rarely happen, but we don't want to break the flow
                logger.warning(f"Could not filter arguments for tool '{tool_name}': {e}")

        return await call_next(context)


def load_openapi_spec() -> dict[str, Any]:
    """Load the NextDNS OpenAPI specification from YAML file.

    Returns:
        dict: The OpenAPI specification as a dictionary

    Raises:
        FileNotFoundError: If the OpenAPI spec file cannot be found
        yaml.YAMLError: If the YAML file is invalid
    """
    # Load spec from package directory
    spec_path = Path(__file__).parent / "nextdns-openapi.yaml"

    if not spec_path.exists():
        logger.critical(f"OpenAPI spec not found at: {spec_path}")
        logger.critical("The nextdns-openapi.yaml file must be in the package directory.")
        sys.exit(1)

    logger.info(f"Loading OpenAPI spec from: {spec_path}")
    with open(spec_path, "r") as f:
        spec: dict[str, Any] = yaml.safe_load(f)

    return spec


def build_route_mappings() -> list[RouteMap]:
    """Create the RouteMap list used for OpenAPI conversion.

    Combines excluded routes with default route mappings.

    Returns:
        list[RouteMap]: Complete list of route mappings for MCP tool generation
    """
    return [*EXCLUDED_ROUTES, *DEFAULT_ROUTE_MAPPINGS]


def get_openapi_tool_names(spec: dict[str, Any]) -> set[str]:
    """Extract operationIds from an OpenAPI spec to identify auto-generated tools."""
    names: set[str] = set()
    for path_item in spec.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                op_id = operation.get("operationId") if isinstance(operation, dict) else None
                if op_id:
                    names.add(op_id)
    return names


def allow_extra_fields_component_fn(component, *args, **kwargs):
    """
    Patch OpenAPI-imported Pydantic models to allow extra fields (ignore unknown fields).
    Only applies to Pydantic model classes, not enums or primitives.
    Compatible with Pydantic v2 and v1.
    """
    # Only patch Pydantic model classes (skip enums, primitives, etc.)
    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover
        return component  # pragma: no cover
    if isinstance(component, type) and issubclass(component, BaseModel):
        # Pydantic v2
        if hasattr(component, "model_config"):
            component.model_config = {**getattr(component, "model_config", {}), "extra": "ignore"}
        # Pydantic v1 (legacy compatibility)
        elif hasattr(component, "__config__"):  # pragma: no cover

            class Config(getattr(component, "__config__")):  # pragma: no cover
                extra = "ignore"  # pragma: no cover

            component.__config__ = Config  # pragma: no cover
    return component


def create_mcp_server(api_client: httpx.AsyncClient) -> FastMCP:
    """Create and configure the NextDNS MCP server.

    Args:
        api_client: Pre-configured AsyncClient for API calls

    Returns:
        FastMCP: Configured MCP server instance

    Raises:
        FileNotFoundError: If OpenAPI spec cannot be found
        yaml.YAMLError: If OpenAPI spec is invalid
    """
    # Load the OpenAPI specification
    logger.info("Loading NextDNS OpenAPI specification...")
    openapi_spec = load_openapi_spec()

    # Create MCP server from OpenAPI spec
    logger.info("Generating MCP server from OpenAPI specification...")
    route_maps = build_route_mappings()

    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=api_client,
        route_maps=route_maps,
        name="NextDNS MCP Server",
        strict_input_validation=False,
        mcp_component_fn=allow_extra_fields_component_fn,
    )

    # Add middleware to strip unknown fields from tool arguments
    # This allows AI clients (like OpenAI) that send extra fields to work properly
    mcp.add_middleware(StripExtraFieldsMiddleware())

    # Remove the ~80 auto-generated OpenAPI tools. The grouped CRUD tools below
    # replace them, so only the small intentional surface is exposed.
    # FastMCP internally stores registered tools on each provider in a private
    # ``_tools`` dict. We intentionally mutate it here as a version-pinned
    # workaround; the project pins FastMCP so this shape is controlled.
    openapi_tool_names = get_openapi_tool_names(openapi_spec)
    for provider in mcp.providers:
        tool_registry = getattr(provider, "_tools", None)
        if not isinstance(tool_registry, dict):
            logger.warning(
                "Provider %s has no _tools dict; OpenAPI tool cleanup skipped. FastMCP shape may have changed.",
                provider,
            )
            continue
        for tool_name in openapi_tool_names:
            tool_registry.pop(tool_name, None)
            # Tool may have already been excluded by route mappings; ignore silently.

    # Add metadata about the server
    logger.info("MCP server created successfully")
    default_profile = get_default_profile()
    if default_profile:
        logger.info(f"Default profile: {default_profile}")

    return mcp
