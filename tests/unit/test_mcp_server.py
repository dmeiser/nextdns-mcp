"""Unit tests for MCP server creation (openapi.py create_mcp_server)."""

import logging

import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import client as client_module
from nextdns_mcp.openapi import StripExtraFieldsMiddleware, create_mcp_server
from nextdns_mcp.tools import doh as doh_module


class TestCreateMcpServer:
    """Tests for the create_mcp_server() function."""

    def test_returns_named_fastmcp_instance(self, mock_api_key, monkeypatch):
        """create_mcp_server returns a configured FastMCP instance."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        result = create_mcp_server()

        assert isinstance(result, FastMCP)
        assert result.name == "NextDNS MCP Server"

    def test_registers_strip_extra_fields_middleware(self, mock_api_key, monkeypatch):
        """create_mcp_server installs the StripExtraFieldsMiddleware."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        result = create_mcp_server()

        middlewares = getattr(result, "middleware", None) or []
        assert any(isinstance(m, StripExtraFieldsMiddleware) for m in middlewares)

    @pytest.mark.asyncio
    async def test_no_tools_until_registered_by_server(self, mock_api_key, monkeypatch):
        """The server starts with no tools.

        Grouped CRUD tools are registered by server.py after create_mcp_server()
        returns, so the function itself must expose no OpenAPI-derived tools.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        result = create_mcp_server()

        tools = await result.list_tools()
        assert tools == []

    def test_logs_default_profile_when_set(self, mock_api_key, monkeypatch, caplog):
        """create_mcp_server logs the default profile when one is configured."""
        caplog.set_level(logging.INFO)
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        create_mcp_server()

        assert "Default profile: abc123" in caplog.text

    def test_no_default_profile_log(self, mock_api_key, monkeypatch, caplog):
        """create_mcp_server does not log a default profile when none is set."""
        caplog.set_level(logging.INFO)
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.delenv("NEXTDNS_DEFAULT_PROFILE", raising=False)

        create_mcp_server()

        assert "Default profile:" not in caplog.text


class TestProductionServerTools:
    """Test that the production MCP server exposes only the grouped tools."""

    @pytest.mark.asyncio
    async def test_only_grouped_tools_registered(self):
        """The production server should expose exactly 8 tools: 7 grouped tools plus dohLookup."""
        from nextdns_mcp.server import mcp_server

        tools = await mcp_server.list_tools()
        tool_names = {tool.name for tool in tools}

        expected = {
            "dohLookup",
            "manageProfiles",
            "manageSettings",
            "manageLists",
            "manageRewrites",
            "manageLogs",
            "queryAnalytics",
            "plotAnalytics",
        }

        assert tool_names == expected, f"Unexpected tools registered: {tool_names ^ expected}"

    @pytest.mark.asyncio
    async def test_no_atomic_api_tools_registered(self):
        """Atomic per-endpoint tools (e.g. listProfiles) must not be exposed.

        The server no longer generates tools from the OpenAPI spec (issue #141),
        so only the grouped CRUD tools are available.
        """
        from nextdns_mcp.server import mcp_server

        tools = await mcp_server.list_tools()
        tool_names = {tool.name for tool in tools}

        atomic_tools = {
            "listProfiles",
            "createProfile",
            "getProfile",
            "updateProfile",
            "deleteProfile",
            "getSettings",
            "updateSettings",
            "getAllowlist",
            "addToAllowlist",
            "replaceAllowlist",
        }

        assert not (tool_names & atomic_tools), f"Atomic tools still registered: {tool_names & atomic_tools}"


class TestServerLifespan:
    """Test that server lifespan shuts down long-lived HTTP clients (issue #277)."""

    @pytest.mark.asyncio
    async def test_server_lifespan_closes_http_clients(self, mock_api_key, monkeypatch):
        """Server lifespan exit must close both API and DoH clients and reset singletons."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_client", None)
        monkeypatch.setattr(doh_module, "_doh_client", None)

        server = create_mcp_server()
        api_client = client_module.get_api_client()
        doh_client = doh_module._get_doh_client()

        try:
            assert not api_client.is_closed
            assert not doh_client.is_closed

            async with server._lifespan_manager():
                pass

            assert api_client.is_closed
            assert doh_client.is_closed
            assert client_module._client is None
            assert doh_module._doh_client is None
        finally:
            if not api_client.is_closed:
                await api_client.aclose()
            if not doh_client.is_closed:
                await doh_client.aclose()

    @pytest.mark.asyncio
    async def test_real_client_session_closes_http_clients_on_disconnect(self, mock_api_key, monkeypatch):
        """A real MCP client session (the public surface) closes both clients when it ends.

        The lifecycle above drives FastMCP's private ``_lifespan_manager``; this
        one goes through ``fastmcp.Client``, so it also proves the hook is wired
        into the transport the server actually runs under.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_client", None)
        monkeypatch.setattr(doh_module, "_doh_client", None)

        server = create_mcp_server()

        async with Client(server) as mcp_client:
            await mcp_client.list_tools()
            api_client = client_module.get_api_client()
            doh_client = doh_module._get_doh_client()
            assert not api_client.is_closed
            assert not doh_client.is_closed

        assert api_client.is_closed
        assert doh_client.is_closed
        assert client_module._client is None
        assert doh_module._doh_client is None
