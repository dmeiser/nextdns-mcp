"""NextDNS MCP Server - FastMCP-based implementation using OpenAPI spec."""

import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP

# Load environment variables from .env file
load_dotenv()


def get_api_key() -> Optional[str]:
    """Get API key from environment or Docker secret file.

    Checks in order:
    1. NEXTDNS_API_KEY environment variable
    2. NEXTDNS_API_KEY_FILE environment variable pointing to a secret file

    Returns:
        str: The API key if found, None otherwise
    """
    # Check direct environment variable first
    api_key = os.getenv("NEXTDNS_API_KEY")
    if api_key:
        return api_key

    # Check for Docker secret file
    api_key_file = os.getenv("NEXTDNS_API_KEY_FILE")
    if api_key_file:
        try:
            with open(api_key_file, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            print(f"ERROR: API key file not found: {api_key_file}", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: Failed to read API key file: {e}", file=sys.stderr)

    return None


# Get configuration from environment
NEXTDNS_API_KEY = get_api_key()
NEXTDNS_DEFAULT_PROFILE = os.getenv("NEXTDNS_DEFAULT_PROFILE")
NEXTDNS_BASE_URL = "https://api.nextdns.io"
NEXTDNS_HTTP_TIMEOUT = float(os.getenv("NEXTDNS_HTTP_TIMEOUT", "30"))

# Validate required configuration
if not NEXTDNS_API_KEY:
    print("ERROR: NEXTDNS_API_KEY is required", file=sys.stderr)
    print("Set either:", file=sys.stderr)
    print("  - NEXTDNS_API_KEY environment variable", file=sys.stderr)
    print("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret", file=sys.stderr)
    sys.exit(1)


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
        print(f"ERROR: OpenAPI spec not found at: {spec_path}", file=sys.stderr)
        print("The nextdns-openapi.yaml file must be in the package directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading OpenAPI spec from: {spec_path}", file=sys.stderr)
    with open(spec_path, "r") as f:
        spec = yaml.safe_load(f)

    # Filter out operations marked with x-fastmcp-generate: false
    # FastMCP doesn't natively support this, so we filter programmatically
    if "paths" in spec:
        for path, path_item in list(spec["paths"].items()):
            for method, operation in list(path_item.items()):
                if isinstance(operation, dict) and operation.get("x-fastmcp-generate") is False:
                    operation_id = operation.get("operationId", f"{method.upper()} {path}")
                    print(
                        f"  Excluding operation: {operation_id} (x-fastmcp-generate: false)",
                        file=sys.stderr,
                    )
                    del path_item[method]

            # Remove path entirely if no methods remain
            if not any(k in path_item for k in ["get", "post", "put", "patch", "delete"]):
                del spec["paths"][path]

    return spec


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
    print("Loading NextDNS OpenAPI specification...", file=sys.stderr)
    openapi_spec = load_openapi_spec()

    # Create authenticated HTTP client
    print(f"Creating HTTP client for {NEXTDNS_BASE_URL}...", file=sys.stderr)
    api_client = create_nextdns_client()

    # Create MCP server from OpenAPI spec
    print("Generating MCP server from OpenAPI specification...", file=sys.stderr)
    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=api_client,
        name="NextDNS MCP Server",
        timeout=NEXTDNS_HTTP_TIMEOUT,
    )

    # Add metadata about the server
    print(f"✓ MCP server created successfully", file=sys.stderr)
    if NEXTDNS_DEFAULT_PROFILE:
        print(f"  Default profile: {NEXTDNS_DEFAULT_PROFILE}", file=sys.stderr)

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
    valid_types = [
        "A",
        "AAAA",
        "CNAME",
        "MX",
        "NS",
        "PTR",
        "SOA",
        "TXT",
        "SRV",
        "CAA",
        "DNSKEY",
        "DS",
    ]
    record_type_upper = record_type.upper()
    if record_type_upper not in valid_types:
        return {"error": f"Invalid record type: {record_type}", "valid_types": valid_types}

    # Construct DoH query URL
    doh_url = f"https://dns.nextdns.io/{target_profile}/dns-query"
    params = {"name": domain, "type": record_type_upper}
    headers = {"accept": "application/dns-json"}

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

            # Add human-readable status
            status_codes = {
                0: "NOERROR - Success",
                1: "FORMERR - Format error",
                2: "SERVFAIL - Server failure",
                3: "NXDOMAIN - Non-existent domain",
                4: "NOTIMP - Not implemented",
                5: "REFUSED - Query refused",
            }
            if "Status" in result:
                result["_metadata"]["status_description"] = status_codes.get(
                    result["Status"], f"Unknown status code: {result['Status']}"
                )

            return result

    except httpx.HTTPError as e:
        return {
            "error": f"HTTP error during DoH lookup: {str(e)}",
            "profile_id": target_profile,
            "domain": domain,
            "type": record_type_upper,
        }
    except Exception as e:
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


if __name__ == "__main__":
    print("Starting NextDNS MCP Server...", file=sys.stderr)
    print(f"  Base URL: {NEXTDNS_BASE_URL}", file=sys.stderr)
    print(f"  Timeout: {NEXTDNS_HTTP_TIMEOUT}s", file=sys.stderr)
    print("", file=sys.stderr)

    # Run the MCP server
    mcp.run()
