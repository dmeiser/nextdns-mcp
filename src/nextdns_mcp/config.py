"""Configuration module for NextDNS MCP Server.

This module handles all configuration loading, validation, and constants
for the NextDNS MCP server.

SPDX-License-Identifier: MIT
"""

import logging
import math
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised when configuration values are invalid or malformed."""


class MissingApiKeyError(ConfigurationError):
    """Raised when a NextDNS API key has not been configured yet.

    Subclasses ``ConfigurationError`` so that a single ``except
    ConfigurationError`` covers both "no key configured yet" and "the value
    you configured is invalid".
    """


def configure_logging() -> None:
    """Configure root logging for the NextDNS MCP server.

    Called only from the ``__main__`` entrypoint so that importing this
    module has no logging side effects.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# Core API configuration
NEXTDNS_BASE_URL = "https://api.nextdns.io"

# MCP Transport configuration

# Default HTTP request timeout in seconds
DEFAULT_HTTP_TIMEOUT: float = 30.0

# Default hard cap on the total bytes a log download may stream to disk, in
# bytes. The cap is enforced while streaming so a single download cannot fill
# the OS temp directory. Overridable via NEXTDNS_DOWNLOAD_MAX_BYTES.
DEFAULT_DOWNLOAD_MAX_BYTES: int = 1024 * 1024 * 1024  # 1 GiB

# Constants for profile access control
ALLOW_ALL_PROFILES: set[str] = set()  # Represents "ALL" profiles


def get_api_key() -> str | None:
    """Get API key from environment."""
    key = os.getenv("NEXTDNS_API_KEY")
    if key:
        return key.strip()

    # 2. Check file specified in environment variable
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
            f"Invalid NEXTDNS_DOWNLOAD_MAX_BYTES: {raw!r}. Expected a positive number of bytes."
        ) from None

    return val


def get_default_profile() -> str | None:
    """Get default profile from environment."""
    return os.getenv("NEXTDNS_DEFAULT_PROFILE")


def _read_only_from_env() -> bool:
    """Read NEXTDNS_READ_ONLY once."""
    value = os.getenv("NEXTDNS_READ_ONLY", "").lower()
    return value in ("true", "1", "yes")


def is_read_only() -> bool:
    """Check if read-only mode is enabled."""
    return _read_only_from_env()


def get_readable_profiles() -> set[str] | None:
    """Get readable profile list from environment.

    Returns:
        None if empty/unset (deny all), empty set if "ALL" (allow all),
        or set of specific profile IDs
    """
    profiles = os.getenv("NEXTDNS_READABLE_PROFILES", "")
    return parse_profile_list(profiles)


def get_writable_profiles() -> set[str] | None:
    """Get writable profile list from environment.

    Returns:
        None if empty/unset (deny all), empty set if "ALL" (allow all),
        or set of specific profile IDs
    """
    if is_read_only():
        return None  # Read-only mode = deny all writes
    profiles = os.getenv("NEXTDNS_WRITABLE_PROFILES", "")
    return parse_profile_list(profiles)


def _is_empty_profile_list(profile_str: str) -> bool:
    """Check if profile string is empty or whitespace."""
    return not profile_str or not profile_str.strip()


def _is_allow_all(profile_str: str) -> bool:
    """Check if profile string means 'allow all'."""
    return profile_str.strip().upper() == "ALL"


def parse_profile_list(profile_str: str) -> set[str] | None:
    """Parse a comma-separated list of profile IDs.

    Args:
        profile_str: Comma-separated string of profile IDs

    Returns:
        Set of profile IDs normalized to lowercase (profile IDs are hex and
        matched case-insensitively), None if string is empty/unset (deny all),
        or empty set if "ALL"/"all" specified (allow all)
    """
    if _is_empty_profile_list(profile_str):
        return None  # Empty/unset = deny all
    if _is_allow_all(profile_str):
        return set()  # Empty set = allow all
    return {p.strip().lower() for p in profile_str.split(",") if p.strip()}


