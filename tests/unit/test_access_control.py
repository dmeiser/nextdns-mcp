"""Unit tests for profile access control functionality."""

import pytest
from unittest.mock import patch

from nextdns_mcp.config import (
    parse_profile_list,
    get_readable_profiles,
    get_writable_profiles,
    can_read_profile,
    can_write_profile,
)
from nextdns_mcp.server import (
    extract_profile_id_from_url,
    is_write_operation,
)


class TestParseProfileList:
    """Test the parse_profile_list function."""

    def test_parse_empty_string(self):
        """Test parsing an empty string returns empty set."""
        result = parse_profile_list("")
        assert result == set()

    def test_parse_whitespace_only(self):
        """Test parsing whitespace-only string returns empty set."""
        result = parse_profile_list("   ")
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

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_empty_returns_empty_set(self):
        """Test that empty env vars return empty set (all profiles readable)."""
        result = get_readable_profiles()
        assert result == set()

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "abc123,def456")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_returns_readable_profiles(self):
        """Test that readable profiles are returned."""
        result = get_readable_profiles()
        assert result == {"abc123", "def456"}

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "ghi789")
    def test_writable_with_empty_readable_means_all_readable(self):
        """Test that empty readable means all profiles are readable (even with writable set)."""
        result = get_readable_profiles()
        # Empty set means ALL profiles are readable
        assert result == set()

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "abc123")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "def456,ghi789")
    def test_combines_readable_and_writable(self):
        """Test that readable and writable are combined when both are set."""
        result = get_readable_profiles()
        assert result == {"abc123", "def456", "ghi789"}

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "abc123")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_readable_without_writable(self):
        """Test that only readable profiles are returned when writable is empty."""
        result = get_readable_profiles()
        assert result == {"abc123"}


class TestGetWritableProfiles:
    """Test the get_writable_profiles function."""

    @patch("nextdns_mcp.config.NEXTDNS_READ_ONLY", False)
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_empty_returns_empty_set(self):
        """Test that empty env var returns empty set (all profiles writable)."""
        result = get_writable_profiles()
        assert result == set()

    @patch("nextdns_mcp.config.NEXTDNS_READ_ONLY", False)
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
    def test_returns_writable_profiles(self):
        """Test that writable profiles are returned."""
        result = get_writable_profiles()
        assert result == {"abc123", "def456"}

    @patch("nextdns_mcp.config.NEXTDNS_READ_ONLY", True)
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
    def test_read_only_returns_empty_set(self):
        """Test that read-only mode returns empty set (no profiles writable)."""
        result = get_writable_profiles()
        assert result == set()


class TestCanReadProfile:
    """Test the can_read_profile function."""

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_allows_all_when_empty(self):
        """Test that all profiles are readable when no restrictions."""
        assert can_read_profile("abc123") is True
        assert can_read_profile("xyz999") is True

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "abc123,def456")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_allows_only_listed_profiles(self):
        """Test that only listed profiles are readable."""
        assert can_read_profile("abc123") is True
        assert can_read_profile("def456") is True
        assert can_read_profile("xyz999") is False

    @patch("nextdns_mcp.config.NEXTDNS_READABLE_PROFILES", "")
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "ghi789")
    def test_writable_is_readable(self):
        """Test that writable profiles are readable."""
        assert can_read_profile("ghi789") is True
        assert can_read_profile("xyz999") is True  # Empty readable = all readable


class TestCanWriteProfile:
    """Test the can_write_profile function."""

    @patch("nextdns_mcp.config.NEXTDNS_READ_ONLY", False)
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "")
    def test_allows_all_when_empty(self):
        """Test that all profiles are writable when no restrictions."""
        assert can_write_profile("abc123") is True
        assert can_write_profile("xyz999") is True

    @patch("nextdns_mcp.config.NEXTDNS_READ_ONLY", False)
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "abc123,def456")
    def test_allows_only_listed_profiles(self):
        """Test that only listed profiles are writable."""
        assert can_write_profile("abc123") is True
        assert can_write_profile("def456") is True
        assert can_write_profile("xyz999") is False

    @patch("nextdns_mcp.config.NEXTDNS_READ_ONLY", True)
    @patch("nextdns_mcp.config.NEXTDNS_WRITABLE_PROFILES", "abc123")
    def test_read_only_denies_all(self):
        """Test that read-only mode denies all writes."""
        assert can_write_profile("abc123") is False
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
