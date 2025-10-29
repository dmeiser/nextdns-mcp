"""Integration tests for config validation - actually executes the code."""
import os
import pytest
import sys
from unittest.mock import patch


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    # Clear all environment variables
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


def test_validate_configuration_calls_sys_exit(clean_env):
    """Test that validate_configuration actually calls sys.exit when no API key."""
    # No API key set
    
    # Need to reload the module to get fresh state
    import importlib
    if "nextdns_mcp.config" in sys.modules:
        del sys.modules["nextdns_mcp.config"]
    
    import nextdns_mcp.config as config
    
    # Mock sys.exit to capture the call
    with patch.object(sys, 'exit') as mock_exit:
        config.validate_configuration()
        mock_exit.assert_called_once_with(1)


def test_log_api_key_error_calls_logger(clean_env):
    """Test that _log_api_key_error actually logs messages."""
    # Import fresh module
    if "nextdns_mcp.config" in sys.modules:
        del sys.modules["nextdns_mcp.config"]
    
    import nextdns_mcp.config as config
    
    # Mock logger to capture calls
    from unittest.mock import Mock
    import logging
    mock_logger = Mock(spec=logging.Logger)
    
    with patch.object(config, 'logger', mock_logger):
        config._log_api_key_error()
        
        # Verify all 4 critical messages were logged
        assert mock_logger.critical.call_count == 4
        mock_logger.critical.assert_any_call("NEXTDNS_API_KEY is required")


def test_validate_configuration_logs_access_control(clean_env):
    """Test that validate_configuration calls _log_access_control_settings."""
    clean_env("NEXTDNS_API_KEY", "test-key")
    
    # Import fresh module
    if "nextdns_mcp.config" in sys.modules:
        del sys.modules["nextdns_mcp.config"]
    
    import nextdns_mcp.config as config
    
    # Mock logger and sys.exit
    from unittest.mock import Mock
    import logging
    mock_logger = Mock(spec=logging.Logger)
    
    with patch.object(config, 'logger', mock_logger):
        config.validate_configuration()
        
        # Verify logging was called (at least the unrestricted message)
        assert mock_logger.info.call_count >= 2  # At least 2 info messages
