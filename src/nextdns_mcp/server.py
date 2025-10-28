"""NextDNS MCP Server - FastMCP-based implementation using OpenAPI spec.

SPDX-License-Identifier: MIT
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.openapi import DEFAULT_ROUTE_MAPPINGS, RouteMap

from .config import (
    DNS_STATUS_CODES,
    EXCLUDED_ROUTES,
    NEXTDNS_API_KEY,
    NEXTDNS_BASE_URL,
    NEXTDNS_DEFAULT_PROFILE,
    NEXTDNS_HTTP_TIMEOUT,
    VALID_DNS_RECORD_TYPES,
)

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


def load_openapi_spec() -> dict:
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
        spec = yaml.safe_load(f)

    return spec


def build_route_mappings() -> list[RouteMap]:
    """Create the RouteMap list used for OpenAPI conversion.

    Combines excluded routes with default route mappings.

    Returns:
        list[RouteMap]: Complete list of route mappings for MCP tool generation
    """
    return [*EXCLUDED_ROUTES, *DEFAULT_ROUTE_MAPPINGS]


def create_nextdns_client() -> httpx.AsyncClient:
    """Create an authenticated HTTP client for NextDNS API.

    Returns:
        httpx.AsyncClient: Configured async HTTP client with authentication
    """
    headers = {
        "X-Api-Key": NEXTDNS_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # Remove any headers that weren't set to actual values to satisfy type checkers
    clean_headers = {key: value for key, value in headers.items() if value is not None}

    return httpx.AsyncClient(
        base_url=NEXTDNS_BASE_URL,
        headers=clean_headers,
        timeout=NEXTDNS_HTTP_TIMEOUT,
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
        timeout=NEXTDNS_HTTP_TIMEOUT,
    )

    # Add metadata about the server
    logger.info("MCP server created successfully")
    if NEXTDNS_DEFAULT_PROFILE:
        logger.info(f"Default profile: {NEXTDNS_DEFAULT_PROFILE}")

    return mcp


# Create the MCP server instance
mcp = create_mcp_server()


# Add custom DoH lookup tool
async def _dohLookup_impl(
    domain: str, profile_id: Optional[str] = None, record_type: str = "A"
) -> dict:
    """Implementation of DoH lookup functionality.

    Perform a DNS-over-HTTPS lookup using a NextDNS profile.

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
    # Use default profile if not specified
    target_profile = profile_id
    if not target_profile:
        # Re-read environment in case tests or callers override it after import
        env_default = os.getenv("NEXTDNS_DEFAULT_PROFILE")
        if env_default:
            target_profile = env_default
        else:
            target_profile = NEXTDNS_DEFAULT_PROFILE

    if not target_profile:
        return {
            "error": "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set",
            "hint": "Provide profile_id parameter or set NEXTDNS_DEFAULT_PROFILE environment variable",
        }

    # Validate record type
    record_type_upper = record_type.upper()
    if record_type_upper not in VALID_DNS_RECORD_TYPES:
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
        async with httpx.AsyncClient(timeout=NEXTDNS_HTTP_TIMEOUT) as client:
            response = await client.get(doh_url, params=params, headers=headers)
            response.raise_for_status()

            result = response.json()

            # Add helpful metadata
            result["_metadata"] = {
                "profile_id": target_profile,
                "query_domain": domain,
                "query_type": record_type_upper,
                "doh_endpoint": f"{doh_url}?name={domain}&type={record_type_upper}",
            }

            # Add human-readable status description
            if "Status" in result:
                status_desc = DNS_STATUS_CODES.get(
                    result["Status"], f"Unknown status code: {result['Status']}"
                )
                result["_metadata"]["status_description"] = status_desc
                logger.debug(f"DoH lookup result: {domain} -> {status_desc}")

            return result

    except httpx.HTTPError as e:
        logger.error(f"HTTP error during DoH lookup for {domain}: {str(e)}")
        return {
            "error": f"HTTP error during DoH lookup: {str(e)}",
            "profile_id": target_profile,
            "domain": domain,
            "type": record_type_upper,
        }
    except Exception as e:
        logger.error(f"Unexpected error during DoH lookup for {domain}: {str(e)}")
        return {
            "error": f"Unexpected error during DoH lookup: {str(e)}",
            "profile_id": target_profile,
            "domain": domain,
            "type": record_type_upper,
        }


# Register the DoH lookup tool with MCP
@mcp.tool()
async def dohLookup(domain: str, profile_id: Optional[str] = None, record_type: str = "A") -> dict:
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
) -> dict:
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
    import json

    logger.info(f"Bulk update: {param_name} for profile {profile_id}")

    # Parse and validate JSON array
    try:
        array_data = json.loads(data)
        if not isinstance(array_data, list):
            logger.warning(f"Invalid {param_name} format: expected array, got {type(array_data).__name__}")
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
        return response.json()
    except httpx.HTTPError as e:
        logger.error(f"HTTP error during bulk update: {str(e)}")
        return {"error": f"HTTP error: {str(e)}"}


@mcp.tool()
async def updateDenylist(profile_id: str, entries: str) -> dict:
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
async def updateAllowlist(profile_id: str, entries: str) -> dict:
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
async def updateParentalControlServices(profile_id: str, services: str) -> dict:
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
async def updateParentalControlCategories(profile_id: str, categories: str) -> dict:
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
async def updateSecurityTlds(profile_id: str, tlds: str) -> dict:
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
async def updatePrivacyBlocklists(profile_id: str, blocklists: str) -> dict:
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
async def updatePrivacyNatives(profile_id: str, natives: str) -> dict:
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
    logger.info(f"  Timeout: {NEXTDNS_HTTP_TIMEOUT}s")

    # Run the MCP server
    mcp.run()
