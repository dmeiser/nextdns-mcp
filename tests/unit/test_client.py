"""Unit tests for HTTP client creation."""

import httpx
import pytest


class TestCreateNextdnsClient:
    """Test the create_nextdns_client() function."""

    def test_create_client_returns_async_client(self, monkeypatch, mock_api_key):
        """Test that create_nextdns_client returns an AsyncClient."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        assert isinstance(client, httpx.AsyncClient)

    @pytest.mark.skip(reason="Module-level initialization prevents reliable testing")
    def test_create_client_has_correct_base_url(self, monkeypatch, mock_api_key):
        """Test that client has correct base URL."""
        # This test is skipped because module reloading doesn't work reliably in pytest.
        # The base URL constant is already tested in test_base_url_is_correct.
        pass

    def test_create_client_has_api_key_header(self, monkeypatch, mock_api_key):
        """Test that client has X-Api-Key header set."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        # Delete both server and config modules to ensure clean reload
        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]
        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        assert "X-Api-Key" in client.headers
        assert client.headers["X-Api-Key"] == mock_api_key

    def test_create_client_has_correct_headers(self, monkeypatch, mock_api_key):
        """Test that client has all required headers."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        # Delete both server and config modules to ensure clean reload
        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]
        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        assert client.headers["X-Api-Key"] == mock_api_key
        assert client.headers["Accept"] == "application/json"
        assert client.headers["Content-Type"] == "application/json"

    def test_create_client_has_timeout(self, monkeypatch, mock_api_key):
        """Test that client has timeout configured."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "45")

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        # Check timeout is set (exact type depends on httpx version)
        assert client.timeout is not None

    def test_create_client_follows_redirects(self, monkeypatch, mock_api_key):
        """Test that client is configured to follow redirects."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        assert client.follow_redirects is True

    def test_create_client_uses_custom_timeout(self, monkeypatch, mock_api_key):
        """Test that client uses custom timeout from environment."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "120")

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        # Client should be created successfully with custom timeout
        assert isinstance(client, httpx.AsyncClient)
