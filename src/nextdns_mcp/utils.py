"""Shared validation and API request utilities for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import logging
import re
from typing import Any

import httpx

from . import client

logger = logging.getLogger(__name__)

# Safe identifier patterns to prevent path traversal and ACL bypass.
SAFE_PROFILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
SAFE_ENTRY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")


def is_safe_profile_id(value: str | int) -> bool:
    """Return True if value is a safe profile_id segment."""
    return bool(SAFE_PROFILE_ID_PATTERN.match(str(value)))


def is_safe_entry_id(value: str) -> bool:
    """Return True if value is a safe entry_id segment (allows domain-like IDs).

    Rejects path separators and parent-directory sequences that could be used
    for path traversal when the ID is embedded in a URL path.
    """
    if not value or "/" in value or "\\" in value or ".." in value:
        return False
    return bool(SAFE_ENTRY_ID_PATTERN.match(value))


def _validate_profile_id(profile_id: str | int) -> dict[str, Any] | None:
    """Return an error dict if profile_id is not a safe identifier."""
    if not is_safe_profile_id(profile_id):
        return {"error": f"Invalid profile_id format: {profile_id}"}
    return None


def _validate_entry_id(entry_id: str) -> dict[str, Any] | None:
    """Return an error dict if entry_id is not a safe identifier."""
    if not is_safe_entry_id(entry_id):
        return {"error": f"Invalid entry_id format: {entry_id}"}
    return None


def _build_query_params(**kwargs: Any) -> dict[str, Any]:
    """Build a query-param dict, dropping None values and normalizing booleans."""
    params = {k: v for k, v in kwargs.items() if v is not None}
    return {k: ("true" if v else "false") if isinstance(v, bool) else v for k, v in params.items()}


async def _api_request(method: str, url: str, params: dict[str, Any] | None = None, json: Any = None) -> dict[str, Any]:
    """Make an HTTP request through the access-controlled client and return JSON."""
    try:
        response = await client.api_client.request(method, url, params=params, json=json)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"success": True}
        return response.json()
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in {method} {url}: {e}")
        error_response = getattr(e, "response", None)
        status_code = error_response.status_code if error_response is not None else None
        body = error_response.text if error_response is not None else None
        raise RuntimeError(f"HTTP error {status_code} in {method} {url}: {e} (response: {body})") from e
    except Exception as e:
        logger.error(f"Unexpected error in {method} {url}: {e}")
        raise RuntimeError(f"Unexpected error in {method} {url}: {e}") from e
