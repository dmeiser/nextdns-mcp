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
# ruff: noqa: E402
"""NextDNS MCP Server - FastMCP-based implementation using OpenAPI spec.

SPDX-License-Identifier: MIT
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union

from dotenv import load_dotenv

# Load environment variables from .env file first
load_dotenv()

# Disable FastMCP automatic update checks to prevent startup delays and hangs in offline/CI environments
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

import httpx

# Use a non-interactive matplotlib backend so plotting works in headless/CI environments
import matplotlib
import mcp.types
import yaml
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.openapi import RouteMap
from fastmcp.server.providers.openapi.routing import DEFAULT_ROUTE_MAPPINGS
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

# Pydantic import for allow_extra_fields_component_fn and BeforeValidator
try:
    from pydantic import BaseModel, BeforeValidator
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore
    BeforeValidator = None  # type: ignore

from .config import (
    DNS_STATUS_CODES,
    EXCLUDED_ROUTES,
    NEXTDNS_BASE_URL,
    VALID_DNS_RECORD_TYPES,
    can_read_profile,
    can_write_profile,
    get_api_key,
    get_default_profile,
    get_http_timeout,
    is_read_only,
)

logger = logging.getLogger(__name__)


# Profile IDs from docker MCP CLI may arrive as integers when the 6-char hex ID
# happens to contain only decimal digits (e.g., "315244"). Use BeforeValidator
# to coerce int inputs to str while preserving None for the default-profile fallback.
def _coerce_profile_id(v: object) -> object:
    """Coerce non-None profile_id values to str; leave None as-is."""
    return str(v) if v is not None else v


_coerce_to_str = BeforeValidator(_coerce_profile_id) if BeforeValidator is not None else lambda x: x
OptionalProfileId = Annotated[Optional[str], _coerce_to_str]

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
ProfileOperation = Literal["list", "create", "get", "update", "delete"]
SettingsCategory = Literal["general", "privacy", "security", "parental", "performance", "logs", "blockpage"]
ListType = Literal[
    "allowlist",
    "denylist",
    "privacy_blocklists",
    "privacy_natives",
    "security_tlds",
    "parental_categories",
    "parental_services",
]
ListOperation = Literal["get", "add", "replace", "update", "remove"]
RewriteOperation = Literal["list", "add", "delete"]
LogOperation = Literal["get", "clear", "download"]
AnalyticsMetric = Literal[
    "status",
    "domains",
    "queryTypes",
    "reasons",
    "ips",
    "dnssec",
    "encryption",
    "ipVersions",
    "protocols",
    "devices",
    "destinations",
]
PlotMetric = Literal[
    "status",
    "devices",
    "protocols",
    "queryTypes",
    "ipVersions",
    "dnssec",
    "encryption",
    "reasons",
    "ips",
]

# List types that support per-entry PATCH updates.
_LIST_UPDATEABLE_TYPES: set[ListType] = {
    "allowlist",
    "denylist",
    "parental_categories",
    "parental_services",
}

# Path-segment mappings for grouped CRUD tools.
_SETTINGS_PATHS: dict[SettingsCategory, str] = {
    "general": "settings",
    "privacy": "privacy",
    "security": "security",
    "parental": "parentalControl",
    "performance": "settings/performance",
    "logs": "settings/logs",
    "blockpage": "settings/blockPage",
}

_LIST_PATHS: dict[ListType, str] = {
    "allowlist": "allowlist",
    "denylist": "denylist",
    "privacy_blocklists": "privacy/blocklists",
    "privacy_natives": "privacy/natives",
    "security_tlds": "security/tlds",
    "parental_categories": "parentalControl/categories",
    "parental_services": "parentalControl/services",
}


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

    def _coerce_string_value(self, s: str) -> Any:
        """Coerce a string to bool, int, float, or return original string.

        Separated into its own helper to reduce method complexity for radon.
        """
        sl = s.lower()
        if sl == "true":
            return True
        if sl == "false":
            return False
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        if s.replace(".", "", 1).replace("-", "", 1).isdigit():
            try:
                return float(s)
            except ValueError:
                return s
        return s

    def _coerce_value(self, value: Any) -> Any:
        """Coerce a value (recursing into dicts/lists) or delegate string coercion."""
        if isinstance(value, str):
            return self._coerce_string_value(value)
        if isinstance(value, dict):
            return {k: self._coerce_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._coerce_value(item) for item in value]
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

                # Coerce string values to proper types (bool, int, float)
                coerced_args = {k: self._coerce_value(v) for k, v in filtered_args.items()}
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


def extract_profile_id_from_url(url: str) -> Optional[str]:
    """Extract profile_id from a URL path.

    Args:
        url: The URL path (e.g., "/profiles/abc123/settings")

    Returns:
        The profile_id if found, None otherwise
    """
    # Match /profiles/{profile_id}/... pattern
    match = re.match(r"^/?profiles/([^/]+)(?:/|$)", url)
    if match:
        return match.group(1)
    return None


def is_write_operation(method: str) -> bool:
    """Check if an HTTP method is a write operation.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE)

    Returns:
        True if it's a write operation, False otherwise
    """
    return method.upper() in ("POST", "PUT", "PATCH", "DELETE")


def _coerce_string_to_bool(value: str) -> bool | None:
    """Try to coerce a string to boolean.

    Args:
        value: String value to coerce

    Returns:
        Boolean value or None if not a boolean string
    """
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _is_integer(value: str) -> bool:
    """Check if string represents an integer."""
    return value.isdigit() or (value.startswith("-") and value[1:].isdigit())


def _try_parse_float(value: str) -> float | None:
    """Try to parse string as float."""
    if value.replace(".", "", 1).replace("-", "", 1).isdigit():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_string_to_number(value: str) -> int | float | None:
    """Try to coerce a string to int or float.

    Args:
        value: String value to coerce

    Returns:
        Int, float, or None if not a number string
    """
    if _is_integer(value):
        return int(value)
    return _try_parse_float(value)


def _coerce_string(value: str) -> Any:
    """Coerce a string to bool or number if possible."""
    bool_value = _coerce_string_to_bool(value)
    if bool_value is not None:
        return bool_value

    num_value = _coerce_string_to_number(value)
    if num_value is not None:
        return num_value

    return value


def _coerce_dict(data: dict[Any, Any]) -> dict[Any, Any]:
    """Recursively coerce dictionary values."""
    return {key: coerce_json_types(value) for key, value in data.items()}


def _coerce_list(data: list[Any]) -> list[Any]:
    """Recursively coerce list items."""
    return [coerce_json_types(item) for item in data]


def coerce_json_types(data: Any) -> Any:
    """Coerce string representations to proper JSON types.

    This handles type coercion for parameters passed as strings by Docker MCP CLI.
    FastMCP's OpenAPI integration doesn't coerce types when making HTTP requests,
    so we need to do it here.

    Args:
        data: Input data (dict, list, or primitive)

    Returns:
        Data with coerced types
    """
    if isinstance(data, dict):
        return _coerce_dict(data)
    if isinstance(data, list):
        return _coerce_list(data)
    if isinstance(data, str):
        return _coerce_string(data)
    return data


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


def _build_query_params(**kwargs: Any) -> dict[str, Any]:
    """Build a query-param dict, dropping None values and normalizing booleans."""
    params = {k: v for k, v in kwargs.items() if v is not None}
    return {k: ("true" if v else "false") if isinstance(v, bool) else v for k, v in params.items()}


def _coerce_json_arg(value: Any) -> Any:
    """Parse a JSON object/array string argument into its Python equivalent.

    The Docker MCP CLI passes object/array parameters as strings (e.g.
    ``'{"key": true}'``). This helper transparently converts those strings so
    the grouped tools can accept either a JSON string or the native Python type.
    Primitive strings (entry IDs, domains, etc.) are left unchanged to avoid
    silently coercing values like ``"true"`` or ``"123"`` into non-string types.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(value)
            except json.JSONDecodeError, TypeError:
                return value
    return value


