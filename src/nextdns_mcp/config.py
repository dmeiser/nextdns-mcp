"""Configuration module for NextDNS MCP Server.

This module handles configuration loading and validation for the NextDNS MCP
server: the API key, the API base URL, the HTTP timeout, the download size
cap, the tool-search mode, and the startup validation that ties them
together.

Two concerns used to live here and now have their own modules, re-exported
below so every existing ``nextdns_mcp.config`` import keeps resolving:

- logging setup — :mod:`nextdns_mcp.logging_config` (``configure_logging``);
- profile access control — :mod:`nextdns_mcp.profile_access` (read-only
  mode, the readable/writable/protected profile lists, and the per-request
  :class:`ProfileAccessControl` snapshot).

SPDX-License-Identifier: MIT
"""

import logging
import math
import os
from typing import Any

from .errors import ConfigurationError, MissingApiKeyError
from .logging_config import configure_logging
from .profile_access import (
    _BOOL_FALSE_TOKENS,
    _BOOL_TOKENS,
    _BOOL_TRUE_TOKENS,
    ALLOW_ALL_PROFILES,
    ProfileAccessControl,
    _combine_readable,
    _combine_set_readable,
    _is_allow_all,
    _is_empty_profile_list,
    _normalized_profile_ids,
    _parse_bool_env,
    _read_only_from_env,
    can_read_profile,
    can_write_profile,
    get_protected_profiles,
    get_readable_profiles,
    get_readable_profiles_set,
    get_writable_profiles,
    get_writable_profiles_set,
    is_read_only,
    load_profile_access_control,
    parse_profile_list,
)

__all__ = [
    "ALLOW_ALL_PROFILES",
    "DEFAULT_API_BASE",
    "DEFAULT_DOWNLOAD_MAX_BYTES",
    "DEFAULT_HTTP_TIMEOUT",
    "DEFAULT_TOOL_SEARCH_MODE",
    "NEXTDNS_BASE_URL",
    "TOOL_SEARCH_MODES",
    "_BOOL_FALSE_TOKENS",
    "_BOOL_TOKENS",
    "_BOOL_TRUE_TOKENS",
    "ConfigurationError",
    "MissingApiKeyError",
    "ProfileAccessControl",
    "_combine_readable",
    "_combine_set_readable",
    "_is_allow_all",
    "_is_empty_profile_list",
    "_normalized_profile_ids",
    "_parse_bool_env",
    "_read_only_from_env",
    "can_read_profile",
    "can_write_profile",
    "configure_logging",
    "get_api_key",
    "get_default_profile",
    "get_download_max_bytes",
    "get_http_timeout",
    "get_protected_profiles",
    "get_readable_profiles",
    "get_readable_profiles_set",
    "get_tool_search_mode",
    "get_writable_profiles",
    "get_writable_profiles_set",
    "is_generative_ui_enabled",
    "is_read_only",
    "load_profile_access_control",
    "parse_profile_list",
    "validate_configuration",
]

logger = logging.getLogger(__name__)


# Core API configuration. Overridable via NEXTDNS_API_BASE - the name the
# harness and container-E2E scripts already pass into the server's
# environment; before this the override reached the child process and was
# simply ignored, so a custom base configured the harness's REST checks but
# not the MCP server under test. Read once at import: the ACL client's
# base_url, the DoH allowlist host and the startup banner all resolve from
# this constant, so the override configures every one of them.
DEFAULT_API_BASE = "https://api.nextdns.io"
NEXTDNS_BASE_URL = os.getenv("NEXTDNS_API_BASE", DEFAULT_API_BASE).rstrip("/")

# Default HTTP request timeout in seconds
DEFAULT_HTTP_TIMEOUT: float = 30.0

# Default hard cap on the total bytes a log download may stream to disk, in
# bytes. The cap is enforced while streaming so a single download cannot fill
# the OS temp directory. Overridable via NEXTDNS_DOWNLOAD_MAX_BYTES.
DEFAULT_DOWNLOAD_MAX_BYTES: int = 1024 * 1024 * 1024  # 1 GiB