@dataclass(frozen=True)
class ProfileAccessControl:
    """Immutable snapshot of the profile access control environment.

    Every request takes exactly one snapshot (see
    :func:`load_profile_access_control`) and runs all of its access checks
    against it, so a change to the process environment that lands while a
    request is in flight cannot desynchronize the checks from each other or
    from the decision to send the request upstream.

    ``readable`` is the *combined* readable set (write implies read) and
    ``writable`` is the raw writable set; both use ``None`` for "deny all" and
    an empty set for "allow all".
    """

    read_only: bool
    readable: frozenset[str] | None
    writable: frozenset[str] | None

    def can_read(self, profile_id: str) -> bool:
        """Return whether the snapshot allows reading ``profile_id``."""
        if self.readable is None:
            return False
        if not self.readable:
            return True
        return profile_id.lower() in self.readable

    def can_write(self, profile_id: str) -> bool:
        """Return whether the snapshot allows writing ``profile_id``."""
        if self.read_only or self.writable is None:
            return False
        if not self.writable:
            return True
        return profile_id.lower() in self.writable

    @property
    def any_readable(self) -> bool:
        """Whether the global (deny-all) read gate is open."""
        return self.readable is not None

    @property
    def any_writable(self) -> bool:
        """Whether the global (deny-all) write gate is open."""
        return not self.read_only and self.writable is not None


def load_profile_access_control() -> ProfileAccessControl:
    """Read the profile ACL environment exactly once and return a snapshot.

    Each of NEXTDNS_READ_ONLY, NEXTDNS_READABLE_PROFILES and
    NEXTDNS_WRITABLE_PROFILES is read once, so all decisions derived from the
    returned snapshot agree with each other.

    The snapshot is deliberately not cached across requests: it is a
    per-request value, so the next request re-reads the environment and no
    invalidation step (or TTL) is needed. Configuration changes therefore take
    effect on the next request, never retroactively on one already in flight.
    """
    read_only = _read_only_from_env()
    readable = parse_profile_list(os.getenv("NEXTDNS_READABLE_PROFILES", ""))
    # Read-only mode denies all writes, so the writable set stays None.
    writable = None if read_only else parse_profile_list(os.getenv("NEXTDNS_WRITABLE_PROFILES", ""))
    return ProfileAccessControl(
        read_only=read_only,
        readable=_combine_readable(readable, writable),
        writable=None if writable is None else frozenset(writable),
    )


def _combine_readable(readable: set[str] | None, writable: set[str] | None) -> frozenset[str] | None:
    """Combine the readable and writable sets into the effective readable set.

    Returns:
        None if nothing is readable (deny all), an empty frozenset if all
        profiles are readable (allow all), else the set of readable profile IDs.
    """
    # No writable profiles: the readable list alone decides, and both unset = deny all
    if writable is None:
        return None if readable is None else frozenset(readable)

    # Readable unset but writable set: writable profiles are implicitly readable
    if readable is None:
        return frozenset(writable)

    # If readable is empty set (ALL), allow all
    if not readable:
        return frozenset(ALLOW_ALL_PROFILES)

    # Writable ALL implies readable ALL
    if not writable:
        return frozenset(ALLOW_ALL_PROFILES)

    # Readable is set: combine with writable (write implies read)
    return frozenset(readable | writable)


def get_readable_profiles_set() -> set[str] | None:
    """Get the set of profiles that are allowed to be read.

    Convenience wrapper that takes a fresh snapshot; callers performing
    several checks for one request should take a single snapshot with
    :func:`load_profile_access_control` instead.

    Returns:
        None if no profiles are readable (deny all),
        empty set if all profiles are readable (allow all),
        or set of specific profile IDs
    """
    readable = load_profile_access_control().readable
    return None if readable is None else set(readable)


def get_writable_profiles_set() -> set[str] | None:
    """Get the set of profiles that are allowed to be written to.

    Convenience wrapper that takes a fresh snapshot; callers performing
    several checks for one request should take a single snapshot with
    :func:`load_profile_access_control` instead.

    Returns:
        None if no profiles are writable (deny all),
        empty set if all profiles are writable (allow all),
        or set of specific profile IDs
    """
    writable = load_profile_access_control().writable
    return None if writable is None else set(writable)


def can_read_profile(profile_id: str) -> bool:
    """Check if a profile can be read.

    Convenience wrapper around a fresh snapshot; see
    :func:`load_profile_access_control`.

    Args:
        profile_id: The profile ID to check

    Returns:
        True if the profile can be read, False otherwise
    """
    return load_profile_access_control().can_read(profile_id)


def can_write_profile(profile_id: str) -> bool:
    """Check if a profile can be written to.

    Convenience wrapper around a fresh snapshot; see
    :func:`load_profile_access_control`.

    Args:
        profile_id: The profile ID to check

    Returns:
        True if the profile can be written to, False otherwise
    """
    return load_profile_access_control().can_write(profile_id)


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
    _log_access_control_settings()


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
