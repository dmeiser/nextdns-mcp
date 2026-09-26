"""Unit tests for HTTP client creation."""

import httpx
import pytest

from nextdns_mcp.config import ConfigurationError


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

    def test_create_client_has_correct_base_url(self, monkeypatch, mock_api_key):
        """Test that client has correct base URL."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        assert str(client.base_url) == "https://api.nextdns.io"

    def test_create_client_has_api_key_header(self, monkeypatch, mock_api_key):
        """Test that X-Api-Key header is set as a static header during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        # Delete both server and config modules to ensure clean reload
        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]
        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        # API key is set at initialization time in static headers
        assert "X-Api-Key" in client.headers
        assert client.headers["X-Api-Key"] == mock_api_key
        assert isinstance(client, httpx.AsyncClient)

    def test_create_client_has_correct_headers(self, monkeypatch, mock_api_key):
        """Test that client has all required headers including the API key set during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        # Delete both server and config modules to ensure clean reload
        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]
        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        # API key is set at initialization in static headers
        assert "X-Api-Key" in client.headers
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

    def test_create_client_does_not_follow_redirects(self, monkeypatch, mock_api_key):
        """Test that client does not follow redirects to avoid leaking the API key."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.server import create_nextdns_client

        client = create_nextdns_client()

        assert client.follow_redirects is False

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

    def test_create_client_raises_configuration_error_on_invalid_timeout(self, monkeypatch, mock_api_key):
        """Regression test for #163: invalid NEXTDNS_HTTP_TIMEOUT raises ConfigurationError during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "not-a-number")

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        err = exc_info.value
        assert "NEXTDNS_HTTP_TIMEOUT" in str(err)
        assert "'not-a-number'" in str(err)
        assert "Expected a positive number of seconds." in str(err)
        # Verify no bare ValueError traceback from deep in client construction
        assert err.__cause__ is None
        assert type(err) is ConfigurationError

    def test_create_client_raises_configuration_error_on_empty_timeout(self, monkeypatch, mock_api_key):
        """Regression test for #163: empty NEXTDNS_HTTP_TIMEOUT raises ConfigurationError during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "")

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        err = exc_info.value
        assert "NEXTDNS_HTTP_TIMEOUT" in str(err)
        assert "''" in str(err)
        assert "Expected a positive number of seconds." in str(err)
        assert err.__cause__ is None

    def test_create_client_raises_configuration_error_on_boundary_zero_timeout(self, monkeypatch, mock_api_key):
        """Regression test for #163: boundary zero timeout raises ConfigurationError during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "0")

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        assert "NEXTDNS_HTTP_TIMEOUT" in str(exc_info.value)
        assert "'0'" in str(exc_info.value)

    def test_create_client_raises_configuration_error_on_boundary_negative_timeout(self, monkeypatch, mock_api_key):
        """Regression test for #163: boundary negative timeout raises ConfigurationError during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "-5")

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        assert "NEXTDNS_HTTP_TIMEOUT" in str(exc_info.value)
        assert "'-5'" in str(exc_info.value)

    def test_get_api_client_raises_configuration_error_on_invalid_timeout(self, monkeypatch, mock_api_key):
        """Regression test for #163: get_api_client raises ConfigurationError when timeout is invalid."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "bad-timeout")

        import nextdns_mcp.client as client_module

        monkeypatch.setattr(client_module, "_client", None)

        with pytest.raises(ConfigurationError) as exc_info:
            client_module.get_api_client()

        assert "NEXTDNS_HTTP_TIMEOUT" in str(exc_info.value)
        assert "'bad-timeout'" in str(exc_info.value)

    def test_create_client_raises_configuration_error_on_absent_api_key(self, monkeypatch):
        """Regression test for #185: create_nextdns_client raises ConfigurationError when key is absent."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        assert "NEXTDNS_API_KEY is required" in str(exc_info.value)
        assert issubclass(ConfigurationError, ValueError)

    def test_create_client_raises_configuration_error_on_empty_api_key(self, monkeypatch):
        """Regression test for #185: create_nextdns_client raises ConfigurationError when key is empty string."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "")
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        assert "NEXTDNS_API_KEY is required" in str(exc_info.value)

    def test_create_client_raises_configuration_error_on_whitespace_api_key(self, monkeypatch):
        """Regression test for #185: create_nextdns_client raises ConfigurationError when key is only whitespace."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "   ")
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError) as exc_info:
            create_nextdns_client()

        assert "NEXTDNS_API_KEY is required" in str(exc_info.value)

    def test_create_client_with_valid_key_constructs_normally(self, monkeypatch, mock_api_key):
        """Regression test for #185: create_nextdns_client constructs normally when key is valid."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        assert isinstance(client, httpx.AsyncClient)
        assert client.headers["X-Api-Key"] == mock_api_key

    def test_get_api_client_raises_configuration_error_on_absent_api_key(self, monkeypatch):
        """Regression test for #185: get_api_client raises ConfigurationError when key is absent."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        import nextdns_mcp.client as client_module

        monkeypatch.setattr(client_module, "_client", None)

        with pytest.raises(ConfigurationError) as exc_info:
            client_module.get_api_client()

        assert "NEXTDNS_API_KEY is required" in str(exc_info.value)

    def test_get_api_client_raises_configuration_error_on_empty_api_key(self, monkeypatch):
        """Regression test for #185: get_api_client raises ConfigurationError when key is empty."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "")
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        import nextdns_mcp.client as client_module

        monkeypatch.setattr(client_module, "_client", None)

        with pytest.raises(ConfigurationError) as exc_info:
            client_module.get_api_client()

        assert "NEXTDNS_API_KEY is required" in str(exc_info.value)

    def test_module_getattr_api_client_raises_configuration_error_on_absent_api_key(self, monkeypatch):
        """Regression test for #185: client.api_client attribute raises ConfigurationError when key is absent."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        import nextdns_mcp.client as client_module

        client_module.__dict__.pop("api_client", None)
        monkeypatch.setattr(client_module, "_client", None)

        with pytest.raises(ConfigurationError) as exc_info:
            _ = client_module.api_client

        assert "NEXTDNS_API_KEY is required" in str(exc_info.value)
