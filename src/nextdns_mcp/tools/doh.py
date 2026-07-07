"""DNS-over-HTTPS lookup tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
from typing import Any

import httpx

from ..coercion import OptionalProfileId
from ..config import DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES, can_read_profile, get_default_profile, get_http_timeout
from ..utils import is_safe_profile_id

logger = logging.getLogger(__name__)


def _get_target_profile(profile_id: str | None) -> str | None:
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

    if not is_safe_profile_id(target_profile):
        return {"error": f"Invalid profile_id format: {target_profile}"}

    if not can_read_profile(target_profile):
        return {"error": f"Read access denied for profile: {target_profile}"}

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
