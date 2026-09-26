"""Regression tests for lazy factories replacing module-level singletons.

Issue #150: api_client and mcp_server were built at import time and
logging.basicConfig ran at import. These tests pin the lazy behavior:
importing the modules must not build the client or server, and the
factories must cache and lazily expose the singletons.
"""

import importlib
import logging
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import config, server


class TestLazyApiClient:
    """The API client must be created lazily, not at import time."""

    def test_get_api_client_caches_instance(self, monkeypatch, mock_api_key):
        """First call creates the client; subsequent calls return the same object."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_client", None)

        first = client_module.get_api_client()
        second = client_module.get_api_client()

        assert first is second

    def test_module_getattr_exposes_api_client(self, monkeypatch, mock_api_key):
        """The backward-compatible client.api_client attribute is served lazily."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        # Clear any real api_client entry left in the module namespace by other
        # tests' monkeypatch.setattr(..., "api_client", ...) teardowns. Pop the
        # module dict directly: monkeypatch.delattr would raise AttributeError
        # here because its hasattr probe is answered by the module __getattr__
        # (which lazily builds a client) while there is no real dict entry to
        # delete, making this test order-dependent.
        client_module.__dict__.pop("api_client", None)
        monkeypatch.setattr(client_module, "_client", None)
        sentinel = object()
        monkeypatch.setattr(client_module, "create_nextdns_client", lambda: sentinel)

        # The module attribute resolves to the same instance the factory serves.
        assert client_module.api_client is sentinel
        assert client_module.get_api_client() is sentinel

    def test_module_getattr_unknown_name_raises(self):
        """Unknown attribute names still raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = client_module.does_not_exist_xyz

    def test_api_key_read_at_creation_not_import(self, monkeypatch):
        """The API key is captured when the client is created, not at import."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.setattr(client_module, "_client", None)

        # Calling without a key must fail fast with ConfigurationError
        with pytest.raises(config.ConfigurationError):
            client_module.get_api_client()

        # Recreate with a key set later: header must be present
        monkeypatch.setattr(client_module, "_client", None)
        monkeypatch.setenv("NEXTDNS_API_KEY", "late_key")
        keyed_client = client_module.get_api_client()
        assert keyed_client.headers["X-Api-Key"] == "late_key"


class TestLazyMcpServer:
    """The MCP server must be created lazily, not at import time."""

    def test_get_mcp_server_caches_instance(self, monkeypatch, mock_api_key):
        """First call builds the server; subsequent calls return the same object."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(server, "_mcp_server", None)

        first = server.get_mcp_server()
        second = server.get_mcp_server()

        assert first is second
        assert first is not None
        assert hasattr(first, "run")

    def test_module_getattr_exposes_mcp_server(self, monkeypatch, mock_api_key):
        """The backward-compatible mcp_server and mcp attributes are served lazily."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(server, "_mcp_server", None)

        assert server.mcp_server is server.get_mcp_server()
        assert server.mcp is server.get_mcp_server()

    def test_module_getattr_exposes_api_client(self, monkeypatch, mock_api_key):
        """server.api_client resolves to the same shared instance as the client factory."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_client", None)
        sentinel = object()
        monkeypatch.setattr(client_module, "create_nextdns_client", lambda: sentinel)

        assert server.api_client is sentinel
        assert client_module.get_api_client() is sentinel

    def test_module_getattr_unknown_name_raises(self):
        """Unknown attribute names still raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = server.does_not_exist_xyz


class TestImportHasNoSideEffects:
    """A fresh import of the modules must not construct the singletons (issue #150)."""

    def test_reimport_does_not_construct_singletons(self, monkeypatch, request):
        """Re-importing with constructors instrumented must not invoke them."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        constructions: list[str] = []

        def record_client_init(*args: object, **kwargs: object) -> None:
            constructions.append("httpx.AsyncClient")

        monkeypatch.setattr(httpx.AsyncClient, "__init__", record_client_init)

        original_server = importlib.import_module("nextdns_mcp.server")
        original_client = importlib.import_module("nextdns_mcp.client")
        sys.modules.pop("nextdns_mcp.server", None)
        sys.modules.pop("nextdns_mcp.client", None)

        def restore_modules() -> None:
            sys.modules["nextdns_mcp.server"] = original_server
            sys.modules["nextdns_mcp.client"] = original_client

        request.addfinalizer(restore_modules)

        fresh_server = importlib.import_module("nextdns_mcp.server")
        fresh_client = importlib.import_module("nextdns_mcp.client")

        assert constructions == []
        assert fresh_client._client is None
        assert fresh_server._mcp_server is None


class TestConfigureLogging:
    """Root logging must be configurable without import-time side effects."""

    def test_configure_logging_calls_basic_config(self):
        """configure_logging configures root logging via basicConfig."""
        with patch("logging.basicConfig") as mock_basic_config:
            config.configure_logging()

        mock_basic_config.assert_called_once()
        assert mock_basic_config.call_args.kwargs["level"] == logging.INFO


class TestRunServer:
    """_run_server wires validation to a typed exception and then runs."""

    def test_run_server_raises_without_api_key(self, monkeypatch):
        """Missing API key raises MissingApiKeyError before the server runs."""
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)

        with pytest.raises(config.MissingApiKeyError):
            server._run_server()

    def test_run_server_validates_then_runs(self, monkeypatch, mock_api_key):
        """With a valid key, configuration is validated and the server runs."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        ran = []

        def fake_run(**kwargs):
            ran.append(kwargs)

        with (
            patch.object(server, "configure_logging") as mock_configure,
            patch.object(server, "validate_configuration") as mock_validate,
            patch.object(server, "get_mcp_server") as mock_get_server,
        ):
            mock_get_server.return_value = SimpleNamespace(run=fake_run)
            server._run_server()

        mock_configure.assert_called_once()
        mock_validate.assert_called_once()
        assert ran == [{}]

    def test_run_server_raises_on_invalid_timeout(self, monkeypatch, mock_api_key):
        """Invalid NEXTDNS_HTTP_TIMEOUT raises ConfigurationError when running server."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "invalid-timeout")

        with pytest.raises(config.ConfigurationError) as exc_info:
            server._run_server()

        assert "NEXTDNS_HTTP_TIMEOUT" in str(exc_info.value)
        assert "'invalid-timeout'" in str(exc_info.value)
