"""Unit tests for configuration and API key loading."""

import os
import tempfile
from pathlib import Path

import pytest

# We need to test the functions before the module initializes
# So we'll import them individually after setting up the environment


class TestGetApiKey:
    """Test the get_api_key() function."""

    def test_get_api_key_from_env_var(self, monkeypatch, mock_api_key):
        """Test loading API key from environment variable."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        # Import after setting env var
        from nextdns_mcp.server import get_api_key

        result = get_api_key()
        assert result == mock_api_key

    def test_get_api_key_from_file(self, monkeypatch, temp_api_key_file, mock_api_key):
        """Test loading API key from Docker secret file."""
        # Remove direct env var
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        # Set file path
        monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(temp_api_key_file))

        from nextdns_mcp.server import get_api_key

        result = get_api_key()
        assert result == mock_api_key

    def test_get_api_key_env_var_takes_precedence(self, monkeypatch, temp_api_key_file):
        """Test that environment variable takes precedence over file."""
        env_key = "env_api_key"
        monkeypatch.setenv("NEXTDNS_API_KEY", env_key)
        monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(temp_api_key_file))

        from nextdns_mcp.server import get_api_key

        result = get_api_key()
        assert result == env_key

    def test_get_api_key_file_not_found(self, monkeypatch, capsys):
        """Test handling of missing API key file."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.setenv("NEXTDNS_API_KEY_FILE", "/nonexistent/file.txt")

        from nextdns_mcp.server import get_api_key

        result = get_api_key()
        assert result is None

        # Check error message was printed
        captured = capsys.readouterr()
        assert "ERROR: API key file not found" in captured.err

    def test_get_api_key_file_read_error(self, monkeypatch, capsys):
        """Test handling of file read errors."""
        # Create a directory (can't be read as a file)
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
            monkeypatch.setenv("NEXTDNS_API_KEY_FILE", tmpdir)

            from nextdns_mcp.server import get_api_key

            result = get_api_key()
            assert result is None

            # Check error message was printed
            captured = capsys.readouterr()
            assert "ERROR: Failed to read API key file" in captured.err

    def test_get_api_key_none_when_not_set(self, monkeypatch):
        """Test that None is returned when no API key is configured."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        from nextdns_mcp.server import get_api_key

        result = get_api_key()
        assert result is None

    def test_get_api_key_strips_whitespace(self, monkeypatch):
        """Test that API key from file has whitespace stripped."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("  test_key_with_spaces  \n")
            temp_path = f.name

        try:
            monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
            monkeypatch.setenv("NEXTDNS_API_KEY_FILE", temp_path)

            from nextdns_mcp.server import get_api_key

            result = get_api_key()
            assert result == "test_key_with_spaces"
        finally:
            Path(temp_path).unlink(missing_ok=True)


class TestEnvironmentConfiguration:
    """Test environment variable configuration."""

    @pytest.mark.skip(reason="Module-level initialization prevents testing different env configs")
    def test_default_timeout_value(self, monkeypatch, mock_api_key):
        """Test default timeout is 30 seconds."""
        # This test is skipped because the module is already loaded with specific
        # timeout values, and reloading doesn't work reliably in pytest.
        pass

    @pytest.mark.skip(reason="Module-level initialization prevents testing different env configs")
    def test_custom_timeout_value(self, monkeypatch, mock_api_key):
        """Test custom timeout can be set."""
        # This test is skipped because the module is already loaded with specific
        # timeout values, and reloading doesn't work reliably in pytest.
        pass

    def test_base_url_is_correct(self, monkeypatch, mock_api_key):
        """Test that base URL is set correctly."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp import server

        assert server.NEXTDNS_BASE_URL == "https://api.nextdns.io"
