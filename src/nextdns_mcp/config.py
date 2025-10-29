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

# Core API configuration
NEXTDNS_BASE_URL = "https://api.nextdns.io"

# Operations that bypass profile access control
GLOBALLY_ALLOWED_OPERATIONS = {
    "listProfiles",  # Required to discover available profiles
    "dohLookup",  # Custom DoH lookup tool
}


def get_api_key() -> Optional[str]:
    """Get API key from environment."""
    key = os.getenv("NEXTDNS_API_KEY")
    if key:
        return key.strip()

    key_file = os.getenv("NEXTDNS_API_KEY_FILE")
    if key_file:
        try:
            logger.debug(f"Reading API key from file: {key_file}")
            with open(key_file, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            logger.error(f"API key file not found: {key_file}")
        except Exception as e:
            logger.error(f"Failed to read API key file: {e}")

    return None


def get_http_timeout() -> float:
    """Get HTTP timeout from environment."""
    return float(os.getenv("NEXTDNS_HTTP_TIMEOUT", "30"))


def get_default_profile() -> Optional[str]:
    """Get default profile from environment."""
    return os.getenv("NEXTDNS_DEFAULT_PROFILE")


def get_readable_profiles() -> set[str]:
    """Get readable profile list from environment."""
    profiles = os.getenv("NEXTDNS_READABLE_PROFILES", "")
    return parse_profile_list(profiles)


def get_writable_profiles() -> set[str]:
    """Get writable profile list from environment."""
    if is_read_only():
        return set()
    profiles = os.getenv("NEXTDNS_WRITABLE_PROFILES", "")
    return parse_profile_list(profiles)


def is_read_only() -> bool:
    """Check if read-only mode is enabled."""
    value = os.getenv("NEXTDNS_READ_ONLY", "").lower()
    return value in ("true", "1", "yes")


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


def get_readable_profiles_set() -> set[str]:
    """Get the set of profiles that are allowed to be read.
    
    Returns:
        Set of profile IDs. Empty set means all profiles are readable.
    """
    readable = get_readable_profiles()  # Get from env
    writable = get_writable_profiles()  # Get from env

    # If readable is empty, all profiles are readable
    if not readable:
        return set()

    # If readable is set, combine with writable (write implies read)
    return readable | writable


def get_writable_profiles_set() -> set[str]:
    """Get the set of profiles that are allowed to be written to.

    Returns:
        Set of profile IDs. Empty set means all profiles are writable (unless read-only mode).
    """
    if is_read_only():
        return set()
    return get_writable_profiles()  # Already checked read-only flag


def can_read_profile(profile_id: str) -> bool:
    """Check if a profile can be read.
    
    Args:
        profile_id: The profile ID to check

    Returns:
        True if the profile can be read, False otherwise
    """
    readable = get_readable_profiles_set()
    # Empty set means all profiles are readable
    return not readable or profile_id in readable


def can_write_profile(profile_id: str) -> bool:
    """Check if a profile can be written to.
    
    Args:
        profile_id: The profile ID to check

    Returns:
        True if the profile can be written to, False otherwise
    """
    if is_read_only():
        return False
    writable = get_writable_profiles_set()
    # Empty set means all profiles are writable (unless read-only)
    return not writable or profile_id in writable


def _log_api_key_error():
    """Log error message for missing API key."""
    logger.critical("NEXTDNS_API_KEY is required")
    logger.critical("Set either:")
    logger.critical("  - NEXTDNS_API_KEY environment variable")
    logger.critical("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")


def _log_access_control_settings():
    """Log current access control configuration."""
    readable = get_readable_profiles_set()
    writable = get_writable_profiles_set()

    if is_read_only():
        logger.info("Read-only mode is ENABLED - all write operations are disabled")

    if readable:
        logger.info(f"Readable profiles restricted to: {sorted(readable)}")
    else:
        logger.info("All profiles are readable (no restrictions)")

    if writable:
        logger.info(f"Writable profiles restricted to: {sorted(writable)}")
    elif not is_read_only():
        logger.info("All profiles are writable (no restrictions)")


def validate_configuration() -> None:
    """Validate required configuration is present.

    Raises:
        SystemExit: If required configuration is missing
    """
    if not get_api_key():
        _log_api_key_error()
        sys.exit(1)

    _log_access_control_settings()


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



