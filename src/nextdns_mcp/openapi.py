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
import time
from typing import Any, NamedTuple

import httpx
import mcp.types
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

from .client import get_api_client
from .coercion import _is_integer
from .config import ConfigurationError, get_api_key, get_default_profile

logger = logging.getLogger(__name__)

# --- /health readiness probe tuning -----------------------------------------

HEALTH_PROBE_PATH = "/profiles"
"""Cheapest authenticated read on the NextDNS API.

``GET /profiles`` returns the caller's profile list, so a 2xx proves the API
key is accepted without touching (or mutating) any specific profile."""

HEALTH_PROBE_TIMEOUT = 5.0
"""Seconds allowed for the readiness probe before NextDNS is called unreachable."""

HEALTH_CACHE_TTL = 30.0
"""Seconds a probe result is reused, so health polling cannot hammer the API."""

HEALTH_FAILURE_AUTH = "auth"
"""Failure class: NextDNS rejected the configured credentials."""

HEALTH_FAILURE_UNREACHABLE = "unreachable"
"""Failure class: NextDNS could not be probed, or answered unusably."""


class HealthFailure(NamedTuple):
    """A failed readiness probe.

    Attributes:
        failure_class: ``"auth"`` or ``"unreachable"``.
        reason: Short operator-facing explanation. Built only from the HTTP
            status code and the exception type name, so it can never carry API
            key material, auth headers, or the upstream response body.
    """

    failure_class: str
    reason: str


# Cached probe result as (monotonic timestamp, failure or None when healthy).
_health_probe_cache: tuple[float, HealthFailure | None] | None = None


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
        if "integer" in schema_types and _is_integer(s):
            try:
                return int(s)
            except ValueError:
                return s
        if "number" in schema_types and s.replace(".", "", 1).replace("-", "", 1).isdecimal():
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

    async def _prepare_arguments(
        self,
        fastmcp_server: FastMCP,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch the tool schema, strip unknown fields, and coerce argument types.

        Fails closed: if the tool's parameter schema cannot be fetched, the call
        is aborted with a ToolError instead of passing arguments through
        unstripped/un-coerced, so callers see a clear error rather than the
        downstream validation failures the middleware exists to prevent.
        """
        try:
            tool = await fastmcp_server.get_tool(tool_name)
            if tool is None:
                # Tool not found, pass arguments through untouched
                return arguments

            parameters = tool.parameters
            known_params = set(parameters.get("properties", {}).keys())

            # Filter arguments to only include known parameters
            original_keys = set(arguments.keys())
            filtered_args = {k: v for k, v in arguments.items() if k in known_params}

            # Log if any fields were stripped (for debugging)
            stripped_keys = original_keys - known_params
            if stripped_keys:
                logger.debug(f"Tool '{tool_name}': Stripped unknown fields: {stripped_keys}")

            # Coerce string values to proper types based on the parameter schema
            properties = parameters.get("properties", {})
            coerced_args = {k: self._coerce_value(v, properties.get(k)) for k, v in filtered_args.items()}
            if coerced_args != filtered_args:
                logger.debug(f"Tool '{tool_name}': Coerced types in arguments")

            return coerced_args
        except NotFoundError:
            raise
        except Exception as e:
            # Schema fetch/parse failure: fail closed so callers get a clear error
            # instead of downstream validation failures from unstripped args.
            logger.exception(f"Failed to prepare arguments for tool '{tool_name}'; aborting call")
            raise ToolError(
                f"Tool call for '{tool_name}' failed closed: could not fetch its parameter "
                f"schema to validate arguments (root cause: {e})"
            ) from e

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next: CallNext[mcp.types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Filter tool arguments to only include known parameters and coerce types.

        Fails closed: if the tool's parameter schema cannot be fetched, the call
        is aborted with a ToolError instead of passing arguments through
        unstripped/un-coerced.
        """
        arguments = context.message.arguments
        if arguments and context.fastmcp_context:
            context.message.arguments = await self._prepare_arguments(
                context.fastmcp_context.fastmcp, context.message.name, arguments
            )

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
    """Register the ``/health`` readiness endpoint on the FastMCP HTTP app.

    ``GET /health`` is a real readiness check, not a liveness constant: it
    probes the NextDNS API with the configured API key and returns ``200 OK``
    with ``{"status": "ok"}`` only when that probe succeeds. A failing probe
    returns ``503`` together with the failure class (``auth`` or
    ``unreachable``) and a short reason. Results are cached for
    ``HEALTH_CACHE_TTL`` seconds so a polling load balancer cannot hammer the
    API. The route is registered with FastMCP's ``custom_route`` so it is served
    alongside the ``/mcp`` streamable-HTTP endpoint; in stdio mode there is no
    HTTP surface, so the route is simply not reachable.

    Args:
        mcp: The FastMCP server to attach the route to.
    """

    async def health_check(_request: Request) -> JSONResponse:
        """Report NextDNS API readiness as measured by a live credential probe."""
        failure = await check_nextdns_readiness()
        if failure is None:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {"status": "error", "class": failure.failure_class, "reason": failure.reason},
            status_code=503,
        )

    mcp.custom_route("/health", methods=["GET"])(health_check)


async def check_nextdns_readiness() -> HealthFailure | None:
    """Return the readiness result, probing NextDNS at most once per cache window.

    Returns:
        None when the configured credentials work against NextDNS, otherwise a
        HealthFailure describing why the service is not ready.
    """
    global _health_probe_cache

    now = time.monotonic()
    cached = _health_probe_cache
    if cached is not None and now - cached[0] < HEALTH_CACHE_TTL:
        logger.debug(f"Reusing cached /health readiness result (age {now - cached[0]:.1f}s)")
        return cached[1]

    failure = await _run_health_probe()
    _health_probe_cache = (time.monotonic(), failure)
    return failure


async def _run_health_probe() -> HealthFailure | None:
    """Probe the NextDNS API once with the configured credentials.

    The request goes through the client's RAW transport (``raw_request``), which
    bypasses profile access control: a local ACL denial must not mask the true
    upstream authentication state. Only the status code and the exception type
    name are reported, never the API key, the auth headers, or the response
    body.

    Returns:
        None when NextDNS accepted the credentials, otherwise a HealthFailure.
    """
    try:
        client = get_api_client()
        response = await client.raw_request("GET", HEALTH_PROBE_PATH, timeout=HEALTH_PROBE_TIMEOUT)
    except ConfigurationError:
        if not get_api_key():
            return HealthFailure(HEALTH_FAILURE_AUTH, "No usable NextDNS API key is configured")
        return HealthFailure(
            HEALTH_FAILURE_UNREACHABLE,
            "NextDNS client configuration is invalid (check NEXTDNS_HTTP_TIMEOUT)",
        )
    except httpx.TimeoutException:
        return HealthFailure(
            HEALTH_FAILURE_UNREACHABLE,
            f"NextDNS API did not respond within {HEALTH_PROBE_TIMEOUT:g}s",
        )
    except httpx.HTTPError as exc:
        return HealthFailure(
            HEALTH_FAILURE_UNREACHABLE,
            f"Could not reach the NextDNS API ({type(exc).__name__})",
        )

    if response.is_success:
        return None
    if response.status_code in (401, 403):
        return HealthFailure(
            HEALTH_FAILURE_AUTH,
            f"NextDNS API rejected the configured credentials (HTTP {response.status_code})",
        )
    return HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        f"NextDNS API answered with an unusable status (HTTP {response.status_code})",
    )
