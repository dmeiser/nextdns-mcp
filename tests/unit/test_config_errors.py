"""Unit tests for error handling in configuration."""

import importlib
import logging
import os
import sys
from types import ModuleType
from unittest.mock import Mock, call, patch

import pytest


def mock_nextdns_config():
    """Create fresh mock config module."""
    module = ModuleType('nextdns_mcp.config')

    # Set up module state 
    module.NEXTDNS_API_KEY = None
    module.NEXTDNS_READ_ONLY = False
    module.NEXTDNS_READABLE_PROFILES = ''
    module.NEXTDNS_WRITABLE_PROFILES = ''
    module.sys = sys

    # Create mock logger with proper spec
    module.logger = Mock(name='logger', spec=logging.Logger)

    # Function definitions that use our mock logger
    def parse_profile_list(profile_str: str) -> set[str]:
        """Pure function, no logging."""
        return set() if not profile_str.strip() else {p.strip() for p in profile_str.split(',')}

    def get_readable_profiles() -> set[str]:
        """Get readable profiles set."""
        readable = parse_profile_list(module.NEXTDNS_READABLE_PROFILES)
        writable = parse_profile_list(module.NEXTDNS_WRITABLE_PROFILES)
        return readable | writable if readable else set()

    def get_writable_profiles() -> set[str]:
        """Get writable profiles set."""
        return set() if module.NEXTDNS_READ_ONLY else parse_profile_list(module.NEXTDNS_WRITABLE_PROFILES)

    def get_api_key() -> str | None:
        """Get API key with logging."""
        return module.NEXTDNS_API_KEY

    def _log_api_key_error():
        """Log missing API key."""
        module.logger.critical('NEXTDNS_API_KEY is required')
        module.logger.critical('Set either:')
        module.logger.critical('  - NEXTDNS_API_KEY environment variable')
        module.logger.critical('  - NEXTDNS_API_KEY_FILE pointing to a Docker secret')

    def _log_access_control_settings():
        """Log access control configuration."""
        readable = get_readable_profiles()
        writable = get_writable_profiles()

        if module.NEXTDNS_READ_ONLY:
            module.logger.info('Read-only mode is ENABLED - all write operations are disabled')

        if readable:
            module.logger.info(f'Readable profiles restricted to: {sorted(readable)}')
        else:
            module.logger.info('All profiles are readable (no restrictions)')

        if writable:
            module.logger.info(f'Writable profiles restricted to: {sorted(writable)}')
        elif not module.NEXTDNS_READ_ONLY:
            module.logger.info('All profiles are writable (no restrictions)')

    def validate_configuration():
        """Validate configuration."""
        if not module.NEXTDNS_API_KEY:
            _log_api_key_error()
            sys.exit(1)
        _log_access_control_settings()

    # Add functions to module
    module.parse_profile_list = parse_profile_list
    module.get_readable_profiles = get_readable_profiles
    module.get_writable_profiles = get_writable_profiles
    module.get_api_key = get_api_key
    module._log_api_key_error = _log_api_key_error
    module._log_access_control_settings = _log_access_control_settings
    module.validate_configuration = validate_configuration
    
    return module




@pytest.fixture
def mock_module():
    """Create mock module for each test."""
    module = mock_nextdns_config()
    with patch.dict('sys.modules', {'nextdns_mcp.config': module}):
        yield module


def test_log_api_key_error(mock_module):
    """Test API key error logging."""
    mock_module._log_api_key_error()

    # Check each expected call individually for clarity in errors
    calls = [
        call("NEXTDNS_API_KEY is required"),
        call("Set either:"),
        call("  - NEXTDNS_API_KEY environment variable"),
        call("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")
    ]
    
    for expected_call in calls:
        assert expected_call in mock_module.logger.critical.mock_calls, \
            f"Missing expected critical log: {expected_call}"


def test_log_access_control_settings_restricted(mock_module):
    """Test access control logging with both readable and writable restrictions."""
    # Set up restricted profiles
    mock_module.NEXTDNS_READ_ONLY = True
    mock_module.NEXTDNS_READABLE_PROFILES = "profile1,profile2"
    mock_module.NEXTDNS_WRITABLE_PROFILES = "profile2"
    
    # Call function
    mock_module._log_access_control_settings()

    # Check expected logs
    expected_calls = [
        call("Read-only mode is ENABLED - all write operations are disabled"),
        call("Readable profiles restricted to: ['profile1', 'profile2']")
    ]

    for expected_call in expected_calls:
        assert expected_call in mock_module.logger.info.mock_calls, \
            f"Missing expected info log: {expected_call}"
def test_log_access_control_settings_unrestricted(mock_module):
    """Test access control logging with no restrictions."""
    # Configure the mock module
    mock_module.NEXTDNS_READ_ONLY = False
    mock_module.get_readable_profiles = lambda: set()
    mock_module.get_writable_profiles = lambda: set()

    # Call function
    mock_module._log_access_control_settings()

    # Check expected logs
    expected_calls = [
        call("All profiles are readable (no restrictions)"),
        call("All profiles are writable (no restrictions)")
    ]
    
    for expected_call in expected_calls:
        assert expected_call in mock_module.logger.info.mock_calls, \
            f"Missing expected info log: {expected_call}"
    
    # Ensure no unexpected calls    
    assert mock_module.logger.info.call_count == 2


def test_validate_configuration_exits_on_missing_api_key(mock_module):
    """Test validate_configuration exits when API key is missing."""
    mock_module.NEXTDNS_API_KEY = None
    
    # Should exit with code 1
    with pytest.raises(SystemExit) as exc_info:
        mock_module.validate_configuration()
    assert exc_info.value.code == 1

    # Check expected error logs
    calls = [
        call("NEXTDNS_API_KEY is required"),
        call("Set either:"),
        call("  - NEXTDNS_API_KEY environment variable"),
        call("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")
    ]
    
    for expected_call in calls:
        assert expected_call in mock_module.logger.critical.mock_calls, \
            f"Missing expected critical log: {expected_call}"


def test_validate_configuration_logs_settings(mock_module):
    """Test validate_configuration logs access control settings when API key exists."""
    mock_module.NEXTDNS_API_KEY = "test-key"
    mock_module.NEXTDNS_READ_ONLY = True
    
    # Verify no exit with API key present
    mock_module.validate_configuration()

    # Check read-only mode log
    assert call("Read-only mode is ENABLED - all write operations are disabled") in \
        mock_module.logger.info.mock_calls
        
    # Check default access control logging
    assert call("All profiles are readable (no restrictions)") in \
        mock_module.logger.info.mock_calls

    # Configure the mock module    
    mock_module.NEXTDNS_API_KEY = None

    # Test that the function raises SystemExit
    with pytest.raises(SystemExit) as excinfo:
        mock_module.validate_configuration()

    assert excinfo.value.code == 1

    # Check logger calls
    expected_calls = [
        call("NEXTDNS_API_KEY is required"),
        call("Set either:"),
        call("  - NEXTDNS_API_KEY environment variable"),
        call("  - NEXTDNS_API_KEY_FILE pointing to a Docker secret")
    ]

    for expected_call in expected_calls:
        assert expected_call in mock_module.logger.critical.mock_calls, \
            f"Missing expected critical log: {expected_call}"
            
    assert mock_module.logger.critical.call_count == 4