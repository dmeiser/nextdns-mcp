"""NextDNS MCP Server - FastMCP-based implementation using OpenAPI spec.

SPDX-License-Identifier: MIT
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.openapi import DEFAULT_ROUTE_MAPPINGS, RouteMap

from .config import (
    DNS_STATUS_CODES,
    EXCLUDED_ROUTES,
    GLOBALLY_ALLOWED_OPERATIONS,
    NEXTDNS_BASE_URL,
    VALID_DNS_RECORD_TYPES,
    can_read_profile,
    can_write_profile,
    get_api_key,
    get_default_profile,
    get_http_timeout,
    is_read_only,
)

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


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


def _coerce_string_to_number(value: str) -> int | float | None:
    """Try to coerce a string to int or float.

    Args:
        value: String value to coerce

    Returns:
        Int, float, or None if not a number string
    """
    # Try integer
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)

    # Try float
    if value.replace(".", "", 1).replace("-", "", 1).isdigit():
        try:
            return float(value)
        except ValueError:
            pass

    return None


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
        return {key: coerce_json_types(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [coerce_json_types(item) for item in data]
    elif isinstance(data, str):
        # Try boolean coercion
        bool_value = _coerce_string_to_bool(data)
        if bool_value is not None:
            return bool_value

        # Try number coercion
        num_value = _coerce_string_to_number(data)
        if num_value is not None:
            return num_value

    return data


def create_access_denied_response(
    method: str, url: str, error_msg: str, profile_id: str
) -> httpx.Response:
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

    async def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Make an HTTP request with access control checks.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Returns:
            Response from the API, or a 403 Forbidden response if access is denied
        """
        # Log the actual URL being requested for debugging
        logger.info(f"HTTP Request: {method} {url}")

        # Extract profile_id from URL if present
        profile_id = extract_profile_id_from_url(str(url))

        # Only check access if URL contains a profile_id
        if profile_id:
            is_write = is_write_operation(method)

            if is_write:
                error_response = self._check_write_access(profile_id, method, url)
            else:
                error_response = self._check_read_access(profile_id, method, url)

            if error_response:
                return error_response

        # Coerce string types in JSON body (handles Docker MCP CLI passing everything as strings)
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            kwargs["json"] = coerce_json_types(kwargs["json"])
            logger.debug(f"Coerced JSON body: {kwargs['json']}")

        # Access allowed, proceed with the request
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


def create_mcp_server() -> FastMCP:
    """Create and configure the NextDNS MCP server.

    Returns:
        FastMCP: Configured MCP server instance

    Raises:
        FileNotFoundError: If OpenAPI spec cannot be found
        yaml.YAMLError: If OpenAPI spec is invalid
    """
    # Load the OpenAPI specification
    logger.info("Loading NextDNS OpenAPI specification...")
    openapi_spec = load_openapi_spec()

    # Create authenticated HTTP client
    logger.info(f"Creating HTTP client for {NEXTDNS_BASE_URL}")
    api_client = create_nextdns_client()

    # Create MCP server from OpenAPI spec
    logger.info("Generating MCP server from OpenAPI specification...")
    route_maps = build_route_mappings()

    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=api_client,
        route_maps=route_maps,
        name="NextDNS MCP Server",
        timeout=get_http_timeout(),
    )

    # Add metadata about the server
    logger.info("MCP server created successfully")
    default_profile = get_default_profile()
    if default_profile:
        logger.info(f"Default profile: {default_profile}")

    return mcp


# Create the MCP server instance
mcp = create_mcp_server()


# Add custom DoH lookup tool
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


# Add custom DoH lookup tool
async def _dohLookup_impl(
    domain: str, profile_id: Optional[str] = None, record_type: str = "A"
) -> dict[str, Any]:
    """Implementation of DoH lookup functionality.

    See dohLookup() for full documentation.
    """
    # Get target profile
    target_profile = _get_target_profile(profile_id)
    if not target_profile:
        return {
            "error": "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
            "hint": "Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
        }

    # Validate record type
    is_valid, record_type_upper = _validate_record_type(record_type)
    if not is_valid:
        logger.warning(f"Invalid DNS record type requested: {record_type}")
        return {
            "error": f"Invalid record type: {record_type}",
            "valid_types": VALID_DNS_RECORD_TYPES,
        }

    # Construct DoH query URL
    doh_url = f"https://dns.nextdns.io/{target_profile}/dns-query"
    params = {"name": domain, "type": record_type_upper}
    headers = {"accept": "application/dns-json"}

    logger.info(f"DoH lookup: {domain} ({record_type_upper}) via profile {target_profile}")

    try:
        # Create a separate HTTP client for DoH queries (doesn't need API key)
        async with httpx.AsyncClient(timeout=get_http_timeout()) as client:
            response = await client.get(doh_url, params=params, headers=headers)
            response.raise_for_status()

            result: dict[str, Any] = response.json()

            # Add helpful metadata
            result["_metadata"] = _build_doh_metadata(
                target_profile,
                domain,
                record_type_upper,
                doh_url,
                result.get("Status"),
            )

            if result.get("Status") is not None:
                logger.debug(
                    f"DoH lookup result: {domain} -> {result['_metadata']['status_description']}"
                )

            return result

    except Exception as e:
        error_type = "HTTP error" if isinstance(e, httpx.HTTPError) else "Unexpected error"
        logger.error(f"{error_type} during DoH lookup for {domain}: {str(e)}")
        return {
            "error": f"{error_type} during DoH lookup: {str(e)}",
            "profile_id": target_profile,
            "domain": domain,
            "type": record_type_upper,
        }


