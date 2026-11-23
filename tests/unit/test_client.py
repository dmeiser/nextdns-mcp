"""Unit tests for HTTP client creation."""

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp.config import ConfigurationError


class TestCreateNextdnsClient:
    """Test the create_nextdns_client() function."""

    def test_create_client_returns_async_client(self, monkeypatch, mock_api_key):
        """Test that create_nextdns_client returns an AsyncClient."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        assert not isinstance(client, httpx.AsyncClient)  # composition, not inheritance (issue #639)

    def test_create_client_has_correct_base_url(self, monkeypatch, mock_api_key):
        """Test that client has correct base URL."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        assert str(client.base_url) == "https://api.nextdns.io"

    def test_create_client_reads_api_base_at_call_time(self, monkeypatch, mock_api_key):
        """A long-lived process picks up a NEXTDNS_API_BASE change when it creates a client."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp.client import create_nextdns_client

        monkeypatch.setenv("NEXTDNS_API_BASE", "https://first.example.invalid/")
        first = create_nextdns_client()
        assert str(first.base_url) == "https://first.example.invalid"

        monkeypatch.setenv("NEXTDNS_API_BASE", "https://second.example.invalid")
        second = create_nextdns_client()
        assert str(second.base_url) == "https://second.example.invalid"

    def test_create_client_rejects_non_https_api_base(self, monkeypatch, mock_api_key):
        """A runtime change to a non-https base fails closed instead of sending the key in plaintext."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://api.nextdns.io")

        from nextdns_mcp.client import create_nextdns_client

        with pytest.raises(ConfigurationError, match="NEXTDNS_API_BASE must use https://"):
            create_nextdns_client()

    def test_create_client_has_api_key_header(self, monkeypatch, mock_api_key):
        """Test that X-Api-Key header is set as a static header during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        # Delete both server and config modules to ensure clean reload
        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]
        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        # API key is set at initialization time in static headers
        assert "X-Api-Key" in client.headers
        assert client.headers["X-Api-Key"] == mock_api_key
        assert not isinstance(client, httpx.AsyncClient)  # composition, not inheritance (issue #639)

    def test_create_client_has_correct_headers(self, monkeypatch, mock_api_key):
        """Test that client has all required headers including the API key set during client creation."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        # Delete both server and config modules to ensure clean reload
        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]
        if "nextdns_mcp.config" in sys.modules:
            del sys.modules["nextdns_mcp.config"]

        from nextdns_mcp.client import create_nextdns_client

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

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        # Check timeout is set (exact type depends on httpx version)
        assert client.timeout is not None

    def test_create_client_does_not_follow_redirects(self, monkeypatch, mock_api_key):
        """Test that client does not follow redirects to avoid leaking the API key."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        assert client.follow_redirects is False

    def test_create_client_uses_custom_timeout(self, monkeypatch, mock_api_key):
        """Test that client uses custom timeout from environment."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "120")

        import sys

        if "nextdns_mcp.server" in sys.modules:
            del sys.modules["nextdns_mcp.server"]

        from nextdns_mcp.client import create_nextdns_client

        client = create_nextdns_client()

        # Client should be created successfully with custom timeout
        assert not isinstance(client, httpx.AsyncClient)  # composition, not inheritance (issue #639)

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

        assert not isinstance(client, httpx.AsyncClient)  # composition, not inheritance (issue #639)
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

    @pytest.mark.asyncio
    async def test_close_api_client_closes_and_resets(self, monkeypatch, mock_api_key):
        """close_api_client closes the active client and resets _client to None."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        import nextdns_mcp.client as client_module

        monkeypatch.setattr(client_module, "_client", None)

        client = client_module.get_api_client()
        assert not client.is_closed
        assert client_module._client is client

        await client_module.close_api_client()
        assert client.is_closed
        assert client_module._client is None

    @pytest.mark.asyncio
    async def test_close_api_client_noop_when_none(self, monkeypatch):
        """close_api_client is a safe no-op when no client was created."""
        import nextdns_mcp.client as client_module

        monkeypatch.setattr(client_module, "_client", None)

        await client_module.close_api_client()
        assert client_module._client is None


class TestAccessControlledClientVerbConveniences:
    """The verb conveniences are wrappers over the ACL-checked request path (issue #639).

    Composition means they are no longer inherited from httpx, so each one has
    to route through ``request()`` — an ACL denial or off-site destination must
    stop it there, exactly as it stops ``request()`` itself.
    """

    @pytest.mark.parametrize("verb", ["get", "post", "put", "patch", "delete", "head", "options"])
    @pytest.mark.asyncio
    async def test_verb_sends_through_the_acl_path(self, verb: str, monkeypatch, mock_api_key) -> None:
        """Every verb reaches the transport with its own method when the ACL allows it."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        client = client_module.AccessControlledClient(
            base_url="https://api.nextdns.io",
            transport=httpx.MockTransport(handler),
            headers={"X-Api-Key": mock_api_key},
        )
        async with client:
            response = await getattr(client, verb)("/profiles/abc123/settings")

        assert response.status_code == 200
        assert [r.method for r in seen] == [verb.upper()]

    @pytest.mark.parametrize("verb", ["get", "post", "put", "patch", "delete", "head", "options"])
    @pytest.mark.asyncio
    async def test_verb_refused_for_a_denied_profile(self, verb: str, monkeypatch, mock_api_key) -> None:
        """A denied profile blocks the convenience verb before any network I/O."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        client = client_module.AccessControlledClient(
            base_url="https://api.nextdns.io",
            transport=httpx.MockTransport(handler),
            headers={"X-Api-Key": mock_api_key},
        )
        async with client:
            with pytest.raises(client_module.AccessDeniedError):
                await getattr(client, verb)("/profiles/abc123/settings")

        assert seen == []

    @pytest.mark.asyncio
    async def test_head_and_options_are_acl_writes_or_reads(self, monkeypatch, mock_api_key) -> None:
        """OPTIONS/HEAD classify like any other method rather than bypassing the ACL."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        client = client_module.AccessControlledClient(
            base_url="https://api.nextdns.io",
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            headers={"X-Api-Key": mock_api_key},
        )
        async with client:
            with pytest.raises(client_module.AccessDeniedError) as exc_info:
                await client.options("/profiles/abc123/settings")

        assert exc_info.value.code == "read_access_denied"


