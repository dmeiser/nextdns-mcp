"""Unit tests for file-based configuration."""

import logging
from unittest.mock import Mock, patch

import pytest

from nextdns_mcp import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clean API key environment variables before each test."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)


@pytest.fixture
def active_config():
    """Get the currently loaded nextdns_mcp.config module."""
    import sys

    return sys.modules.get("nextdns_mcp.config", config)


@pytest.fixture
def mock_logger(monkeypatch, active_config):
    """Mock nextdns_mcp.config.logger for testing error logs."""
    logger_mock = Mock(spec=logging.Logger)
    monkeypatch.setattr(active_config, "logger", logger_mock)
    return logger_mock


def test_get_api_key_from_file(monkeypatch, active_config, tmp_path):
    """Test reading API key from file."""
    key_file = tmp_path / "api_key.txt"
    key_file.write_text("test-api-key-from-file\n")  # Add newline to test stripping

    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(key_file))

    api_key = active_config.get_api_key()
    assert api_key == "test-api-key-from-file"


def test_get_api_key_file_not_found(monkeypatch, active_config, mock_logger):
    """Test handling of missing API key file."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", "/nonexistent/file")

    assert active_config.get_api_key() is None
    mock_logger.error.assert_called_once_with("API key file not found: /nonexistent/file")


def test_get_api_key_file_error(monkeypatch, active_config, mock_logger):
    """Test handling of API key file read error."""

    def mock_open(*args, **kwargs):
        raise PermissionError("Access denied")

    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", "/some/file")

    with patch("builtins.open", mock_open):
        assert active_config.get_api_key() is None
        mock_logger.error.assert_called_once_with("Failed to read API key file: Access denied")


def test_get_api_key_file_unicode_error(monkeypatch, active_config, mock_logger):
    """Test handling of API key file unicode decode error."""

    def mock_open(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "invalid start byte")

    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", "/some/file")

    with patch("builtins.open", mock_open):
        assert active_config.get_api_key() is None
        assert mock_logger.error.call_count == 1
        assert "Failed to read API key file" in mock_logger.error.call_args[0][0]


def test_parse_profile_list_empty(active_config):
    """Test parsing empty profile lists."""
    assert active_config.parse_profile_list("") is None
    assert active_config.parse_profile_list("  ") is None
    assert active_config.parse_profile_list(",") == set()
    assert active_config.parse_profile_list("  ,  ,  ") == set()


@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("", None),  # Empty string
        ("  ", None),  # Only whitespace
        ("ALL", set()),  # Allow all uppercase
        ("all", set()),  # Allow all lowercase
        ("profile1", {"profile1"}),  # Single profile
        ("profile1,profile2", {"profile1", "profile2"}),  # Multiple profiles
        ("  profile1  ,  profile2  ", {"profile1", "profile2"}),  # Extra whitespace
        ("profile1,,profile2", {"profile1", "profile2"}),  # Empty entries
        (",,,", set()),  # Only commas
        ("profile1,profile1,profile2", {"profile1", "profile2"}),  # Duplicates
    ],
)
def test_parse_profile_list_variants(active_config, input_str, expected):
    """Test parse_profile_list with various inputs."""
    assert active_config.parse_profile_list(input_str) == expected


def test_get_api_key_from_env(monkeypatch, active_config):
    """Test reading API key directly from environment."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "env-key")
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)
    assert active_config.get_api_key() == "env-key"
