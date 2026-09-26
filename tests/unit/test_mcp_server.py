"""Unit tests for MCP server creation (openapi.py create_mcp_server)."""

import logging

import pytest
from fastmcp import FastMCP

from nextdns_mcp.openapi import StripExtraFieldsMiddleware, create_mcp_server


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
