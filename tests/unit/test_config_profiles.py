"""Tests for profile access control configuration."""

import logging
import os
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def mock_logger():
    """Mock logger for tests."""
    return Mock(spec=logging.Logger)


@pytest.fixture
def patch_env(monkeypatch):
    """Fixture to patch environment for each test."""
    # Clear all environment variables for clean test
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    # Return monkeypatch.setenv for test use
    return monkeypatch.setenv


def test_profile_access_defaults(patch_env):
    """Test default profile access (no restrictions)."""
    import nextdns_mcp.config as config

    assert config.can_read_profile("any-profile")  # All readable by default
    assert config.can_write_profile("any-profile")  # All writable by default


def test_readable_profiles_restricted(patch_env):
    """Test readable profiles restrictions."""
    # Set up environment FIRST
    patch_env("NEXTDNS_READABLE_PROFILES", "profile1,profile2")
    patch_env("NEXTDNS_WRITABLE_PROFILES", "profile3")

    # Import after environment is set
    import nextdns_mcp.config as config

    # Can read specified profiles
    assert config.can_read_profile("profile1")
    assert config.can_read_profile("profile2")

    # Cannot read other profiles when readable is specified
    assert not config.can_read_profile("profile4")

    # Writable implies readable
    assert config.can_read_profile("profile3")

    # Get readable set matches all allowed profiles
    assert config.get_readable_profiles_set() == {"profile1", "profile2", "profile3"}


def test_writable_profiles_restricted(patch_env):
    """Test writable profiles restrictions."""
    # Set writable profiles
    patch_env("NEXTDNS_WRITABLE_PROFILES", "profile1,profile2")

    import nextdns_mcp.config as config

    # Test write permissions
    assert config.can_write_profile("profile1")
    assert config.can_write_profile("profile2")
    assert not config.can_write_profile("profile3")

    # Writable implies readable
    assert config.can_read_profile("profile1")
    assert config.can_read_profile("profile2")

    # Verify writable set
    assert config.get_writable_profiles_set() == {"profile1", "profile2"}


def test_readable_profiles_include_writable(patch_env):
    """Test that writable profiles are also readable."""
    # Set up environment FIRST
    patch_env("NEXTDNS_READABLE_PROFILES", "profile1,profile2")
    patch_env("NEXTDNS_WRITABLE_PROFILES", "profile2,profile3")

    # Import after environment is set
    import nextdns_mcp.config as config

    # Direct readable profiles
    assert config.can_read_profile("profile1")
    assert config.can_read_profile("profile2")

    # Readable via writable permission
    assert config.can_read_profile("profile3")

    # Verify write permissions
    assert not config.can_write_profile("profile1")  # Only readable
    assert config.can_write_profile("profile2")  # Both read/write
    assert config.can_write_profile("profile3")  # Write-only (implies read)

    # Verify readable set includes all profiles
    assert config.get_readable_profiles_set() == {"profile1", "profile2", "profile3"}


def test_read_only_mode_blocks_writes(patch_env):
    """Test read-only mode disables all writes."""
    # Enable read-only mode and set writable profiles
    patch_env("NEXTDNS_READ_ONLY", "true")
    patch_env("NEXTDNS_WRITABLE_PROFILES", "profile1,profile2")

    import nextdns_mcp.config as config

    # Verify reads still work
    assert config.can_read_profile("profile1")
    assert config.can_read_profile("profile2")

    # Verify all writes blocked regardless of writable profiles
    assert not config.can_write_profile("profile1")
    assert not config.can_write_profile("profile2")
    assert not config.can_write_profile("profile3")

    # Verify writable set is empty in read-only mode
    assert config.get_writable_profiles_set() == set()


def test_log_access_control_all_access(mock_logger, patch_env):
    """Test logging with no restrictions."""
    with patch("nextdns_mcp.config.logger", mock_logger):
        import nextdns_mcp.config as config

        config._log_access_control_settings()

    # Verify unrestricted access messages
    mock_logger.info.assert_any_call("All profiles are readable (no restrictions)")
    mock_logger.info.assert_any_call("All profiles are writable (no restrictions)")


def test_log_access_control_read_only(mock_logger, patch_env):
    """Test logging in read-only mode."""
    patch_env("NEXTDNS_READ_ONLY", "true")
    with patch("nextdns_mcp.config.logger", mock_logger):
        import nextdns_mcp.config as config

        config._log_access_control_settings()

    # Verify read-only mode message
    mock_logger.info.assert_any_call(
        "Read-only mode is ENABLED - all write operations are disabled"
    )


def test_log_access_control_restricted(mock_logger, patch_env):
    """Test logging with profile restrictions."""
    # Set up profile restrictions
    patch_env("NEXTDNS_READABLE_PROFILES", "profile1,profile2")
    patch_env("NEXTDNS_WRITABLE_PROFILES", "profile2")

    with patch("nextdns_mcp.config.logger", mock_logger):
        import nextdns_mcp.config as config

        config._log_access_control_settings()

    # Verify restriction messages
    readable_msg = "Readable profiles restricted to: ['profile1', 'profile2']"
    writable_msg = "Writable profiles restricted to: ['profile2']"

    mock_logger.info.assert_any_call(readable_msg)
    mock_logger.info.assert_any_call(writable_msg)
