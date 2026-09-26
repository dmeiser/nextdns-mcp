"""Unit tests for access control configuration."""

import pytest

from nextdns_mcp.config import (
    can_read_profile,
    can_write_profile,
    get_readable_profiles,
    get_readable_profiles_set,
    get_writable_profiles,
    get_writable_profiles_set,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clean access control environment variables before each test."""
    monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
    monkeypatch.delenv("NEXTDNS_WRITABLE_PROFILES", raising=False)
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)


def test_readable_profiles_empty_config(monkeypatch):
    """Test with no readable profiles configured (deny all)."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")

    assert get_readable_profiles() is None
    assert can_read_profile("any-profile") is False


def test_readable_profiles_all(monkeypatch):
    """Test with readable profiles set to ALL (allow all)."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")

    assert get_readable_profiles() == set()
    assert can_read_profile("any-profile") is True


def test_readable_profiles_restricted(monkeypatch):
    """Test with readable profiles restriction."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "profile1,profile2")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")

    readable = get_readable_profiles_set()
    assert readable == {"profile1", "profile2"}
    assert can_read_profile("profile1") is True
    assert can_read_profile("profile3") is False


def test_readable_includes_writable(monkeypatch):
    """Test that writable profiles are also readable."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "profile1,profile2")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile2,profile3")

    # get_readable_profiles returns only READABLE list
    readable = get_readable_profiles()
    assert readable == {"profile1", "profile2"}

    # get_readable_profiles_set combines readable and writable
    readable_set = get_readable_profiles_set()
    assert readable_set == {"profile1", "profile2", "profile3"}
    assert can_read_profile("profile1") is True
    assert can_read_profile("profile2") is True
    assert can_read_profile("profile3") is True


def test_writable_all_implies_readable_all(monkeypatch):
    """Test that writable profiles set to ALL implies readable ALL."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "profile1")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    readable_set = get_readable_profiles_set()
    assert readable_set == set()
    assert can_read_profile("profile1") is True
    assert can_read_profile("any-profile") is True


def test_readable_unset_writable_set(monkeypatch):
    """Test that unset readable profiles adopt writable profiles."""
    monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile1,profile2")

    assert get_readable_profiles() is None
    readable_set = get_readable_profiles_set()
    assert readable_set == {"profile1", "profile2"}
    assert can_read_profile("profile1") is True
    assert can_read_profile("profile3") is False


def test_readable_unset_writable_all(monkeypatch):
    """Test that unset readable profiles adopt writable ALL."""
    monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    assert get_readable_profiles() is None
    readable_set = get_readable_profiles_set()
    assert readable_set == set()
    assert can_read_profile("any-profile") is True


def test_writable_profiles_empty_config(monkeypatch):
    """Test with no writable profiles configured (deny all)."""
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)

    assert get_writable_profiles() is None
    assert can_write_profile("any-profile") is False


def test_writable_profiles_all(monkeypatch):
    """Test with writable profiles set to ALL (allow all)."""
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)

    assert get_writable_profiles() == set()
    assert can_write_profile("any-profile") is True


def test_writable_profiles_restricted(monkeypatch):
    """Test with writable profiles restriction."""
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile1,profile2")
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)

    writable = get_writable_profiles()
    assert writable == {"profile1", "profile2"}
    assert can_write_profile("profile1") is True
    assert can_write_profile("profile3") is False


def test_readonly_mode_blocks_all_writes(monkeypatch):
    """Test that read-only mode blocks all writes."""
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile1,profile2")
    monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")

    assert get_writable_profiles() is None
    assert get_writable_profiles_set() is None
    assert can_write_profile("profile1") is False
    assert can_write_profile("profile2") is False


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
def test_readonly_mode_values(monkeypatch, value, expected):
    """Test different values for NEXTDNS_READ_ONLY."""
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile1")
    monkeypatch.setenv("NEXTDNS_READ_ONLY", value)

    if expected:
        assert get_writable_profiles() is None
        assert can_write_profile("profile1") is False
    else:
        assert get_writable_profiles() == {"profile1"}
        assert can_write_profile("profile1") is True
