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

# Get configuration from environment
NEXTDNS_API_KEY = os.getenv("NEXTDNS_API_KEY")
NEXTDNS_DEFAULT_PROFILE = os.getenv("NEXTDNS_DEFAULT_PROFILE")
NEXTDNS_BASE_URL = "https://api.nextdns.io"
NEXTDNS_HTTP_TIMEOUT = float(os.getenv("NEXTDNS_HTTP_TIMEOUT", "30"))

# Validate required configuration
if not NEXTDNS_API_KEY:
    print("ERROR: NEXTDNS_API_KEY environment variable is required", file=sys.stderr)
    print("Please set it in your .env file or environment", file=sys.stderr)
    sys.exit(1)


def load_openapi_spec() -> dict:
    """Load the NextDNS OpenAPI specification from YAML file.

    Returns:
        dict: The OpenAPI specification as a dictionary

    Raises:
        FileNotFoundError: If the OpenAPI spec file cannot be found
        yaml.YAMLError: If the YAML file is invalid
    """
    # Try to find the OpenAPI spec file
    possible_paths = [
        Path(__file__).parent.parent.parent / "nextdns-openapi.yaml",  # Development
        Path("/app/nextdns-openapi.yaml"),  # Docker container
        Path("nextdns-openapi.yaml"),  # Current directory
    ]

    for spec_path in possible_paths:
        if spec_path.exists():
            print(f"Loading OpenAPI spec from: {spec_path}", file=sys.stderr)
            with open(spec_path, "r") as f:
                return yaml.safe_load(f)

    # If we get here, we couldn't find the spec file
    raise FileNotFoundError(
        f"Could not find nextdns-openapi.yaml in any of: {[str(p) for p in possible_paths]}"
    )


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

    return httpx.AsyncClient(
        base_url=NEXTDNS_BASE_URL,
        headers=headers,
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


if __name__ == "__main__":
    print("Starting NextDNS MCP Server...", file=sys.stderr)
    print(f"  Base URL: {NEXTDNS_BASE_URL}", file=sys.stderr)
    print(f"  Timeout: {NEXTDNS_HTTP_TIMEOUT}s", file=sys.stderr)
    print("", file=sys.stderr)

    # Run the MCP server
    mcp.run()