# Register the DoH lookup tool with MCP
@mcp.tool()
async def dohLookup(
    domain: str, profile_id: Optional[str] = None, record_type: str = "A"
) -> dict[str, Any]:
    """Perform a DNS-over-HTTPS lookup using a NextDNS profile.

    This tool performs a DNS query through NextDNS's DoH endpoint, allowing you to test
    how a specific profile would resolve a domain name. This is useful for:
    - Testing if a domain is blocked by your profile settings
    - Verifying DNS resolution behavior
    - Debugging DNS-related issues
    - Testing allowlist/denylist configurations

    Args:
        domain: The domain name to look up (e.g., "adwords.google.com")
        profile_id: NextDNS profile ID (6-character alphanumeric). If not provided, uses NEXTDNS_DEFAULT_PROFILE
        record_type: DNS record type to query. Common types:
            - A: IPv4 address (default)
            - AAAA: IPv6 address
            - CNAME: Canonical name
            - MX: Mail exchange
            - TXT: Text records
            - NS: Name servers
            - SOA: Start of authority
            - PTR: Pointer record

    Returns:
        dict: DNS response in JSON format containing:
            - Status: Query status (0 = NOERROR, 2 = SERVFAIL, 3 = NXDOMAIN)
            - Answer: List of DNS records
            - Question: The query that was made
            - Additional metadata

    Example:
        # Check if adwords.google.com is blocked
        result = await dohLookup("adwords.google.com", "b282de", "A")

        # Check IPv6 address
        result = await dohLookup("google.com", "b282de", "AAAA")
    """
    return await _dohLookup_impl(domain, profile_id, record_type)


# Custom tools for array-based PUT endpoints (excluded from FastMCP auto-generation)
# These endpoints require raw JSON arrays, which FastMCP doesn't support directly.
# We provide custom implementations that accept JSON strings and convert them.


async def _bulk_update_helper(
    profile_id: str, data: str, endpoint: str, param_name: str
) -> dict[str, Any]:
    """Helper function for bulk update operations that require raw JSON arrays.

    This function handles the common pattern of:
    1. Parsing a JSON array string
    2. Validating it's actually an array
    3. Making a PUT request with the array as the body

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        data: JSON array string to send
        endpoint: API endpoint path (e.g., "/profiles/{profile_id}/denylist")
        param_name: Parameter name for error messages (e.g., "entries", "services")

    Returns:
        dict: API response or error dict
    """

    logger.info(f"Bulk update: {param_name} for profile {profile_id}")

    # Parse and validate JSON array
    try:
        array_data = json.loads(data)
        if not isinstance(array_data, list):
            logger.warning(
                f"Invalid {param_name} format: expected array, got {type(array_data).__name__}"
            )
            return {"error": f"{param_name} must be a JSON array string"}
        logger.debug(f"Parsed {len(array_data)} {param_name} items")
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode error for {param_name}: {str(e)}")
        return {"error": f"Invalid JSON: {str(e)}"}

    # Make PUT request with array body
    client = mcp._client  # type: ignore[attr-defined]
    url = endpoint.format(profile_id=profile_id)

    try:
        logger.debug(f"PUT request to {url}")
        response = await client.put(url, json=array_data)
        response.raise_for_status()
        logger.info(f"Bulk update successful: {param_name} for profile {profile_id}")
        result: dict[str, Any] = response.json()
        return result
    except httpx.HTTPError as e:
        logger.error(f"HTTP error during bulk update: {str(e)}")
        return {"error": f"HTTP error: {str(e)}"}


@mcp.tool()
async def updateDenylist(profile_id: str, entries: str) -> dict[str, Any]:
    """Update the denylist for a profile.

    Replace the entire denylist with the provided domains. All previous denylist
    entries will be removed and replaced with the new list.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        entries: JSON array string of domains to block, e.g. '["example.com", "bad.com"]'
                Each entry can be:
                - Domain name: "example.com"
                - Wildcard subdomain: "*.example.com"

    Returns:
        dict: Response containing the updated denylist

    Example:
        result = await updateDenylist("abc123", '["ads.example.com", "tracker.net"]')
    """
    return await _bulk_update_helper(
        profile_id, entries, "/profiles/{profile_id}/denylist", "entries"
    )


