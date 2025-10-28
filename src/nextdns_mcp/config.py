"""Configuration module for NextDNS MCP Server.

This module handles all configuration loading, validation, and constants
for the NextDNS MCP server.

SPDX-License-Identifier: MIT
"""

import logging
import os
import sys
from typing import Optional

from fastmcp.server.openapi import MCPType, RouteMap

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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
            logger.debug(f"Reading API key from file: {api_key_file}")
            with open(api_key_file, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            logger.error(f"API key file not found: {api_key_file}")
        except Exception as e:
            logger.error(f"Failed to read API key file: {e}")

    return None


# Get configuration from environment
NEXTDNS_API_KEY = get_api_key()
NEXTDNS_DEFAULT_PROFILE = os.getenv("NEXTDNS_DEFAULT_PROFILE")
NEXTDNS_BASE_URL = "https://api.nextdns.io"
NEXTDNS_HTTP_TIMEOUT = float(os.getenv("NEXTDNS_HTTP_TIMEOUT", "30"))


def validate_configuration() -> None:
    """Validate required configuration is present.

    Raises:
        SystemExit: If required configuration is missing
    """
    if not NEXTDNS_API_KEY:
        logger.critical("NEXTDNS_API_KEY is required")
        logger.critical("Set either:")
        logger.critical("  - NEXTDNS_API_KEY environment variable")
        logger.critical("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")
        sys.exit(1)


# Routes to exclude from MCP tool generation
# Routes excluded from FastMCP's auto-generation (custom implementations provided in server.py)
#
# Array-based PUT endpoints (7 routes): These require raw JSON arrays as request body,
# which FastMCP doesn't support. Custom @mcp.tool() implementations are provided that
# accept JSON strings and convert them to arrays.
#
# Unsupported endpoints (2 routes):
# - GET /analytics/domains;series: NextDNS API returns 404 (API bug)
# - GET /logs/stream: Uses Server-Sent Events (SSE), not supported by FastMCP
EXCLUDED_ROUTES = [
    # Array-based PUT endpoints (custom implementations provided)
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/denylist$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/allowlist$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/parentalControl/services$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/parentalControl/categories$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/security/tlds$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/privacy/blocklists$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["PUT"],
        pattern=r"^/profiles/\{profile_id\}/privacy/natives$",
        mcp_type=MCPType.EXCLUDE,
    ),
    # Truly unsupported endpoints
    RouteMap(
        methods=["GET"],
        pattern=r"^/profiles/\{profile_id\}/analytics/domains;series$",
        mcp_type=MCPType.EXCLUDE,
    ),
    RouteMap(
        methods=["GET"],
        pattern=r"^/profiles/\{profile_id\}/logs/stream$",
        mcp_type=MCPType.EXCLUDE,
    ),
]

# Valid DNS record types for DoH lookups
VALID_DNS_RECORD_TYPES = [
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

# DNS response status codes (RFC 1035)
DNS_STATUS_CODES = {
    0: "NOERROR - Success",
    1: "FORMERR - Format error",
    2: "SERVFAIL - Server failure",
    3: "NXDOMAIN - Non-existent domain",
    4: "NOTIMP - Not implemented",
    5: "REFUSED - Query refused",
}


# Validate configuration on module import
validate_configuration()
