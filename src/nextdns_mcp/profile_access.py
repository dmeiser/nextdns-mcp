"""Profile access-control policy for the NextDNS MCP server.

This module owns the write-scoping environment: read-only mode, the
readable/writable profile lists, the protected-profile denylist, and the
immutable per-request snapshot (:class:`ProfileAccessControl`) every access
check runs against. ``nextdns_mcp.config`` re-exports these names, so existing
``nextdns_mcp.config`` imports keep resolving.

Two sentinels shape every access decision: ``None`` means "deny all" and an
empty set means "allow all". The distinction is documented on
:func:`parse_profile_list` and pinned by tests rather than re-shaped per call
site.

The boolean environment parsing (``_parse_bool_env``) lives here because
``NEXTDNS_READ_ONLY`` is an access-control input, and a single token table
must serve every boolean environment variable so none of them parses the same
spelling differently.

SPDX-License-Identifier: MIT
"""

import logging
import os
from dataclasses import dataclass

from .constants import SAFE_PROFILE_ID_PATTERN
from .errors import ConfigurationError

logger = logging.getLogger(__name__)

# Constants for profile access control
ALLOW_ALL_PROFILES: set[str] = set()  # Represents "ALL" profiles

_BOOL_TRUE_TOKENS = frozenset(("true", "1", "yes", "on"))
_BOOL_FALSE_TOKENS = frozenset(("false", "0", "no", "off", ""))
_BOOL_TOKENS = _BOOL_TRUE_TOKENS | _BOOL_FALSE_TOKENS


def _parse_bool_env(value: str) -> bool | None:
    """Parse a boolean environment value.

    Returns ``True``/``False`` for recognized tokens and ``None`` for any
    unrecognized value, so callers can distinguish "off" from a typo.
    """
    normalized = value.strip().lower()
    if normalized in _BOOL_TRUE_TOKENS:
        return True
    if normalized in _BOOL_FALSE_TOKENS:
        return False
    return None


def _read_only_from_env() -> bool:
    """Read NEXTDNS_READ_ONLY once."""
    return _parse_bool_env(os.getenv("NEXTDNS_READ_ONLY", "")) is True


def is_read_only() -> bool:
    """Check if read-only mode is enabled."""
    return _read_only_from_env()


def _is_empty_profile_list(profile_str: str) -> bool:
    """Check if profile string is empty or whitespace."""
    return not profile_str or not profile_str.strip()


def _is_allow_all(profile_str: str) -> bool:
    """Check if profile string means 'allow all'."""
    return profile_str.strip().upper() == "ALL"


def _normalized_profile_ids(raw: str) -> set[str]:
    """The comma-separated profile ids in ``raw``, stripped and lowercased.

    The one parsing rule every profile list in the environment goes through,
    so the writable/readable lists and the protected denylist cannot parse
    the same string differently.
    """
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def parse_profile_list(profile_str: str) -> set[str] | None:
    """Parse a comma-separated list of profile IDs into an access decision.

    Returns:
        None - deny everything (unset, empty, or a list that names no profile:
        ``",,,"`` is punctuation, not a grant);
        the empty set - allow everything (the literal ``ALL``/``all``);
        a non-empty set - exactly those ids, lowercased.

        The None/set() sentinel is the shape every caller in this module and
        the client consumes; the distinction is documented here and pinned by
        tests rather than re-shaped per call site.
    """
    if _is_empty_profile_list(profile_str):
        return None  # Empty/unset = deny all
    if _is_allow_all(profile_str):
        return set()  # Empty set = allow all
    ids = _normalized_profile_ids(profile_str)
    if not ids:
        # Non-blank input that parses to no ids (e.g. ",,," or ", ,"): it is
        # neither unset nor ALL, and it names nothing - fail closed. Reading a
        # malformed allowlist as "allow all" inverts a typo into the widest
        # possible grant.
        return None
    return ids


def _profile_list_env(name: str) -> set[str] | None:
    """Read one profile-list environment variable and parse it as an allowlist.

    The one place an allowlist is read from the environment, so the empty
    default and the parser cannot drift between the direct readers
    (``get_readable_profiles`` / ``get_writable_profiles``) and the snapshot
    loader that feeds the per-request access checks.

    Returns:
        The same shape as :func:`parse_profile_list`: ``None`` for deny all,
        the empty set for allow all, else the normalized ids.
    """
    return parse_profile_list(os.getenv(name, ""))


