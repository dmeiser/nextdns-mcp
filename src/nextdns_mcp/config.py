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
        return api_key.strip()

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

# Profile access control configuration
# Comma-separated list of profile IDs that are allowed to be read
# Empty or not set = all profiles can be read
NEXTDNS_READABLE_PROFILES = os.getenv("NEXTDNS_READABLE_PROFILES", "")
# Comma-separated list of profile IDs that are allowed to be written to
# Empty or not set = all profiles can be written to (unless read-only mode is enabled)
# Write access implies read access
NEXTDNS_WRITABLE_PROFILES = os.getenv("NEXTDNS_WRITABLE_PROFILES", "")
# Read-only mode: disables all write operations regardless of profile access
NEXTDNS_READ_ONLY = os.getenv("NEXTDNS_READ_ONLY", "").lower() in ("true", "1", "yes")

# Operations that are globally allowed and don't require profile-specific access checks
# These operations either don't take a profile_id parameter or are essential for discovery
GLOBALLY_ALLOWED_OPERATIONS = {
    "listProfiles",  # Required to discover available profiles
    "dohLookup",  # Custom DoH lookup tool
}


def parse_profile_list(profile_str: str) -> set[str]:
    """Parse a comma-separated list of profile IDs.
    
    Args:
        profile_str: Comma-separated string of profile IDs
        
    Returns:
        Set of profile IDs (empty set if string is empty)
    """
    if not profile_str or not profile_str.strip():
        return set()
    return {p.strip() for p in profile_str.split(",") if p.strip()}


def get_readable_profiles() -> set[str]:
    """Get the set of profiles that are allowed to be read.
    
    Returns:
        Set of profile IDs. Empty set means all profiles are readable.
    """
    readable = parse_profile_list(NEXTDNS_READABLE_PROFILES)
    writable = parse_profile_list(NEXTDNS_WRITABLE_PROFILES)
    # Write access implies read access - but only if readable is explicitly set
    # If readable is empty (all profiles readable), keep it empty
    if readable and writable:
        readable = readable | writable
    elif not readable and not writable:
        # Both empty = all profiles accessible
        return set()
    elif not readable and writable:
        # Empty readable but writable set = all profiles readable (writable is subset)
        return set()
    # readable is set, writable is empty
    return readable


def get_writable_profiles() -> set[str]:
    """Get the set of profiles that are allowed to be written to.
    
    Returns:
        Set of profile IDs. Empty set means all profiles are writable (unless read-only mode).
    """
    if NEXTDNS_READ_ONLY:
        return set()  # No profiles writable in read-only mode
    return parse_profile_list(NEXTDNS_WRITABLE_PROFILES)


def can_read_profile(profile_id: str) -> bool:
    """Check if a profile can be read.
    
    Args:
        profile_id: The profile ID to check
        
    Returns:
        True if the profile can be read, False otherwise
    """
    readable = get_readable_profiles()
    # Empty set means all profiles are readable
    return not readable or profile_id in readable


def can_write_profile(profile_id: str) -> bool:
    """Check if a profile can be written to.
    
    Args:
        profile_id: The profile ID to check
        
    Returns:
        True if the profile can be written to, False otherwise
    """
    if NEXTDNS_READ_ONLY:
        return False
    writable = get_writable_profiles()
    # Empty set means all profiles are writable (unless read-only)
    return not writable or profile_id in writable


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
    
    # Log profile access control settings if configured
    readable = get_readable_profiles()
    writable = get_writable_profiles()
    
    if NEXTDNS_READ_ONLY:
        logger.info("Read-only mode is ENABLED - all write operations are disabled")
    
    if readable:
        logger.info(f"Readable profiles restricted to: {sorted(readable)}")
    else:
        logger.info("All profiles are readable (no restrictions)")
    
    if writable:
        logger.info(f"Writable profiles restricted to: {sorted(writable)}")
    elif not NEXTDNS_READ_ONLY:
        logger.info("All profiles are writable (no restrictions)")


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