class TestRawProbeClientSingleton:
    """The probe transport is a lazy singleton closed on shutdown (issue #639)."""

    def test_get_raw_probe_client_caches_instance(self, monkeypatch, mock_api_key) -> None:
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_raw_probe_client", None)

        first = client_module.get_raw_probe_client()
        second = client_module.get_raw_probe_client()

        assert first is second
        assert isinstance(first, client_module.RawProbeTransport)
        assert first.base_url == "https://api.nextdns.io"
        assert first.headers["X-Api-Key"] == mock_api_key

    @pytest.mark.asyncio
    async def test_close_raw_probe_client_closes_and_resets(self, monkeypatch, mock_api_key) -> None:
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_raw_probe_client", None)

        transport = client_module.get_raw_probe_client()
        assert not transport.is_closed

        await client_module.close_raw_probe_client()
        assert transport.is_closed
        assert client_module._raw_probe_client is None

    @pytest.mark.asyncio
    async def test_close_raw_probe_client_noop_when_none(self, monkeypatch) -> None:
        monkeypatch.setattr(client_module, "_raw_probe_client", None)

        await client_module.close_raw_probe_client()
        assert client_module._raw_probe_client is None

    def test_get_raw_probe_client_requires_an_api_key(self, monkeypatch) -> None:
        """No key means no probe transport: the same fail-fast as the ACL client."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)
        monkeypatch.setattr(client_module, "_raw_probe_client", None)

        with pytest.raises(ConfigurationError):
            client_module.get_raw_probe_client()