@mcp.tool()
async def updateAllowlist(profile_id: str, entries: str) -> dict[str, Any]:
    """Update the allowlist for a profile.

    Replace the entire allowlist with the provided domains. All previous allowlist
    entries will be removed and replaced with the new list.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        entries: JSON array string of domains to allow, e.g. '["safe.com", "trusted.org"]'
                Each entry can be:
                - Domain name: "example.com"
                - Wildcard subdomain: "*.example.com"

    Returns:
        dict: Response containing the updated allowlist

    Example:
        result = await updateAllowlist("abc123", '["safe.com", "trusted.org"]')
    """
    return await _bulk_update_helper(
        profile_id, entries, "/profiles/{profile_id}/allowlist", "entries"
    )


@mcp.tool()
async def updateParentalControlServices(profile_id: str, services: str) -> dict[str, Any]:
    """Update parental control services for a profile.

    Replace the entire list of blocked services with the provided service IDs.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        services: JSON array string of service IDs to block, e.g. '["tiktok", "fortnite", "roblox"]'
                 Common services: tiktok, fortnite, roblox, instagram, snapchat, facebook,
                 twitter, youtube, twitch, discord, whatsapp, telegram, zoom, etc.

    Returns:
        dict: Response containing the updated services list

    Example:
        result = await updateParentalControlServices("abc123", '["tiktok", "fortnite"]')
    """
    return await _bulk_update_helper(
        profile_id,
        services,
        "/profiles/{profile_id}/parentalControl/services",
        "services",
    )


@mcp.tool()
async def updateParentalControlCategories(profile_id: str, categories: str) -> dict[str, Any]:
    """Update parental control website categories for a profile.

    Replace the entire list of blocked website categories with the provided category IDs.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        categories: JSON array string of category IDs to block, e.g. '["gambling", "dating", "piracy"]'
                   Common categories: gambling, dating, piracy, porn, social-networks,
                   video-streaming, gaming, etc.

    Returns:
        dict: Response containing the updated categories list

    Example:
        result = await updateParentalControlCategories("abc123", '["gambling", "dating"]')
    """
    return await _bulk_update_helper(
        profile_id,
        categories,
        "/profiles/{profile_id}/parentalControl/categories",
        "categories",
    )


@mcp.tool()
async def updateSecurityTlds(profile_id: str, tlds: str) -> dict[str, Any]:
    """Update blocked top-level domains (TLDs) for a profile.

    Replace the entire list of blocked TLDs with the provided list.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        tlds: JSON array string of TLDs to block, e.g. '["zip", "mov", "xyz"]'
              Do not include the dot (use "com" not ".com")

    Returns:
        dict: Response containing the updated TLD list

    Example:
        result = await updateSecurityTlds("abc123", '["zip", "mov"]')
    """
    return await _bulk_update_helper(
        profile_id, tlds, "/profiles/{profile_id}/security/tlds", "tlds"
    )


@mcp.tool()
async def updatePrivacyBlocklists(profile_id: str, blocklists: str) -> dict[str, Any]:
    """Update privacy blocklists for a profile.

    Replace the entire list of enabled blocklists with the provided blocklist IDs.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        blocklists: JSON array string of blocklist IDs to enable, e.g. '["nextdns-recommended", "oisd"]'
                   Common blocklists: nextdns-recommended, energized, stevenblack,
                   oisd, notracking, adguard, etc.

    Returns:
        dict: Response containing the updated blocklists

    Example:
        result = await updatePrivacyBlocklists("abc123", '["nextdns-recommended", "oisd"]')
    """
    return await _bulk_update_helper(
        profile_id,
        blocklists,
        "/profiles/{profile_id}/privacy/blocklists",
        "blocklists",
    )


@mcp.tool()
async def updatePrivacyNatives(profile_id: str, natives: str) -> dict[str, Any]:
    """Update native tracking protection settings for a profile.

    Replace the entire list of native tracking protection features with the provided list.

    Args:
        profile_id: The profile ID (6-character alphanumeric)
        natives: JSON array string of native tracking feature IDs, e.g. '["apple", "windows", "alexa"]'
                Common features: apple (Apple device tracking), windows (Microsoft telemetry),
                alexa (Amazon Alexa), samsung, huawei, xiaomi, roku, sonos, etc.

    Returns:
        dict: Response containing the updated native tracking settings

    Example:
        result = await updatePrivacyNatives("abc123", '["apple", "windows"]')
    """
    return await _bulk_update_helper(
        profile_id, natives, "/profiles/{profile_id}/privacy/natives", "natives"
    )


if __name__ == "__main__":
    logger.info("Starting NextDNS MCP Server...")
    logger.info(f"  Base URL: {NEXTDNS_BASE_URL}")
    logger.info(f"  Timeout: {get_http_timeout()}s")

    # Determine transport mode from environment
    transport_mode = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport_mode == "http":
        # HTTP streamable transport for network services
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        logger.info(f"  Transport: HTTP streamable on {host}:{port}")
        logger.info(f"  MCP endpoint: http://{host}:{port}/mcp")
        mcp.run(transport="http", host=host, port=port)
    else:
        # Default: stdio transport for Claude Desktop, MCP Gateway, CLI
        logger.info("  Transport: stdio")
        mcp.run()
