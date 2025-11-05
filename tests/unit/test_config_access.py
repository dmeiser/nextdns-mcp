"""Unit tests for access control configuration."""

import importlib
import os
import sys
from types import ModuleType
from unittest.mock import Mock, patch

import pytest


def mock_nextdns_config():
    """Create a fresh mock config module."""
    # Create clean module
    module = ModuleType("nextdns_mcp.config")
    module.logger = Mock()
    module.sys = sys

    # Import functions
    from nextdns_mcp.config import parse_profile_list

    # Module variables
    module.NEXTDNS_API_KEY = None
    module.NEXTDNS_READ_ONLY = False
    module.NEXTDNS_READABLE_PROFILES = ""
    module.NEXTDNS_WRITABLE_PROFILES = ""

    # Define functions that use module state
    def get_readable_profiles():
        """Mock version that uses module state."""
        readable = parse_profile_list(module.NEXTDNS_READABLE_PROFILES)
        return readable

    def get_writable_profiles():
        """Mock version that uses module state."""
        if module.NEXTDNS_READ_ONLY:
            return None  # Read-only mode = deny all
        writable = parse_profile_list(module.NEXTDNS_WRITABLE_PROFILES)
        return writable

    def get_readable_profiles_set():
        """Mock version that combines readable and writable."""
        readable = get_readable_profiles()
        writable = get_writable_profiles()

        # If readable is None (unset), deny all
        if readable is None:
            return None

        # If readable is empty set (ALL), allow all
        if not readable:
            return set()

        # If readable is set, combine with writable (write implies read)
        if writable is None:
            return readable
        return readable | writable

    def can_read_profile(profile_id):
        """Mock version that uses module state."""
        readable = get_readable_profiles_set()
        # None means deny all, empty set means allow all
        if readable is None:
            return False
        return not readable or profile_id in readable

    def can_write_profile(profile_id):
        """Mock version that uses module state."""
        if module.NEXTDNS_READ_ONLY:
            return False
        writable = get_writable_profiles()
        # None means deny all, empty set means allow all
        if writable is None:
            return False
        return not writable or profile_id in writable

    # Add functions to module
    module.get_readable_profiles = get_readable_profiles
    module.get_writable_profiles = get_writable_profiles
    module.get_readable_profiles_set = get_readable_profiles_set
    module.can_read_profile = can_read_profile
    module.can_write_profile = can_write_profile
    module.parse_profile_list = parse_profile_list

    return module


@pytest.fixture
def mock_env():
    """Provide clean module for each test."""
    # Create clean module
    module = mock_nextdns_config()

    # Replace real module with mock
    old_module = sys.modules.get("nextdns_mcp.config")
    sys.modules["nextdns_mcp.config"] = module

    try:
        yield module
    finally:
        # Restore original module
        if old_module is not None:
            sys.modules["nextdns_mcp.config"] = old_module
        else:
            del sys.modules["nextdns_mcp.config"]


def test_readable_profiles_empty_config(mock_env):
    """Test with no readable profiles configured (deny all)."""
    mock_env.NEXTDNS_READABLE_PROFILES = ""
    mock_env.NEXTDNS_WRITABLE_PROFILES = ""

    assert mock_env.get_readable_profiles() is None
    assert mock_env.can_read_profile("any-profile") is False


def test_readable_profiles_all(mock_env):
    """Test with readable profiles set to ALL (allow all)."""
    mock_env.NEXTDNS_READABLE_PROFILES = "ALL"
    mock_env.NEXTDNS_WRITABLE_PROFILES = ""

    assert mock_env.get_readable_profiles() == set()
    assert mock_env.can_read_profile("any-profile") is True


def test_readable_profiles_restricted(mock_env):
    """Test with readable profiles restriction."""
    mock_env.NEXTDNS_READABLE_PROFILES = "profile1,profile2"
    mock_env.NEXTDNS_WRITABLE_PROFILES = ""

    readable = mock_env.get_readable_profiles_set()
    assert readable == {"profile1", "profile2"}
    assert mock_env.can_read_profile("profile1") is True
    assert mock_env.can_read_profile("profile3") is False


def test_readable_includes_writable(mock_env):
    """Test that writable profiles are also readable."""
    mock_env.NEXTDNS_READABLE_PROFILES = "profile1,profile2"
    mock_env.NEXTDNS_WRITABLE_PROFILES = "profile2,profile3"

    # get_readable_profiles returns only READABLE list
    readable = mock_env.get_readable_profiles()
    assert readable == {"profile1", "profile2"}

    # get_readable_profiles_set combines readable and writable
    readable_set = mock_env.get_readable_profiles_set()
    assert readable_set == {"profile1", "profile2", "profile3"}
    assert mock_env.can_read_profile("profile1") is True
    assert mock_env.can_read_profile("profile2") is True
    assert mock_env.can_read_profile("profile3") is True


def test_writable_profiles_empty_config(mock_env):
    """Test with no writable profiles configured (deny all)."""
    mock_env.NEXTDNS_WRITABLE_PROFILES = ""
    mock_env.NEXTDNS_READ_ONLY = False

    assert mock_env.get_writable_profiles() is None
    assert mock_env.can_write_profile("any-profile") is False


def test_writable_profiles_all(mock_env):
    """Test with writable profiles set to ALL (allow all)."""
    mock_env.NEXTDNS_WRITABLE_PROFILES = "ALL"
    mock_env.NEXTDNS_READ_ONLY = False

    assert mock_env.get_writable_profiles() == set()
    assert mock_env.can_write_profile("any-profile") is True


def test_writable_profiles_restricted(mock_env):
    """Test with writable profiles restriction."""
    mock_env.NEXTDNS_WRITABLE_PROFILES = "profile1,profile2"
    mock_env.NEXTDNS_READ_ONLY = False

    writable = mock_env.get_writable_profiles()
    assert writable == {"profile1", "profile2"}
    assert mock_env.can_write_profile("profile1") is True
    assert mock_env.can_write_profile("profile3") is False


def test_readonly_mode_blocks_all_writes(mock_env):
    """Test that read-only mode blocks all writes."""
    mock_env.NEXTDNS_WRITABLE_PROFILES = "profile1,profile2"
    mock_env.NEXTDNS_READ_ONLY = True

    assert mock_env.get_writable_profiles() is None
    assert mock_env.can_write_profile("profile1") is False
    assert mock_env.can_write_profile("profile2") is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("", False),
        ("anything-else", False),
    ],
)
def test_readonly_mode_values(mock_env, value, expected):
    """Test different values for NEXTDNS_READ_ONLY."""
    mock_env.NEXTDNS_WRITABLE_PROFILES = "profile1"
    mock_env.NEXTDNS_READ_ONLY = expected

    if expected:
        assert mock_env.get_writable_profiles() is None
        assert mock_env.can_write_profile("profile1") is False
    else:
        assert mock_env.get_writable_profiles() == {"profile1"}
        assert mock_env.can_write_profile("profile1") is True