async def _api_request(method: str, url: str, params: dict[str, Any] | None = None, json: Any = None) -> dict[str, Any]:
    """Make an HTTP request through the access-controlled client and return JSON."""
    try:
        response = await api_client.request(method, url, params=params, json=json)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"success": True}
        return response.json()
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in {method} {url}: {e}")
        error_response = getattr(e, "response", None)
        status_code = error_response.status_code if error_response is not None else None
        return {"error": f"HTTP error: {e}", "status_code": status_code}
    except Exception as e:
        logger.error(f"Unexpected error in {method} {url}: {e}")
        return {"error": f"Unexpected error: {e}"}


def create_access_denied_response(method: str, url: str, error_msg: str, profile_id: str) -> httpx.Response:
    """Create a 403 Forbidden response for access denied scenarios.

    Args:
        method: HTTP method
        url: Request URL
        error_msg: Error message to include in response
        profile_id: The profile ID that was denied access

    Returns:
        403 Forbidden Response object
    """
    response = httpx.Response(
        status_code=403,
        json={"error": error_msg, "profile_id": profile_id},
        request=httpx.Request(method, str(url)),
    )
    return response


class AccessControlledClient(httpx.AsyncClient):
    """HTTP client wrapper that enforces profile access control."""

    def _check_write_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check write access and return error response if denied."""
        if can_write_profile(profile_id):
            return None

        if is_read_only():
            error_msg = "Write operation denied: server is in read-only mode"
        else:
            error_msg = f"Write access denied for profile: {profile_id}"

        logger.warning(f"{error_msg} (method={method}, url={url})")
        return create_access_denied_response(method, url, error_msg, profile_id)

    def _check_read_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check read access and return error response if denied."""
        if can_read_profile(profile_id):
            return None

        error_msg = f"Read access denied for profile: {profile_id}"
        logger.warning(f"{error_msg} (method={method}, url={url})")
        return create_access_denied_response(method, url, error_msg, profile_id)

    def _check_access(self, profile_id: str, method: str, url: str) -> httpx.Response | None:
        """Check access control for profile operations."""
        if is_write_operation(method):
            return self._check_write_access(profile_id, method, url)
        return self._check_read_access(profile_id, method, url)

    def _coerce_json_body(self, kwargs: dict[str, Any]) -> None:
        """Coerce string types in JSON request body.

        This handles type coercion for parameters passed as strings by clients like
        Docker MCP CLI. FastMCP's OpenAPI integration may pass string values for
        boolean/integer fields which need to be coerced before sending to the API.
        """
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            kwargs["json"] = coerce_json_types(kwargs["json"])
            logger.debug(f"Coerced JSON body: {kwargs['json']}")

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        """Make an HTTP request with access control checks.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Returns:
            Response from the API, or a 403 Forbidden response if access is denied
        """
        logger.info(f"HTTP Request: {method} {url}")

        profile_id = extract_profile_id_from_url(str(url))
        if profile_id:
            error_response = self._check_access(profile_id, method, url)
            if error_response:
                return error_response

        self._coerce_json_body(kwargs)
        return await super().request(method, url, **kwargs)


