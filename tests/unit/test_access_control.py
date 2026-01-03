"""Unit tests for profile access control functionality."""

import os

import pytest

from nextdns_mcp.config import (
    can_read_profile,
    can_write_profile,
    get_readable_profiles,
    get_writable_profiles,
    parse_profile_list,
)
from nextdns_mcp.server import extract_profile_id_from_url, is_write_operation


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


class TestParseProfileList:
    """Test the parse_profile_list function."""

    def test_parse_empty_string(self):
        """Test parsing an empty string returns None (deny all)."""
        result = parse_profile_list("")
        assert result is None

    def test_parse_whitespace_only(self):
        """Test parsing whitespace-only string returns None (deny all)."""
        result = parse_profile_list("   ")
        assert result is None

    def test_parse_all_uppercase(self):
        """Test parsing 'ALL' returns empty set (allow all)."""
        result = parse_profile_list("ALL")
        assert result == set()

    def test_parse_all_lowercase(self):
        """Test parsing 'all' returns empty set (allow all)."""
        result = parse_profile_list("all")
        assert result == set()

    def test_parse_all_mixed_case(self):
        """Test parsing 'All' returns empty set (allow all)."""
        result = parse_profile_list("All")
        assert result == set()

    def test_parse_all_with_whitespace(self):
        """Test parsing ' ALL ' returns empty set (allow all)."""
        result = parse_profile_list("  ALL  ")
        assert result == set()

    def test_parse_single_profile(self):
        """Test parsing a single profile ID."""
        result = parse_profile_list("abc123")
        assert result == {"abc123"}

    def test_parse_multiple_profiles(self):
        """Test parsing multiple profile IDs."""
        result = parse_profile_list("abc123,def456,ghi789")
        assert result == {"abc123", "def456", "ghi789"}

    def test_parse_with_whitespace(self):
        """Test parsing profiles with whitespace."""
        result = parse_profile_list(" abc123 , def456 , ghi789 ")
        assert result == {"abc123", "def456", "ghi789"}

    def test_parse_ignores_empty_items(self):
        """Test parsing ignores empty items between commas."""
        result = parse_profile_list("abc123,,def456,  ,ghi789")
        assert result == {"abc123", "def456", "ghi789"}


class TestGetReadableProfiles:
    """Test the get_readable_profiles function."""

    def test_empty_returns_none(self, clean_env):
        """Test that empty env vars return None (deny all)."""
        result = get_readable_profiles()
        assert result is None

    def test_all_returns_empty_set(self, clean_env):
        """Test that 'ALL' returns empty set (allow all)."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")
        result = get_readable_profiles()
        assert result == set()

    def test_returns_readable_profiles(self, clean_env):
        """Test that readable profiles are returned."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123,def456")
        result = get_readable_profiles()
        assert result == {"abc123", "def456"}

    def test_writable_does_not_affect_readable(self, clean_env):
        """Test that writable profiles don't affect get_readable_profiles result."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "ghi789")
        result = get_readable_profiles()
        # None means deny all (readable not set)
        assert result is None

    def test_combines_readable_and_writable(self, clean_env):
        """Test that get_readable_profiles returns only READABLE, not combined."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")
        clean_env("NEXTDNS_WRITABLE_PROFILES", "def456,ghi789")
        result = get_readable_profiles()
        # get_readable_profiles() returns ONLY what's in NEXTDNS_READABLE_PROFILES
        assert result == {"abc123"}

    def test_readable_without_writable(self, clean_env):
        """Test that only readable profiles are returned when writable is empty."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")
        result = get_readable_profiles()
        assert result == {"abc123"}


class TestGetWritableProfiles:
    """Test the get_writable_profiles function."""

    def test_empty_returns_none(self, clean_env):
        """Test that empty env var returns None (deny all)."""
        result = get_writable_profiles()
        assert result is None

    def test_all_returns_empty_set(self, clean_env):
        """Test that 'ALL' returns empty set (allow all)."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "ALL")
        result = get_writable_profiles()
        assert result == set()

    def test_returns_writable_profiles(self, clean_env):
        """Test that writable profiles are returned."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
        result = get_writable_profiles()
        assert result == {"abc123", "def456"}

    def test_read_only_returns_none(self, clean_env):
        """Test that read-only mode returns None (no profiles writable)."""
        clean_env("NEXTDNS_READ_ONLY", "true")
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
        result = get_writable_profiles()
        assert result is None


class TestCanReadProfile:
    """Test the can_read_profile function."""

    def test_denies_all_when_empty(self, clean_env):
        """Test that no profiles are readable when not specified (deny all)."""
        assert can_read_profile("abc123") is False
        assert can_read_profile("xyz999") is False

    def test_allows_all_when_set_to_all(self, clean_env):
        """Test that all profiles are readable when set to ALL."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")
        assert can_read_profile("abc123") is True
        assert can_read_profile("xyz999") is True

    def test_allows_only_listed_profiles(self, clean_env):
        """Test that only listed profiles are readable."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123,def456")
        assert can_read_profile("abc123") is True
        assert can_read_profile("def456") is True
        assert can_read_profile("xyz999") is False

    def test_writable_is_readable(self, clean_env):
        """Test that writable profiles are readable."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")
        clean_env("NEXTDNS_WRITABLE_PROFILES", "ghi789")
        assert can_read_profile("abc123") is True  # In readable list
        assert can_read_profile("ghi789") is True  # In writable list (write implies read)
        assert can_read_profile("xyz999") is False  # Not in either list


