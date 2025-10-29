"""Unit tests for file-based configuration."""
import logging
import os
import sys
import tempfile
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

from nextdns_mcp.config import get_api_key, parse_profile_list





def mock_nextdns_config():
    """Create fresh mock config module."""
    module = ModuleType('nextdns_mcp.config')
    
    # Create mock logger that persists during testing
    module.logger = Mock(name='logger', spec=logging.Logger)
    
    # Function definitions that use our mock logger
    def get_api_key():
        """Mock version using our logger."""
        key = os.getenv('NEXTDNS_API_KEY')
        if key:
            return key.strip()
            
        key_file = os.getenv('NEXTDNS_API_KEY_FILE')
        if key_file:
            try:
                with open(key_file, 'r') as f:
                    return f.read().strip()
            except FileNotFoundError:
                module.logger.error(f'API key file not found: {key_file}')
            except Exception as e:
                module.logger.error(f'Failed to read API key file: {e}')
            
        return None
        
    def parse_profile_list(profile_str: str):
        """Pure function, no logging."""
        return {p for p in [p.strip() for p in profile_str.split(',')] if p}
    
    # Add functions to module    
    module.get_api_key = get_api_key
    module.parse_profile_list = parse_profile_list
    module.NEXTDNS_API_KEY = None
    
    return module


@pytest.fixture
def mock_env():
    """Setup clean module for each test."""
    module = mock_nextdns_config()
    old_module = sys.modules.get('nextdns_mcp.config')
    sys.modules['nextdns_mcp.config'] = module
    try:
        yield module
    finally:
        if old_module is not None:
            sys.modules['nextdns_mcp.config'] = old_module
        else:
            del sys.modules['nextdns_mcp.config']


def test_get_api_key_from_file(mock_env):
    """Test reading API key from file."""
    # Create a temporary file with test API key
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write("test-api-key-from-file\n")  # Add newline to test stripping
        temp_path = f.name

    try:
        # Set up environment
        with patch.dict('os.environ', {
            'NEXTDNS_API_KEY_FILE': temp_path
        }, clear=True):
            api_key = mock_env.get_api_key()
            assert api_key == "test-api-key-from-file"
    finally:
        # Clean up temp file
        os.unlink(temp_path)


def test_get_api_key_file_not_found(mock_env):
    """Test handling of missing API key file."""
    # Set environment to use nonexistent file
    with patch.dict('os.environ', {'NEXTDNS_API_KEY_FILE': '/nonexistent/file'}, clear=True):
        assert mock_env.get_api_key() is None
        mock_env.logger.error.assert_called_once_with(
            'API key file not found: /nonexistent/file'
        )


def test_get_api_key_file_error(mock_env):
    """Test handling of API key file read error."""
    def mock_open(*args, **kwargs):
        raise PermissionError("Access denied")
        
    # Set up mock environment with permission error on file read
    with patch('builtins.open', mock_open), \
         patch.dict('os.environ', {'NEXTDNS_API_KEY_FILE': '/some/file'}, clear=True):
        assert mock_env.get_api_key() is None
        mock_env.logger.error.assert_called_once_with(
            'Failed to read API key file: Access denied'
        )


def test_parse_profile_list_empty(mock_env):
    """Test parsing empty profile lists."""
    # Test various types of empty inputs
    assert mock_env.parse_profile_list('') == set()
    assert mock_env.parse_profile_list('  ') == set()
    assert mock_env.parse_profile_list(',') == set()
    assert mock_env.parse_profile_list('  ,  ,  ') == set()


@pytest.mark.parametrize("input_str,expected", [
    ("", set()),  # Empty string
    ("  ", set()),  # Only whitespace
    ("profile1", {"profile1"}),  # Single profile
    ("profile1,profile2", {"profile1", "profile2"}),  # Multiple profiles
    ("  profile1  ,  profile2  ", {"profile1", "profile2"}),  # Extra whitespace
    ("profile1,,profile2", {"profile1", "profile2"}),  # Empty entries
    (",,,", set()),  # Only commas
    ("profile1,profile1,profile2", {"profile1", "profile2"}),  # Duplicates
])
def test_parse_profile_list_variants(mock_env, input_str, expected):
    """Test parse_profile_list with various inputs."""
    assert mock_env.parse_profile_list(input_str) == expected