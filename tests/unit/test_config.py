"""Unit tests for configuration and API key loading."""

import tempfile
from pathlib import Path
from unittest.mock import patch

# We need to test the functions before the module initializes
# So we'll import them individually after setting up the environment


class TestGetApiKey:
    """Test the get_api_key() function."""

    def test_get_api_key_from_env_var(self, monkeypatch, mock_api_key):
        """Test loading API key from environment variable."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        # Import after setting env var
        from nextdns_mcp.config import get_api_key

        result = get_api_key()
        assert result == mock_api_key

    def test_get_api_key_from_file(self, monkeypatch, temp_api_key_file, mock_api_key):
        """Test loading API key from Docker secret file."""
        # Remove direct env var
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        # Set file path
        monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(temp_api_key_file))

        from nextdns_mcp.config import get_api_key

        result = get_api_key()
        assert result == mock_api_key

    def test_get_api_key_env_var_takes_precedence(self, monkeypatch, temp_api_key_file):
        """Test that environment variable takes precedence over file."""
        env_key = "env_api_key"
        monkeypatch.setenv("NEXTDNS_API_KEY", env_key)
        monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(temp_api_key_file))

        from nextdns_mcp.config import get_api_key

        result = get_api_key()
        assert result == env_key

    def test_get_api_key_file_not_found(self, monkeypatch, caplog):
        """Test handling of missing API key file."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.setenv("NEXTDNS_API_KEY_FILE", "/nonexistent/file.txt")

        from nextdns_mcp.config import get_api_key

        result = get_api_key()
        assert result is None

        # Check error message was logged
        assert "API key file not found" in caplog.text

    def test_get_api_key_file_read_error(self, monkeypatch, caplog):
        """Test handling of file read errors."""
        # Create a directory (can't be read as a file)
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
            monkeypatch.setenv("NEXTDNS_API_KEY_FILE", tmpdir)

            from nextdns_mcp.config import get_api_key

            result = get_api_key()
            assert result is None

            # Check error message was logged
            assert "Failed to read API key file" in caplog.text

    def test_get_api_key_none_when_not_set(self, monkeypatch):
        """Test that None is returned when no API key is configured."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        from nextdns_mcp.config import get_api_key

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

            from nextdns_mcp.config import get_api_key

            result = get_api_key()
            assert result == "test_key_with_spaces"
        finally:
            Path(temp_path).unlink(missing_ok=True)


class TestEnvironmentConfiguration:
    """Test environment variable configuration."""

    def test_default_timeout_value(self, monkeypatch):
        """Test default timeout is 30 seconds."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "dummy_key")
        monkeypatch.delenv("NEXTDNS_HTTP_TIMEOUT", raising=False)

        # Clean import of config
        import sys

        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp import config

        assert config.get_http_timeout() == 30.0

    def test_custom_timeout_value(self, monkeypatch):
        """Test custom timeout can be set."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "dummy_key")
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "60")

        with patch.dict("os.environ", {"NEXTDNS_API_KEY": "dummy_key", "NEXTDNS_HTTP_TIMEOUT": "60"}, clear=True):
            # Clean import of config
            import importlib
            import sys

            if "nextdns_mcp.config" in sys.modules:
                del sys.modules["nextdns_mcp.config"]

            # Import and reload to ensure fresh state
            import nextdns_mcp.config

            importlib.reload(nextdns_mcp.config)
            assert nextdns_mcp.config.get_http_timeout() == 60.0

    def test_base_url_is_correct(self, monkeypatch, mock_api_key):
        """Test that base URL is set correctly."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp import config

        assert config.NEXTDNS_BASE_URL == "https://api.nextdns.io"
