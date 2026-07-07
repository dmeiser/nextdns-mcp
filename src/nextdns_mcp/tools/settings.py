"""Grouped settings management tool for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Any, Literal, Optional, Union

from ..coercion import ProfileId, _coerce_json_arg
from ..utils import _api_request, _validate_profile_id

# Grouped-tool literal type aliases exposed to FastMCP for nice schemas.
SettingsCategory = Literal["general", "privacy", "security", "parental", "performance", "logs", "blockpage"]

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


async def _manage_settings_impl(
    operation: Literal["get", "update"],
    category: SettingsCategory,
    profile_id: ProfileId,
    settings: Optional[Union[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Grouped CRUD implementation for profile settings categories."""
    error = _validate_profile_id(profile_id)
    if error:
        return error

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


async def manageSettings(
    operation: Literal["get", "update"],
    category: SettingsCategory,
    profile_id: ProfileId,
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