def _profile_ids_env(name: str) -> set[str]:
    """Read one profile-list environment variable as bare normalized ids.

    For lists that are not allowlists and therefore have no deny-all/allow-all
    sentinel - the protected-profile denylist reads its members through here so
    its parsing cannot diverge from the allowlists' (see
    :func:`_normalized_profile_ids`).

    Returns:
        set[str]: Lowercased ids, empty when the variable is unset or blank.
    """
    return _normalized_profile_ids(os.getenv(name, ""))


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
    protected: frozenset[str] = frozenset()

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
        if profile_id.lower() in self.protected:
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


def get_protected_profiles() -> frozenset[str]:
    """Get the protected-profile denylist from ``NEXTDNS_PROTECTED_PROFILES``.

    Protected profiles are never writable, even when ``NEXTDNS_WRITABLE_PROFILES``
    is ``ALL``; reads are unaffected (writes-only by design, documented in
    docs/safety.md). Intended for data-bearing or production profiles that a
    test agent must not modify or delete.

    A denylist entry that cannot match any profile id is a configuration
    error, not an ignored entry: a typo in a *protection* list silently
    protects nothing, which is the worst possible failure mode for a
    denylist. The check runs at startup (``validate_configuration`` reads the
    access-control snapshot) and on each snapshot load, and says what the
    valid shape is.

    Returns:
        frozenset[str]: Lowercased profile IDs (empty when unset/empty).

    Raises:
        ConfigurationError: If any entry is not a valid profile-id shape.
    """
    ids = _profile_ids_env("NEXTDNS_PROTECTED_PROFILES")
    invalid = sorted(pid for pid in ids if not SAFE_PROFILE_ID_PATTERN.match(pid))
    if invalid:
        raise ConfigurationError(
            f"NEXTDNS_PROTECTED_PROFILES contains entries that cannot match any profile id: {', '.join(invalid)}. "
            "Profile ids are 6 to 10 lowercase alphanumeric characters."
        )
    return frozenset(ids)


def load_profile_access_control() -> ProfileAccessControl:
    """Read the profile ACL environment exactly once and return a snapshot.

    Each of NEXTDNS_READ_ONLY, NEXTDNS_READABLE_PROFILES,
    NEXTDNS_WRITABLE_PROFILES and NEXTDNS_PROTECTED_PROFILES is read once, so
    all decisions derived from the returned snapshot agree with each other.

    The snapshot is deliberately not cached across requests: it is a
    per-request value, so the next request re-reads the environment and no
    invalidation step (or TTL) is needed. Configuration changes therefore take
    effect on the next request, never retroactively on one already in flight.
    """
    read_only = _read_only_from_env()
    readable = _profile_list_env("NEXTDNS_READABLE_PROFILES")
    # Read-only mode denies all writes, so the writable set stays None.
    writable = None if read_only else _profile_list_env("NEXTDNS_WRITABLE_PROFILES")
    return ProfileAccessControl(
        read_only=read_only,
        readable=_combine_readable(readable, writable),
        writable=None if writable is None else frozenset(writable),
        protected=get_protected_profiles(),
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

    return _combine_set_readable(readable, writable)


def _combine_set_readable(readable: set[str], writable: set[str]) -> frozenset[str]:
    """Combine two explicitly configured sets.

    An empty set means ALL on either side, and ALL on either side makes every
    profile readable; otherwise the combination is readable plus writable
    (write implies read).
    """
    if not readable or not writable:
        return frozenset(ALLOW_ALL_PROFILES)
    return frozenset(readable | writable)


def get_readable_profiles() -> set[str] | None:
    """Get readable profile list from environment.

    Returns:
        None if empty/unset (deny all), empty set if "ALL" (allow all),
        or set of specific profile IDs
    """
    return _profile_list_env("NEXTDNS_READABLE_PROFILES")


def get_writable_profiles() -> set[str] | None:
    """Get writable profile list from environment.

    Returns:
        None if empty/unset (deny all), empty set if "ALL" (allow all),
        or set of specific profile IDs
    """
    if is_read_only():
        return None  # Read-only mode = deny all writes
    return _profile_list_env("NEXTDNS_WRITABLE_PROFILES")


def _mutable_profile_set(profiles: frozenset[str] | None) -> set[str] | None:
    """Copy a snapshot frozenset into a fresh set, keeping the ``None`` sentinel.

    The snapshot holds frozensets so no caller can mutate the decision another
    check already made; the convenience accessors hand back a mutable copy.
    ``None`` means deny all and must survive the copy as ``None`` - rewriting
    it to an empty set would read as allow all.
    """
    return None if profiles is None else set(profiles)


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
    return _mutable_profile_set(load_profile_access_control().readable)


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
    return _mutable_profile_set(load_profile_access_control().writable)


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
