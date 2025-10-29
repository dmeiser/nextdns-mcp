"""Test API key validation and error handling."""
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def patch_env(monkeypatch):
    """Fixture to patch environment for each test."""
    # Clear all environment variables for clean test
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    
    # Return monkeypatch.setenv for test use
    return monkeypatch.setenv


def test_api_url():
    """Test API base URL constant."""
    import nextdns_mcp.config as config
    assert config.NEXTDNS_BASE_URL == "https://api.nextdns.io"


def test_default_api_key_empty(patch_env):
    """Test empty API key."""
    import nextdns_mcp.config as config
    assert config.get_api_key() is None


def test_api_key_from_env(patch_env):
    """Test API key from environment variable."""
    patch_env("NEXTDNS_API_KEY", "test-key")
    import nextdns_mcp.config as config
    assert config.get_api_key() == "test-key"


def test_api_key_from_file(patch_env, tmp_path):
    """Test API key from file."""
    key_file = tmp_path / "api-key"
    key_file.write_text("test-key-from-file")
    patch_env("NEXTDNS_API_KEY_FILE", str(key_file))
    import nextdns_mcp.config as config
    assert config.get_api_key() == "test-key-from-file"


def test_api_key_file_not_found(patch_env):
    """Test missing API key file."""
    patch_env("NEXTDNS_API_KEY_FILE", "/nonexistent/file")
    import nextdns_mcp.config as config
    assert config.get_api_key() is None


def test_default_http_timeout(patch_env):
    """Test default HTTP timeout."""
    import nextdns_mcp.config as config
    assert config.get_http_timeout() == 30.0


def test_custom_http_timeout(patch_env):
    """Test custom HTTP timeout."""
    patch_env("NEXTDNS_HTTP_TIMEOUT", "60")
    import nextdns_mcp.config as config
    assert config.get_http_timeout() == 60.0


def test_default_profile_empty(patch_env):
    """Test empty default profile."""
    import nextdns_mcp.config as config
    assert config.get_default_profile() is None


def test_custom_default_profile(patch_env):
    """Test custom default profile."""
    patch_env("NEXTDNS_DEFAULT_PROFILE", "test-profile")
    import nextdns_mcp.config as config
    assert config.get_default_profile() == "test-profile"


def test_read_only_mode_default(patch_env):
    """Test default read-only mode."""
    import nextdns_mcp.config as config
    assert not config.is_read_only()


def test_read_only_mode_enabled(patch_env):
    """Test enabling read-only mode."""
    patch_env("NEXTDNS_READ_ONLY", "true")
    import nextdns_mcp.config as config
    assert config.is_read_only()


def test_globally_allowed_operations():
    """Test operations that bypass access control."""
    import nextdns_mcp.config as config
    assert "listProfiles" in config.GLOBALLY_ALLOWED_OPERATIONS
    assert "dohLookup" in config.GLOBALLY_ALLOWED_OPERATIONS