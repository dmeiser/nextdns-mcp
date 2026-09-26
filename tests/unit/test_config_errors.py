"""Unit tests for error handling in configuration."""

import logging
from unittest.mock import Mock, call

import pytest

from nextdns_mcp import config
from nextdns_mcp.config import MissingApiKeyError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clean configuration environment variables before each test."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
    monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
    monkeypatch.delenv("NEXTDNS_WRITABLE_PROFILES", raising=False)


@pytest.fixture
def active_config():
    """Get the currently loaded nextdns_mcp.config module."""
    import sys

    return sys.modules.get("nextdns_mcp.config", config)


@pytest.fixture
def mock_logger(monkeypatch, active_config):
    """Mock nextdns_mcp.config.logger for verifying log calls."""
    logger_mock = Mock(spec=logging.Logger)
    monkeypatch.setattr(active_config, "logger", logger_mock)
    return logger_mock


def test_log_api_key_error(active_config, mock_logger):
    """Test API key error logging."""
    active_config._log_api_key_error()

    # Check each expected call individually for clarity in errors
    calls = [
        call("NEXTDNS_API_KEY is required"),
        call("Set either:"),
        call("  - NEXTDNS_API_KEY environment variable"),
        call("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret"),
    ]

    for expected_call in calls:
        msg = f"Missing expected critical log: {expected_call}"
        assert expected_call in mock_logger.critical.mock_calls, msg


def test_get_api_key_returns_value(monkeypatch, active_config):
    """Test get_api_key returns the configured key."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "some-key")
    assert active_config.get_api_key() == "some-key"


def test_log_access_control_settings_restricted(monkeypatch, active_config, mock_logger):
    """Test access control logging with both readable and writable restrictions."""
    monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "profile1,profile2")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile2")

    active_config._log_access_control_settings()

    expected_calls = [
        call("Read-only mode is ENABLED - all write operations are disabled"),
        call("Readable profiles restricted to: ['profile1', 'profile2']"),
    ]

    for expected_call in expected_calls:
        assert expected_call in mock_logger.info.mock_calls, f"Missing expected info log: {expected_call}"


def test_log_access_control_settings_unrestricted(monkeypatch, active_config, mock_logger):
    """Test access control logging with no restrictions."""
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    active_config._log_access_control_settings()

    expected_calls = [
        call("All profiles are readable (no restrictions)"),
        call("All profiles are writable (no restrictions)"),
    ]

    for expected_call in expected_calls:
        assert expected_call in mock_logger.info.mock_calls, f"Missing expected info log: {expected_call}"

    assert mock_logger.info.call_count == 2


def test_log_access_control_settings_default_deny(monkeypatch, active_config, mock_logger):
    """Test access control logging with default deny-all (no profiles set)."""
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
    monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
    monkeypatch.delenv("NEXTDNS_WRITABLE_PROFILES", raising=False)

    active_config._log_access_control_settings()

    expected_calls = [
        call("No profiles are readable (deny all by default)"),
        call("No profiles are writable (deny all by default)"),
    ]

    for expected_call in expected_calls:
        assert expected_call in mock_logger.info.mock_calls, f"Missing expected info log: {expected_call}"


def test_log_access_control_settings_writable_restricted(monkeypatch, active_config, mock_logger):
    """Test access control logging when writable profiles are restricted and read-only is disabled."""
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "profile1")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "profile2")

    active_config._log_access_control_settings()

    expected_calls = [
        call("Readable profiles restricted to: ['profile1', 'profile2']"),
        call("Writable profiles restricted to: ['profile2']"),
    ]

    for expected_call in expected_calls:
        assert expected_call in mock_logger.info.mock_calls, f"Missing expected info log: {expected_call}"


def test_validate_configuration_raises_on_missing_api_key(monkeypatch, active_config, mock_logger):
    """Test validate_configuration raises when API key is missing."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

    with pytest.raises(MissingApiKeyError):
        active_config.validate_configuration()

    calls = [
        call("NEXTDNS_API_KEY is required"),
        call("Set either:"),
        call("  - NEXTDNS_API_KEY environment variable"),
        call("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret"),
    ]

    for expected_call in calls:
        msg = f"Missing expected critical log: {expected_call}"
        assert expected_call in mock_logger.critical.mock_calls, msg


def test_validate_configuration_logs_settings(monkeypatch, active_config, mock_logger):
    """Test validate_configuration logs access control settings when API key exists."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-key")
    monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    active_config.validate_configuration()

    assert call("Read-only mode is ENABLED - all write operations are disabled") in mock_logger.info.mock_calls
    assert call("All profiles are readable (no restrictions)") in mock_logger.info.mock_calls

    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)

    with pytest.raises(MissingApiKeyError):
        active_config.validate_configuration()

    calls = [
        call("NEXTDNS_API_KEY is required"),
        call("Set either:"),
        call("  - NEXTDNS_API_KEY environment variable"),
        call("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret"),
    ]

    for expected_call in calls:
        msg = f"Missing expected critical log: {expected_call}"
        assert expected_call in mock_logger.critical.mock_calls, msg

    assert mock_logger.critical.call_count == 4
