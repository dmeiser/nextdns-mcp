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
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# DNS_STATUS_CODES and VALID_DNS_RECORD_TYPES are re-exported here: callers
# already read them off this module, and the definitions live in
# ``constants`` so the tool layer owns nothing that the base layer republishes.
# The import is eager rather than lazy because ``constants`` imports nothing
# from this package, so this edge cannot close a cycle.
from .constants import DNS_STATUS_CODES, SAFE_PROFILE_ID_PATTERN, VALID_DNS_RECORD_TYPES
from .errors import ConfigurationError, MissingApiKeyError
from .logging_config import configure_logging
from .profile_access import _BOOL_FALSE_TOKENS as _BOOL_FALSE_TOKENS  # noqa: PLC0414
from .profile_access import (
    _BOOL_TOKENS,
    ALLOW_ALL_PROFILES,
    ProfileAccessControl,
    _parse_bool_env,
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
from .profile_access import _BOOL_TRUE_TOKENS as _BOOL_TRUE_TOKENS  # noqa: PLC0414
from .profile_access import BOOL_FALSE_TOKENS as BOOL_FALSE_TOKENS  # noqa: PLC0414
from .profile_access import BOOL_TRUE_TOKENS as BOOL_TRUE_TOKENS  # noqa: PLC0414
from .profile_access import _combine_readable as _combine_readable  # noqa: PLC0414
from .profile_access import _combine_set_readable as _combine_set_readable  # noqa: PLC0414
from .profile_access import _is_allow_all as _is_allow_all  # noqa: PLC0414
from .profile_access import _is_empty_profile_list as _is_empty_profile_list  # noqa: PLC0414
from .profile_access import _mutable_profile_set as _mutable_profile_set  # noqa: PLC0414
from .profile_access import _normalized_profile_ids as _normalized_profile_ids  # noqa: PLC0414
from .profile_access import _profile_ids_env as _profile_ids_env  # noqa: PLC0414
from .profile_access import _profile_list_env as _profile_list_env  # noqa: PLC0414
from .profile_access import _read_only_from_env as _read_only_from_env  # noqa: PLC0414

# The pre-split module had no __all__, so star imports bound every public
# attribute - including the stdlib names, ``dataclass`` and
# ``SAFE_PROFILE_ID_PATTERN`` it imported - and no underscore-prefixed
# helper. ``__all__`` pins exactly that surface: the private helpers above
# stay importable by name but stay out of star imports, and the incidental
# re-exports (``dataclass``, ``SAFE_PROFILE_ID_PATTERN``) remain importable
# from here as they were before the split.
__all__ = [
    "ALLOW_ALL_PROFILES",
    "DEFAULT_API_BASE",
    "DEFAULT_DOWNLOAD_MAX_BYTES",
    "DEFAULT_HTTP_TIMEOUT",
    "DEFAULT_TOOL_SEARCH_MODE",
    "DNS_STATUS_CODES",
    "SAFE_PROFILE_ID_PATTERN",
    "TOOL_SEARCH_MODES",
    "VALID_DNS_RECORD_TYPES",
    "Any",
    "Collection",
    "ConfigurationError",
    "MissingApiKeyError",
    "ProfileAccessControl",
    "ServerConfig",
    "can_read_profile",
    "can_write_profile",
    "configure_logging",
    "dataclass",
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
    "logger",
    "logging",
    "math",
    "os",
    "parse_profile_list",
    "validate_configuration",
]

logger = logging.getLogger(__name__)


# Core API configuration. Overridable via NEXTDNS_API_BASE - the name the
# harness and container-E2E scripts already pass into the server's
# environment; before this the override reached the child process and was
# simply ignored, so a custom base configured the harness's REST checks but
# not the MCP server under test. Read at use time through get_api_base(),
# matching the harness's api_base() convention: the ACL client's base_url,
# the destination allow-list host and the startup banner all resolve through
# the getter, so a long-lived process that changes NEXTDNS_API_BASE picks the
# new endpoint up on the next client creation without a reimport or restart.
DEFAULT_API_BASE = "https://api.nextdns.io"

# Default HTTP request timeout in seconds
DEFAULT_HTTP_TIMEOUT: float = 30.0

# Default hard cap on the total bytes a log download may stream to disk, in
# bytes. The cap is enforced while streaming so a single download cannot fill
# the OS temp directory. Overridable via NEXTDNS_DOWNLOAD_MAX_BYTES.
DEFAULT_DOWNLOAD_MAX_BYTES: int = 1024 * 1024 * 1024  # 1 GiB


def _effective_environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the caller's snapshot when supplied, otherwise the process environment."""
    return os.environ if environ is None else environ


def get_api_base(environ: Mapping[str, str] | None = None) -> str:
    """Get the API base URL from ``environ`` (the process environment by default).

    Passing the mapping a server snapshotted at creation is what keeps its
    base URL stable while the process environment moves on.

    Returns:
        str: The configured base URL with any trailing slash removed
            (default ``https://api.nextdns.io``).

    Raises:
        ConfigurationError: If NEXTDNS_API_BASE does not use https://. The
            scheme check runs on every read, not only at startup validation,
            so a runtime change cannot silently redirect the API key to a
            plaintext endpoint.
    """
    env = _effective_environ(environ)
    base_url = env.get("NEXTDNS_API_BASE", DEFAULT_API_BASE).rstrip("/")
    if not base_url.lower().startswith("https://"):
        raise ConfigurationError("NEXTDNS_API_BASE must use https://")
    return base_url


def get_api_key(environ: Mapping[str, str] | None = None) -> str | None:
    """Get API key from ``environ`` (the process environment by default).

    The ``NEXTDNS_API_KEY_FILE`` fallback is unchanged: a Docker secret is
    still read from the path the environment names.
    """
    env = _effective_environ(environ)
    key = env.get("NEXTDNS_API_KEY")
    if key:
        return key.strip()

    # Fall back to a file (Docker secret)
    key_file = env.get("NEXTDNS_API_KEY_FILE")
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


def get_http_timeout(environ: Mapping[str, str] | None = None) -> float:
    """Get HTTP timeout from ``environ`` (the process environment by default).

    Returns:
        float: Configured HTTP timeout in seconds (default 30.0).

    Raises:
        ConfigurationError: If NEXTDNS_HTTP_TIMEOUT is not a positive finite number.
    """
    env = _effective_environ(environ)
    raw = env.get("NEXTDNS_HTTP_TIMEOUT")
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


def get_download_max_bytes(environ: Mapping[str, str] | None = None) -> int:
    """Get the download cap from ``environ`` (the process environment by default).

    The cap is read from the ``NEXTDNS_DOWNLOAD_MAX_BYTES`` environment
    variable; see :data:`DEFAULT_DOWNLOAD_MAX_BYTES` for the default.

    Returns:
        int: Configured maximum bytes a log download may stream to disk
            (default 1 GiB).

    Raises:
        ConfigurationError: If NEXTDNS_DOWNLOAD_MAX_BYTES is not a positive integer.
    """
    env = _effective_environ(environ)
    raw = env.get("NEXTDNS_DOWNLOAD_MAX_BYTES")
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


def get_default_profile(environ: Mapping[str, str] | None = None) -> str | None:
    """Get default profile from ``environ`` (the process environment by default)."""
    env = _effective_environ(environ)
    return env.get("NEXTDNS_DEFAULT_PROFILE")


def is_generative_ui_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Check if Generative UI (on-the-fly Prefab UI generation) is enabled.

    Opt-in beta feature (disabled by default). Can be enabled by setting
    NEXTDNS_GENERATIVE_UI to 'true', '1', 'on', or 'yes'.
    """
    env = _effective_environ(environ)
    return _parse_bool_env(env.get("NEXTDNS_GENERATIVE_UI", "off")) is True


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


def get_tool_search_mode(environ: Mapping[str, str] | None = None) -> str:
    """Get the tool-search mode from ``environ`` (the process environment by default).

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
    env = _effective_environ(environ)
    raw = env.get("NEXTDNS_TOOL_SEARCH", "").strip().lower()
    if not raw:
        return DEFAULT_TOOL_SEARCH_MODE
    if raw not in TOOL_SEARCH_MODES:
        raise ConfigurationError(
            f"Invalid NEXTDNS_TOOL_SEARCH: {raw!r}. Expected one of: {', '.join(TOOL_SEARCH_MODES)}."
        )
    return raw


# The environment inputs a server snapshots at creation. Access-control and
# call-log settings are deliberately absent: those stay per-request/per-call
# reads so their existing semantics (see profile_access and call_log) cannot
# be changed by a configuration snapshot.
_SERVER_CONFIG_ENV_KEYS = (
    "NEXTDNS_API_BASE",
    "NEXTDNS_API_KEY",
    "NEXTDNS_API_KEY_FILE",
    "NEXTDNS_HTTP_TIMEOUT",
    "NEXTDNS_DOWNLOAD_MAX_BYTES",
    "NEXTDNS_DEFAULT_PROFILE",
    "NEXTDNS_GENERATIVE_UI",
    "NEXTDNS_TOOL_SEARCH",
)


@dataclass(frozen=True)
class ServerConfig:
    """The immutable configuration a single server was created with.

    A server snapshots the environment once, then every consumer reads through
    this object instead of ``os.environ``. Two embedded servers built under
    different environment values therefore keep different settings even after
    the process environment changes. Validation stays at the same use sites as
    before: the snapshot holds raw values and the existing getters parse them,
    so an invalid value still fails where it always did (for example the
    readiness probe can still report an invalid timeout as unready).

    The caller can supply the one generative-UI decision already made for the
    build; that avoids a second read of the same setting when the server
    assembles its catalog and provider.

    One deliberate exception to the freeze is a key sourced from
    ``NEXTDNS_API_KEY_FILE``: the path is frozen, but the file is re-read when
    a client is built, so secret rotation and deletion still take effect at
    client rebuild time.
    """

    _environ: Mapping[str, str]
    _generative_ui_enabled: bool | None = None

    @classmethod
    def from_env(cls, *, generative_ui_enabled: bool | None = None) -> "ServerConfig":
        """Snapshot the server configuration inputs from the current environment."""
        snapshot = MappingProxyType({key: os.environ[key] for key in _SERVER_CONFIG_ENV_KEYS if key in os.environ})
        return cls(snapshot, generative_ui_enabled)

    @property
    def api_base(self) -> str:
        return get_api_base(self._environ)

    @property
    def api_key(self) -> str | None:
        """The snapshot's API key; a file-sourced key is read from the frozen path."""
        return get_api_key(self._environ)

    @property
    def http_timeout(self) -> float:
        return get_http_timeout(self._environ)

    @property
    def download_max_bytes(self) -> int:
        return get_download_max_bytes(self._environ)

    @property
    def default_profile(self) -> str | None:
        return get_default_profile(self._environ)

    @property
    def generative_ui_enabled(self) -> bool:
        if self._generative_ui_enabled is not None:
            return self._generative_ui_enabled
        return is_generative_ui_enabled(self._environ)

    @property
    def tool_search_mode(self) -> str:
        return get_tool_search_mode(self._environ)


def _log_api_key_error() -> None:
    """Log error message for missing API key."""
    logger.critical("NEXTDNS_API_KEY is required")
    logger.critical("Set either:")
    logger.critical("  - NEXTDNS_API_KEY environment variable")
    logger.critical("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")


def _log_profile_access(profile_set: Collection[str] | None, access_type: str) -> None:
    """Log profile access configuration.

    Takes the snapshot's frozensets as-is: this function only reads the value
    to decide which of the three sentences to log, so it needs no mutable copy.
    """
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
    read_only = access.read_only

    if read_only:
        logger.info("Read-only mode is ENABLED - all write operations are disabled")

    _log_profile_access(access.readable, "readable")
    if not read_only:
        _log_profile_access(access.writable, "writable")
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
    get_api_base()  # Validates NEXTDNS_API_BASE (https scheme) at startup.
    get_tool_search_mode()  # Reject an unsupported disclosure mode before the server is built.
    for env_name, default in (
        ("NEXTDNS_READ_ONLY", ""),
        ("NEXTDNS_GENERATIVE_UI", "off"),
    ):
        raw = os.getenv(env_name, default)
        if _parse_bool_env(raw) is None:
            raise ConfigurationError(f"Invalid {env_name}: {raw!r}. Expected one of {sorted(_BOOL_TOKENS - {''})}.")

    _log_access_control_settings()