def get_api_key() -> str | None:
    """Get API key from environment."""
    key = os.getenv("NEXTDNS_API_KEY")
    if key:
        return key.strip()

    # Fall back to a file (Docker secret)
    key_file = os.getenv("NEXTDNS_API_KEY_FILE")
    if key_file:
        try:
            logger.debug(f"Reading API key from file: {key_file}")
            with open(key_file, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            logger.error(f"API key file not found: {key_file}")
        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read API key file: {e}")

    return None


def get_http_timeout() -> float:
    """Get HTTP timeout from environment.

    Returns:
        float: Configured HTTP timeout in seconds (default 30.0).

    Raises:
        ConfigurationError: If NEXTDNS_HTTP_TIMEOUT is not a positive finite number.
    """
    raw = os.getenv("NEXTDNS_HTTP_TIMEOUT")
    if raw is None:
        return DEFAULT_HTTP_TIMEOUT

    try:
        val = float(raw)
        if not math.isfinite(val) or val <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError(
            f"Invalid NEXTDNS_HTTP_TIMEOUT: {raw!r}. Expected a positive number of seconds."
        ) from None

    return val


def get_download_max_bytes() -> int:
    """Get the total download size cap in bytes.

    The cap is read from the ``NEXTDNS_DOWNLOAD_MAX_BYTES`` environment
    variable; see :data:`DEFAULT_DOWNLOAD_MAX_BYTES` for the default.

    Returns:
        int: Configured maximum bytes a log download may stream to disk
            (default 1 GiB).

    Raises:
        ConfigurationError: If NEXTDNS_DOWNLOAD_MAX_BYTES is not a positive integer.
    """
    raw = os.getenv("NEXTDNS_DOWNLOAD_MAX_BYTES")
    if raw is None:
        return DEFAULT_DOWNLOAD_MAX_BYTES

    try:
        val = int(raw)
        if val <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError(
            f"Invalid NEXTDNS_DOWNLOAD_MAX_BYTES: {raw!r}. Expected a positive integer of bytes."
        ) from None

    return val


def get_default_profile() -> str | None:
    """Get default profile from environment."""
    return os.getenv("NEXTDNS_DEFAULT_PROFILE")


def is_generative_ui_enabled() -> bool:
    """Check if Generative UI (on-the-fly Prefab UI generation) is enabled.

    Opt-in beta feature (disabled by default). Can be enabled by setting
    NEXTDNS_GENERATIVE_UI to 'true', '1', 'on', or 'yes'.
    """
    return _parse_bool_env(os.getenv("NEXTDNS_GENERATIVE_UI", "off")) is True


# Tool-search modes accepted by NEXTDNS_TOOL_SEARCH. "bm25" (the default)
# replaces the listing with FastMCP's synthetic search_tools/call_tool pair
# (progressive disclosure); "off" advertises the full tool catalog. FastMCP
# also offers a regex transform; it was dropped before shipping because it
# ranks nothing (registration order within a pattern match) and none of the
# live model validations that shaped this feature ran under it.
TOOL_SEARCH_MODES: tuple[str, ...] = ("off", "bm25")

# What an unset (or empty) NEXTDNS_TOOL_SEARCH resolves to. bm25 is what
# every live-model validation of this contract ran under, so the full
# 80-tool listing is the explicit opt-out rather than the default everyone
# trips over.
DEFAULT_TOOL_SEARCH_MODE = "bm25"


def get_tool_search_mode() -> str:
    """Get the tool-search (progressive disclosure) mode from the environment.

    Reads ``NEXTDNS_TOOL_SEARCH``: ``"bm25"`` (the default) enables FastMCP's
    search transform, which hides the real tool catalog behind a
    ``search_tools`` + ``call_tool`` pair; ``"off"`` keeps the normal full
    listing. Unset or empty means ``"bm25"``.

    Returns:
        str: One of ``"off"`` or ``"bm25"`` (default ``"bm25"``).

    Raises:
        ConfigurationError: If NEXTDNS_TOOL_SEARCH is not one of the accepted
            values.
    """
    raw = os.getenv("NEXTDNS_TOOL_SEARCH", "").strip().lower()
    if not raw:
        return DEFAULT_TOOL_SEARCH_MODE
    if raw not in TOOL_SEARCH_MODES:
        raise ConfigurationError(
            f"Invalid NEXTDNS_TOOL_SEARCH: {raw!r}. Expected one of: {', '.join(TOOL_SEARCH_MODES)}."
        )
    return raw


def _log_api_key_error() -> None:
    """Log error message for missing API key."""
    logger.critical("NEXTDNS_API_KEY is required")
    logger.critical("Set either:")
    logger.critical("  - NEXTDNS_API_KEY environment variable")
    logger.critical("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")


def _log_profile_access(profile_set: set[str] | None, access_type: str) -> None:
    """Log profile access configuration."""
    if profile_set is None:
        logger.info(f"No profiles are {access_type} (deny all by default)")
    elif not profile_set:
        logger.info(f"All profiles are {access_type} (no restrictions)")
    else:
        logger.info(f"{access_type.capitalize()} profiles restricted to: {sorted(profile_set)}")


def _log_protected_profiles(access: ProfileAccessControl) -> None:
    """Say at startup which profiles the write denylist actually holds.

    The denylist is the operator's safety net for ``WRITABLE=ALL`` with a
    production profile on the same key; naming its members means a typo
    cannot sit silently unprotected. The entries are shape-validated by
    ``get_protected_profiles`` before this line is ever reached (see
    ``validate_configuration``).
    """
    if access.protected:
        logger.info(f"Protected profiles (writes denied even under ALL): {', '.join(sorted(access.protected))}")


def _log_access_control_settings() -> None:
    """Log current access control configuration."""
    access = load_profile_access_control()
    readable = None if access.readable is None else set(access.readable)
    writable = None if access.writable is None else set(access.writable)
    read_only = access.read_only

    if read_only:
        logger.info("Read-only mode is ENABLED - all write operations are disabled")

    _log_profile_access(readable, "readable")
    if not read_only:
        _log_profile_access(writable, "writable")
    _log_protected_profiles(access)


def validate_configuration() -> None:
    """Validate required configuration is present and valid.

    Raises:
        MissingApiKeyError: If no API key is configured. This is a
            ``ConfigurationError`` subclass, so a single
            ``except ConfigurationError`` also covers the invalid-value cases
            below.
        ConfigurationError: If configuration values are invalid.
    """
    if not get_api_key():
        _log_api_key_error()
        raise MissingApiKeyError(
            "NEXTDNS_API_KEY is required. Set the NEXTDNS_API_KEY environment "
            "variable or NEXTDNS_API_KEY_FILE pointing to a Docker secret."
        )

    get_http_timeout()
    get_download_max_bytes()
    base_url = os.getenv("NEXTDNS_API_BASE", DEFAULT_API_BASE).rstrip("/")
    if not base_url.lower().startswith("https://"):
        raise ConfigurationError("NEXTDNS_API_BASE must use https://")
    for env_name, default in (
        ("NEXTDNS_READ_ONLY", ""),
        ("NEXTDNS_GENERATIVE_UI", "off"),
    ):
        raw = os.getenv(env_name, default)
        if _parse_bool_env(raw) is None:
            raise ConfigurationError(f"Invalid {env_name}: {raw!r}. Expected one of {sorted(_BOOL_TOKENS - {''})}.")

    _log_access_control_settings()


def __getattr__(name: str) -> Any:
    if name in {"DNS_STATUS_CODES", "VALID_DNS_RECORD_TYPES"}:
        from .tools.doh import DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES

        return {"DNS_STATUS_CODES": DNS_STATUS_CODES, "VALID_DNS_RECORD_TYPES": VALID_DNS_RECORD_TYPES}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