def create_nextdns_client() -> httpx.AsyncClient:
    """Create an authenticated HTTP client for NextDNS API with access control.

    Returns:
        httpx.AsyncClient: Configured async HTTP client with authentication and access control
    """
    headers = {
        "X-Api-Key": get_api_key(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # Remove any headers that weren't set to actual values to satisfy type checkers
    clean_headers = {key: value for key, value in headers.items() if value is not None}

    return AccessControlledClient(
        base_url=NEXTDNS_BASE_URL,
        headers=clean_headers,
        timeout=get_http_timeout(),
        follow_redirects=True,
    )


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
                "Provider %s has no _tools dict; OpenAPI tool cleanup skipped. " "FastMCP shape may have changed.",
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


# Create authenticated HTTP client (module-level for access by helper functions)
logger.info(f"Creating HTTP client for {NEXTDNS_BASE_URL}")
api_client = create_nextdns_client()

# Create the MCP server instance
mcp_server = create_mcp_server(api_client)


# Custom MCP tools (replacing the ~80 OpenAPI-generated atomic tools)


def _get_target_profile(profile_id: Optional[str]) -> str | None:
    """Get the target profile ID, using default if not specified."""
    if profile_id:
        return profile_id

    # Use config function to get default profile
    return get_default_profile()


def _validate_record_type(record_type: str) -> tuple[bool, str]:
    """Validate DNS record type.

    Returns:
        Tuple of (is_valid, record_type_upper)
    """
    record_type_upper = record_type.upper()
    is_valid = record_type_upper in VALID_DNS_RECORD_TYPES
    return is_valid, record_type_upper


def _build_doh_metadata(
    profile_id: str, domain: str, record_type: str, doh_url: str, status: int | None
) -> dict[str, Any]:
    """Build metadata for DoH response."""
    metadata: dict[str, Any] = {
        "profile_id": profile_id,
        "query_domain": domain,
        "query_type": record_type,
        "doh_endpoint": f"{doh_url}?name={domain}&type={record_type}",
    }

    if status is not None:
        status_desc = DNS_STATUS_CODES.get(status, f"Unknown status code: {status}")
        metadata["status_description"] = status_desc

    return metadata


async def doh_lookup(doh_url: str, domain: str, record_type: str, target_profile: str) -> dict[str, Any]:
    """Execute DoH query and return result with metadata."""
    params = {"name": domain, "type": record_type}
    headers = {"accept": "application/dns-json"}

    try:
        async with httpx.AsyncClient(timeout=get_http_timeout()) as client:
            response = await client.get(doh_url, params=params, headers=headers)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            result["_metadata"] = _build_doh_metadata(
                target_profile, domain, record_type, doh_url, result.get("Status")
            )
            if result.get("Status") is not None:
                logger.debug(f"DoH lookup result: {domain} -> {result['_metadata']['status_description']}")
            return result
    except Exception as e:
        error_type = "HTTP error" if isinstance(e, httpx.HTTPError) else "Unexpected error"
        logger.error(f"{error_type} during DoH lookup for {domain}: {str(e)}")
        return {
            "error": f"{error_type} during DoH lookup: {str(e)}",
            "profile_id": target_profile,
            "domain": domain,
            "type": record_type,
        }


async def _dohLookup_impl(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> dict[str, Any]:
    """Implementation of DoH lookup functionality.

    See dohLookup() for full documentation.
    """
    target_profile = _get_target_profile(profile_id)
    if not target_profile:
        return {
            "error": "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
            "hint": "Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
        }

    is_valid, record_type_upper = _validate_record_type(record_type)
    if not is_valid:
        logger.warning(f"Invalid DNS record type requested: {record_type}")
        return {
            "error": f"Invalid record type: {record_type}",
            "valid_types": VALID_DNS_RECORD_TYPES,
        }

    doh_url = f"https://dns.nextdns.io/{target_profile}/dns-query"
    logger.info(f"DoH lookup: {domain} ({record_type_upper}) via profile {target_profile}")
    return await doh_lookup(doh_url, domain, record_type_upper, target_profile)


@mcp_server.tool()
async def dohLookup(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> dict[str, Any]:
    """Perform a DNS-over-HTTPS lookup using a NextDNS profile.

    Args:
        domain: The domain name to look up (e.g., "adwords.google.com")
        profile_id: NextDNS profile ID. If not provided, uses NEXTDNS_DEFAULT_PROFILE.
        record_type: DNS record type to query (default "A").

    Returns:
        dict: DNS response in JSON format plus a ``_metadata`` field.
    """
    return await _dohLookup_impl(domain, profile_id, record_type)


# Metrics supported by the analytics time-series plotting tools.
_PLOT_ANALYTICS_METRICS = frozenset(
    {
        "status",
        "devices",
        "protocols",
        "queryTypes",
        "ipVersions",
        "dnssec",
        "encryption",
        "reasons",
        "ips",
    }
)


def _extract_series_label(series: dict[str, Any], index: int) -> str:
    """Return a human-readable label for a time-series data entry."""
    for key in ("name", "status", "protocol", "version", "id"):
        value = series.get(key)
        if value is not None and value != "":
            return str(value)
    if "validated" in series:
        return "validated" if series["validated"] else "not_validated"
    if "encrypted" in series:
        return "encrypted" if series["encrypted"] else "unencrypted"
    return f"series_{index}"


def _parse_series_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp returned by the NextDNS API."""
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S.%f%z")


def _render_series_chart(
    metric: str,
    times: list[str],
    series_data: list[dict[str, Any]],
) -> bytes:
    """Render a PNG line chart from time-series data and return the raw bytes."""
    parsed_times = [_parse_series_timestamp(t) for t in times]
    numeric_times = mdates.date2num(parsed_times)

    fig, ax = plt.subplots(figsize=(10, 6))
    for index, series in enumerate(series_data):
        label = _extract_series_label(series, index)
        queries = series.get("queries", [])
        ax.plot(numeric_times, queries, label=label, marker="o", markersize=3)

    ax.set_title(f"NextDNS Analytics: {metric}")
    ax.set_xlabel("Time")
    ax.set_ylabel("Queries")
    ax.legend()
    ax.tick_params(axis="x", rotation=30)
    fig.autofmt_xdate()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


async def _plot_analytics_series_impl(
    metric: str,
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-1d",
    to_time: str | int = "now",
    interval: int = 3600,
    alignment: str = "end",
    timezone: str = "GMT",
    partials: str = "none",
    limit: int = 10,
) -> dict[str, Any] | mcp.types.ImageContent:
    """Generate a PNG line chart from a NextDNS analytics time-series endpoint."""
    if metric not in _PLOT_ANALYTICS_METRICS:
        return {
            "error": f"Unsupported metric: {metric}",
            "supported_metrics": sorted(_PLOT_ANALYTICS_METRICS),
        }

    if interval < 60:
        return {
            "error": "interval must be at least 60 seconds",
            "minimum_interval": 60,
        }

    target_profile = _get_target_profile(profile_id)
    if not target_profile:
        return {
            "error": "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
            "hint": "Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
        }

    params: dict[str, Any] = {
        "from": from_time,
        "to": to_time,
        "interval": interval,
        "alignment": alignment,
        "timezone": timezone,
        "partials": partials,
        "limit": limit,
    }

    url = f"/profiles/{target_profile}/analytics/{metric};series"
    logger.info(f"Plotting analytics series: {metric} for profile {target_profile}")

    try:
        response = await api_client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as e:
        logger.error(f"HTTP error while fetching analytics series {metric}: {e}")
        return {"error": f"HTTP error while fetching analytics series: {e}"}
    except Exception as e:
        logger.error(f"Unexpected error while fetching analytics series {metric}: {e}")
        return {"error": f"Unexpected error while fetching analytics series: {e}"}

    meta = payload.get("meta", {})
    series_meta = meta.get("series", {})
    times = series_meta.get("times", [])
    series_data = payload.get("data", [])

    if not times or not series_data:
        return {
            "error": "No time-series data available to plot",
            "metric": metric,
            "profile_id": target_profile,
        }

    try:
        png_bytes = await asyncio.to_thread(_render_series_chart, metric, times, series_data)
    except Exception as e:
        logger.error(f"Error rendering chart for {metric}: {e}")
        return {"error": f"Error rendering chart: {e}"}

    return Image(data=png_bytes, format="png").to_image_content()


async def _manage_profiles_impl(
    operation: ProfileOperation,
    profile_id: OptionalProfileId = None,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for NextDNS profiles."""
    if operation == "list":
        return await _api_request("GET", "/profiles")

    if operation == "create":
        if not name:
            return {"error": "name is required for create operation"}
        return await _api_request("POST", "/profiles", json={"name": name})

    if not profile_id:
        return {"error": "profile_id is required for this operation"}

    url = f"/profiles/{profile_id}"
    if operation == "get":
        return await _api_request("GET", url)
    if operation == "update":
        if not name:
            return {"error": "name is required for update operation"}
        return await _api_request("PATCH", url, json={"name": name})
    if operation == "delete":
        return await _api_request("DELETE", url)

    return {"error": f"Unsupported operation: {operation}"}


async def _manage_settings_impl(
    operation: Literal["get", "update"],
    category: SettingsCategory,
    profile_id: str,
    settings: Optional[Union[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for profile settings categories."""
    path = _SETTINGS_PATHS[category]
    url = f"/profiles/{profile_id}/{path}"

    if operation == "get":
        return await _api_request("GET", url)
    if operation == "update":
        if settings is None:
            return {"error": "settings is required for update operation"}
        settings = _coerce_json_arg(settings)
        if not isinstance(settings, dict):
            return {"error": "settings must be a JSON object"}
        return await _api_request("PATCH", url, json=settings)

    return {"error": f"Unsupported operation: {operation}"}


async def _lists_get(base_url: str) -> dict[str, Any]:
    """Fetch the current list entries."""
    return await _api_request("GET", base_url)


async def _lists_add(base_url: str, entry: Optional[Union[str, dict[str, Any]]]) -> dict[str, Any]:
    """Add a single entry to a list."""
    if entry is None:
        return {"error": "entry is required for add operation"}
    entry = _coerce_json_arg(entry)
    body = entry if isinstance(entry, dict) else {"id": entry}
    return await _api_request("POST", base_url, json=body)


async def _lists_replace(base_url: str, entries: Optional[Union[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """Replace the entire list with a new set of entries."""
    if entries is None:
        return {"error": "entries is required for replace operation"}
    entries = _coerce_json_arg(entries)
    if not isinstance(entries, list):
        return {"error": "entries must be a JSON array"}
    return await _api_request("PUT", base_url, json=entries)


async def _lists_update(
    base_url: str,
    list_type: ListType,
    entry_id: Optional[str],
    entry: Optional[Union[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Update a single list entry by id (only for supported list types)."""
    if list_type not in _LIST_UPDATEABLE_TYPES:
        return {
            "error": f"update is not supported for list_type={list_type}",
            "supported_list_types": sorted(_LIST_UPDATEABLE_TYPES),
        }
    if entry_id is None:
        return {"error": "entry_id is required for update operation"}
    entry = _coerce_json_arg(entry)
    if not isinstance(entry, dict):
        return {"error": "entry must be a dict for update operation"}
    return await _api_request("PATCH", f"{base_url}/{entry_id}", json=entry)


async def _lists_remove(base_url: str, entry_id: Optional[str]) -> dict[str, Any]:
    """Remove a single list entry by id."""
    if entry_id is None:
        return {"error": "entry_id is required for remove operation"}
    return await _api_request("DELETE", f"{base_url}/{entry_id}")


async def _manage_lists_impl(
    list_type: ListType,
    operation: ListOperation,
    profile_id: str,
    entry_id: Optional[str] = None,
    entry: Optional[Union[str, dict[str, Any]]] = None,
    entries: Optional[Union[str, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for content/privacy/security/parental lists."""
    path = _LIST_PATHS[list_type]
    base_url = f"/profiles/{profile_id}/{path}"

    if operation == "get":
        return await _lists_get(base_url)
    if operation == "add":
        return await _lists_add(base_url, entry)
    if operation == "replace":
        return await _lists_replace(base_url, entries)
    if operation == "update":
        return await _lists_update(base_url, list_type, entry_id, entry)
    if operation == "remove":
        return await _lists_remove(base_url, entry_id)

    return {"error": f"Unsupported operation: {operation}"}


async def _manage_rewrites_impl(
    operation: RewriteOperation,
    profile_id: str,
    name: Optional[str] = None,
    content: Optional[str] = None,
    entry_id: Optional[str] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for DNS rewrite entries."""
    base_url = f"/profiles/{profile_id}/rewrites"

    if operation == "list":
        return await _api_request("GET", base_url)

    if operation == "add":
        if not name or not content:
            return {"error": "name and content are required for add operation"}
        return await _api_request("POST", base_url, json={"name": name, "content": content})

    if operation == "delete":
        if not entry_id:
            return {"error": "entry_id is required for delete operation"}
        return await _api_request("DELETE", f"{base_url}/{entry_id}")

    return {"error": f"Unsupported operation: {operation}"}


async def _manage_logs_impl(
    operation: LogOperation,
    profile_id: str,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    limit: Optional[int] = None,
    user: Optional[str] = None,
    device: Optional[str] = None,
    raw: Optional[bool] = None,
) -> dict[str, Any]:
    """Grouped implementation for query logs (get, clear, download)."""
    base_url = f"/profiles/{profile_id}/logs"

    if operation == "get":
        params = _build_query_params(
            **{"from": from_time, "to": to_time, "limit": limit, "device": device, "search": user, "raw": raw}
        )
        return await _api_request("GET", base_url, params=params)

    if operation == "clear":
        return await _api_request("DELETE", base_url)

    if operation == "download":
        try:
            response = await api_client.get(f"{base_url}/download")
            response.raise_for_status()
            return {
                "content_type": response.headers.get("content-type"),
                "size": len(response.content),
                "data": response.text,
            }
        except httpx.HTTPError as e:
            logger.error(f"HTTP error downloading logs: {e}")
            return {"error": f"HTTP error downloading logs: {e}"}
        except Exception as e:
            logger.error(f"Unexpected error downloading logs: {e}")
            return {"error": f"Unexpected error downloading logs: {e}"}

    return {"error": f"Unsupported operation: {operation}"}


async def _query_analytics_impl(
    metric: AnalyticsMetric,
    profile_id: str,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    interval: Optional[int] = None,
    alignment: Optional[str] = None,
    timezone: Optional[str] = None,
    partials: Optional[str] = None,
    limit: Optional[int] = None,
    destination_type: Optional[str] = None,
    series: bool = False,
    cursor: Optional[str] = None,
    device: Optional[str] = None,
    status: Optional[str] = None,
    root: Optional[bool] = None,
) -> dict[str, Any]:
    """Grouped implementation for NextDNS analytics endpoints."""
    suffix = ";series" if series else ""
    url = f"/profiles/{profile_id}/analytics/{metric}{suffix}"

    params: dict[str, Any] = _build_query_params(
        **{"from": from_time, "to": to_time, "limit": limit, "cursor": cursor, "device": device}
    )

    if series:
        params.update(
            _build_query_params(
                interval=interval,
                alignment=alignment,
                timezone=timezone,
                partials=partials,
            )
        )

    if metric == "destinations":
        if not destination_type:
            return {"error": "destination_type is required for destinations metric"}
        params["type"] = destination_type

    if metric == "domains":
        params.update(_build_query_params(status=status, root=root))

    return await _api_request("GET", url, params=params)


# Register the grouped MCP tools


@mcp_server.tool()
async def manageProfiles(
    operation: ProfileOperation,
    profile_id: OptionalProfileId = None,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """Manage NextDNS profiles.

    NextDNS profiles are named configurations that contain DNS settings, blocklists,
    analytics, and logs. Most other tools require a ``profile_id`` from this tool.

    Operations:
        - ``list``: Return all profiles the API key can access.
        - ``create``: Create a new profile (requires ``name``).
        - ``get``: Retrieve a single profile (requires ``profile_id``).
        - ``update``: Rename a profile (requires ``profile_id`` and ``name``).
        - ``delete``: Remove a profile (requires ``profile_id``).

    Examples:
        - list:  ``manageProfiles(operation="list")``
        - create: ``manageProfiles(operation="create", name="Home Network")``
        - get:   ``manageProfiles(operation="get", profile_id="abc123")``
    """
    return await _manage_profiles_impl(operation, profile_id, name)


@mcp_server.tool()
async def manageSettings(
    operation: Literal["get", "update"],
    category: SettingsCategory,
    profile_id: str,
    settings: Optional[Union[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Manage a settings category for a NextDNS profile.

    Settings categories:
        - ``general``: Core profile settings (e.g., name, web3 blocking).
        - ``privacy``: Privacy features such as disguised-tracker blocking and affiliate links.
        - ``security``: Threat intelligence, Google Safe Browsing, typosquatting protection.
        - ``parental``: Parental control enablement (safe search, YouTube restricted mode).
        - ``performance``: ECS, cache boost, and other performance options.
        - ``logs``: Query-logging enablement and retention.
        - ``blockpage``: Whether to show a custom block page for blocked queries.

    Operations:
        - ``get``: Retrieve current settings for the category.
        - ``update``: Apply new settings (requires ``settings`` payload).

    The ``settings`` argument can be a Python dict or a JSON string. For ``update``,
    first call ``get`` to see the current schema, then send only the fields you want
    to change.

    Examples:
        - get: ``manageSettings(operation="get", category="privacy", profile_id="abc123")``
        - update: ``manageSettings(operation="update", category="privacy", profile_id="abc123", settings={"disguisedTrackers": True})``
    """
    return await _manage_settings_impl(operation, category, profile_id, settings)


@mcp_server.tool()
async def manageLists(
    list_type: ListType,
    operation: ListOperation,
    profile_id: str,
    entry_id: Optional[str] = None,
    entry: Optional[Union[str, dict[str, Any]]] = None,
    entries: Optional[Union[str, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Manage allow/deny/block lists for a NextDNS profile.

    List types:
        - ``allowlist`` / ``denylist``: Always-allow or always-block specific domains.
        - ``privacy_blocklists``: Subscribed blocklists (e.g., ``nextdns-recommended``).
        - ``privacy_natives``: Native tracking blockers (e.g., ``apple``, ``facebook``).
        - ``security_tlds``: Dangerous top-level domains to block (e.g., ``zip``).
        - ``parental_categories``: Content categories (e.g., ``gambling``, ``porn``).
        - ``parental_services``: Specific apps/services (e.g., ``tiktok``, ``youtube``).

    Operations:
        - ``get``: Return the current list.
        - ``add``: Append one entry (pass ``entry`` as ``{"id": "value"}`` or as a plain id string).
        - ``remove``: Delete one entry by ``entry_id``.
        - ``update``: Toggle an existing entry by ``entry_id`` (pass ``entry={"active": True|False}``).
          Only supported for ``allowlist``, ``denylist``, ``parental_categories``, and
          ``parental_services``.
        - ``replace``: Replace the entire list with ``entries`` (list of dicts).

    Examples:
        - get: ``manageLists(list_type="denylist", operation="get", profile_id="abc123")``
        - add: ``manageLists(list_type="denylist", operation="add", profile_id="abc123", entry={"id": "example.com"})``
        - remove: ``manageLists(list_type="denylist", operation="remove", profile_id="abc123", entry_id="example.com")``
        - replace: ``manageLists(list_type="privacy_blocklists", operation="replace", profile_id="abc123", entries=[{"id": "nextdns-recommended"}])``
    """
    return await _manage_lists_impl(list_type, operation, profile_id, entry_id, entry, entries)


@mcp_server.tool()
async def manageRewrites(
    operation: RewriteOperation,
    profile_id: str,
    name: Optional[str] = None,
    content: Optional[str] = None,
    entry_id: Optional[str] = None,
) -> dict[str, Any]:
    """Manage DNS rewrite entries for a NextDNS profile.

    Rewrites let you return a custom answer for a hostname. Typical uses:
    - Point an internal hostname to a private IP.
    - Block a domain by rewriting it to ``0.0.0.0``.

    Operations:
        - ``list``: Show existing rewrites.
        - ``add``: Create a rewrite (requires ``name`` and ``content``).
        - ``delete``: Remove a rewrite (requires ``entry_id`` from ``list``).

    Examples:
        - list: ``manageRewrites(operation="list", profile_id="abc123")``
        - add: ``manageRewrites(operation="add", profile_id="abc123", name="router.home", content="192.168.1.1")``
        - delete: ``manageRewrites(operation="delete", profile_id="abc123", entry_id="router.home")``
    """
    return await _manage_rewrites_impl(operation, profile_id, name, content, entry_id)


@mcp_server.tool()
async def manageLogs(
    operation: LogOperation,
    profile_id: str,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    limit: Optional[int] = None,
    user: Optional[str] = None,
    device: Optional[str] = None,
    raw: Optional[bool] = None,
) -> dict[str, Any]:
    """Manage query logs for a NextDNS profile.

    Operations:
        - ``get``: Return recent query log entries (use ``limit`` to cap results).
          Set ``raw=true`` to bypass deduplication/noise filtering.
        - ``clear``: Delete all stored logs for the profile.
        - ``download``: Download retained logs as CSV. ``from_time`` and ``to_time`` are
          ignored by the NextDNS download endpoint.

    Time values can be Unix timestamps or relative strings like ``-1d`` or ``-7d``.
    They are only used by ``get``.

    Examples:
        - get recent: ``manageLogs(operation="get", profile_id="abc123", limit=10)``
        - get raw logs: ``manageLogs(operation="get", profile_id="abc123", raw=true)``
        - download: ``manageLogs(operation="download", profile_id="abc123", from_time="-1d")``
        - clear: ``manageLogs(operation="clear", profile_id="abc123")``
    """
    return await _manage_logs_impl(operation, profile_id, from_time, to_time, limit, user, device, raw)


@mcp_server.tool()
async def queryAnalytics(
    metric: AnalyticsMetric,
    profile_id: str,
    from_time: Optional[str | int] = None,
    to_time: Optional[str | int] = None,
    interval: Optional[int] = None,
    alignment: Optional[str] = None,
    timezone: Optional[str] = None,
    partials: Optional[str] = None,
    limit: Optional[int] = None,
    destination_type: Optional[str] = None,
    series: bool = False,
    cursor: Optional[str] = None,
    device: Optional[str] = None,
    status: Optional[str] = None,
    root: Optional[bool] = None,
) -> dict[str, Any]:
    """Query NextDNS analytics metrics.

    Metrics:
        - ``status``: Query resolution status (default, blocked, allowed, relayed).
        - ``devices``: Queries per device.
        - ``protocols``: DNS transport protocol (DoH, DoT, Do53 UDP/TCP, DoQ).
        - ``queryTypes``: DNS record types requested (A, AAAA, CNAME, etc.).
        - ``ipVersions``: IPv4 vs IPv6 queries.
        - ``dnssec``: DNSSEC validation results.
        - ``encryption``: Encrypted vs unencrypted queries.
        - ``reasons``: Why queries were blocked or allowed.
        - ``ips``: Top source IPs.
        - ``destinations``: Top destinations; requires ``destination_type`` such as
          ``countries`` or ``gafam``.

    Set ``series=true`` to fetch time-series data instead of aggregate totals.
    Time values can be Unix timestamps or relative strings like ``-1d``.

    Optional filters:
        - ``cursor``: Pagination cursor from a previous response.
        - ``device``: Filter analytics to a single device id.
        - ``status``: For the ``domains`` metric, filter by resolution status.
        - ``root``: For the ``domains`` metric, group results by root domain (boolean).

    Examples:
        - totals: ``queryAnalytics(metric="status", profile_id="abc123", from_time="-1d")``
        - time series: ``queryAnalytics(metric="status", profile_id="abc123", from_time="-1d", series=true)``
        - destinations: ``queryAnalytics(metric="destinations", profile_id="abc123", from_time="-1d", destination_type="countries")``
    """
    return await _query_analytics_impl(
        metric,
        profile_id,
        from_time,
        to_time,
        interval,
        alignment,
        timezone,
        partials,
        limit,
        destination_type,
        series,
        cursor,
        device,
        status,
        root,
    )


@mcp_server.tool()
async def plotAnalytics(
    metric: PlotMetric,
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-1d",
    to_time: str | int = "now",
    interval: int = 3600,
    alignment: str = "end",
    timezone: str = "GMT",
    partials: str = "none",
    limit: int = 10,
) -> dict[str, Any] | mcp.types.ImageContent:
    """Generate a PNG line chart for a NextDNS analytics time-series metric.

    Use this to visualize query trends over time. The profile should have recent
    query history; otherwise the tool returns an error explaining that no data is
    available.

    Supported metrics: ``status``, ``devices``, ``protocols``, ``queryTypes``,
    ``ipVersions``, ``dnssec``, ``encryption``, ``reasons``, ``ips``.

    Time values can be Unix timestamps or relative strings like ``-1d``.

    Examples:
        - ``plotAnalytics(metric="status", profile_id="abc123", from_time="-1d")``
        - ``plotAnalytics(metric="devices", profile_id="abc123", from_time="-7d", interval=86400)``

    Returns:
        An MCP ImageContent PNG chart, or an error dict if data is unavailable.
    """
    return await _plot_analytics_series_impl(
        metric=metric,
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        interval=interval,
        alignment=alignment,
        timezone=timezone,
        partials=partials,
        limit=limit,
    )


@mcp_server.prompt(
    name="nextdns-usage-guide",
    description="Comprehensive guide for using the NextDNS MCP server tools",
)
def nextdns_usage_guide() -> str:
    """Return a detailed usage guide for the NextDNS MCP tools."""
    return """# NextDNS MCP Server Usage Guide

This MCP server exposes NextDNS through a small set of grouped tools. Each tool
maps to a functional area of the NextDNS API.

## Core concepts

- **Profile**: A named NextDNS configuration that owns settings, lists, logs,
  analytics, and rewrites. Most tools require a `profile_id`.
- **Default profile**: If `NEXTDNS_DEFAULT_PROFILE` is set, tools that accept an
  optional `profile_id` will use it automatically.
- **Access control**: The server reads `NEXTDNS_READABLE_PROFILES` and
  `NEXTDNS_WRITABLE_PROFILES`. Reads/writes outside those profiles are rejected.

## Available tools

### manageProfiles
List, create, get, update, or delete profiles. Use this first to discover the
`profile_id` you need for other tools.

- `operation="list"`
- `operation="create" name="My Profile"`
- `operation="get" profile_id="abc123"`
- `operation="update" profile_id="abc123" name="New Name"`
- `operation="delete" profile_id="abc123"`

### manageSettings
Get or update one of the seven settings categories:

- `general` — core profile options (e.g., web3 blocking)
- `privacy` — disguised trackers, affiliate links
- `security` — threat intelligence, Google Safe Browsing
- `parental` — safe search, YouTube restricted mode
- `performance` — ECS, cache boost
- `logs` — logging enablement and retention
- `blockpage` — custom block page toggle

When updating, first call `get` to inspect the current schema, then pass only the
fields you want to change in `settings`.

### manageLists
Manage allow/deny/block lists:

- `allowlist` / `denylist` — per-domain overrides
- `privacy_blocklists` — subscribed blocklists
- `privacy_natives` — native tracking blockers
- `security_tlds` — dangerous TLDs
- `parental_categories` — content categories
- `parental_services` — specific apps/services

Operations: `get`, `add`, `remove`, `update`, `replace`.

For `add`, pass `entry={"id": "value"}`. For `remove`/`update`, pass
`entry_id`. For `replace`, pass `entries=[{"id": "value"}, ...]`.

### manageRewrites
Create custom DNS responses for a hostname:

- `operation="list"`
- `operation="add" name="router.home" content="192.168.1.1"`
- `operation="delete" entry_id="router.home"`

### manageLogs
Inspect or export query logs:

- `operation="get"` — recent entries (set `raw=true` for unfiltered logs)
- `operation="clear"` — delete stored logs
- `operation="download"` — CSV export

Time values can be Unix timestamps or relative strings such as `-1d`.

### queryAnalytics
Fetch analytics for a profile. Metrics:

- `status`, `devices`, `protocols`, `queryTypes`, `ipVersions`, `dnssec`,
  `encryption`, `reasons`, `ips`, `destinations`

Set `series=true` for time-series data. The `destinations` metric requires
`destination_type` (e.g., `countries` or `gafam`).

### plotAnalytics
Generate a PNG line chart for supported metrics. Use a profile with query
history. Returns an MCP image or an error if no data is available.

### dohLookup
Perform a DNS-over-HTTPS lookup through NextDNS:

- `dohLookup(domain="example.com", profile_id="abc123", record_type="A")`

## Common workflows

### Block a domain
1. `manageLists(list_type="denylist", operation="add", profile_id="abc123", entry={"id": "bad.example.com"})`

### Allow a domain
1. `manageLists(list_type="allowlist", operation="add", profile_id="abc123", entry={"id": "safe.example.com"})`

### View blocked query trends
1. `queryAnalytics(metric="status", profile_id="abc123", from_time="-1d", series=true)`
2. `plotAnalytics(metric="status", profile_id="abc123", from_time="-1d")`

### Add a DNS rewrite
1. `manageRewrites(operation="add", profile_id="abc123", name="router.home", content="192.168.1.1")`
"""


def get_mcp_run_options() -> dict[str, Any]:
    """Build MCP server run options based on environment configuration.

    Returns:
        Dictionary of options to pass to mcp.run() via **kwargs.
        Empty dict for stdio (default), or dict with transport/host/port for HTTP.
    """
    transport_mode = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport_mode == "http":
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        logger.info(f"  Transport: HTTP streamable on {host}:{port}")
        logger.info(f"  MCP endpoint: http://{host}:{port}/mcp")
        return {"transport": "http", "host": host, "port": port}

    # Default: stdio transport
    logger.info("  Transport: stdio")
    return {}


if __name__ == "__main__":  # pragma: no cover
    # Note: This block is excluded from unit test coverage because:
    # 1. It only executes when running `python -m nextdns_mcp.server` directly
    # 2. When pytest imports the module, __name__ != "__main__"
    # 3. The mcp_server.run() call starts a blocking event loop unsuitable for unit tests
    # The options building logic IS tested via tests/unit/test_mcp_run_options.py
    logger.info("Starting NextDNS MCP Server...")
    logger.info(f"  Base URL: {NEXTDNS_BASE_URL}")
    logger.info(f"  Timeout: {get_http_timeout()}s")
    mcp_server.run(**get_mcp_run_options())