class TestCanWriteProfile:
    """Test the can_write_profile function."""

    def test_denies_all_when_empty(self, clean_env):
        """Test that no profiles are writable when not specified (deny all)."""
        assert can_write_profile("abc123") is False
        assert can_write_profile("xyz999") is False

    def test_allows_all_when_set_to_all(self, clean_env):
        """Test that all profiles are writable when set to ALL."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "ALL")
        assert can_write_profile("abc123") is True
        assert can_write_profile("xyz999") is True

    def test_allows_only_listed_profiles(self, clean_env):
        """Test that only listed profiles are writable."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
        assert can_write_profile("abc123") is True
        assert can_write_profile("def456") is True
        assert can_write_profile("xyz999") is False

    def test_read_only_denies_all(self, clean_env):
        """Test that read-only mode denies all writes."""
        clean_env("NEXTDNS_READ_ONLY", "true")
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
        assert can_write_profile("abc123") is False
        assert can_write_profile("def456") is False
        assert can_write_profile("xyz999") is False


class TestExtractProfileIdFromUrl:
    """Test the extract_profile_id_from_url function."""

    def test_extracts_from_settings_url(self):
        """Test extracting profile ID from settings URL."""
        result = extract_profile_id_from_url("/profiles/abc123/settings")
        assert result == "abc123"

    def test_extracts_from_profile_url(self):
        """Test extracting profile ID from profile URL."""
        result = extract_profile_id_from_url("/profiles/def456")
        assert result == "def456"

    def test_extracts_from_nested_url(self):
        """Test extracting profile ID from deeply nested URL."""
        result = extract_profile_id_from_url("/profiles/ghi789/privacy/blocklists")
        assert result == "ghi789"

    def test_extracts_without_leading_slash(self):
        """Test extracting profile ID without leading slash."""
        result = extract_profile_id_from_url("profiles/jkl012/logs")
        assert result == "jkl012"

    def test_returns_none_for_list_profiles(self):
        """Test that /profiles without ID returns None."""
        result = extract_profile_id_from_url("/profiles")
        assert result is None

    def test_returns_none_for_non_profile_url(self):
        """Test that non-profile URLs return None."""
        result = extract_profile_id_from_url("/analytics/status")
        assert result is None


class TestIsWriteOperation:
    """Test the is_write_operation function."""

    def test_get_is_not_write(self):
        """Test that GET is not a write operation."""
        assert is_write_operation("GET") is False
        assert is_write_operation("get") is False

    def test_post_is_write(self):
        """Test that POST is a write operation."""
        assert is_write_operation("POST") is True
        assert is_write_operation("post") is True

    def test_put_is_write(self):
        """Test that PUT is a write operation."""
        assert is_write_operation("PUT") is True
        assert is_write_operation("put") is True

    def test_patch_is_write(self):
        """Test that PATCH is a write operation."""
        assert is_write_operation("PATCH") is True
        assert is_write_operation("patch") is True

    def test_delete_is_write(self):
        """Test that DELETE is a write operation."""
        assert is_write_operation("DELETE") is True
        assert is_write_operation("delete") is True
