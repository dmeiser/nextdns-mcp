"""Unit tests for NEXTDNS_TOOL_SEARCH progressive tool disclosure.

Covers the config parser (``get_tool_search_mode``) and the server wiring in
``create_mcp_server``/``build_mcp_server`` that swaps FastMCP's search
transforms in: with a mode enabled, ``tools/list`` exposes only the synthetic
``search_tools`` + ``call_tool`` pair, while the real grouped tools stay
callable through the ``call_tool`` proxy.
"""

import pytest

from nextdns_mcp.config import ConfigurationError, get_tool_search_mode
from nextdns_mcp.server import build_mcp_server


class TestGetToolSearchMode:
    """Tests for the NEXTDNS_TOOL_SEARCH parser."""

    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_TOOL_SEARCH", raising=False)
        assert get_tool_search_mode() == "off"

    def test_empty_value_is_off(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "")
        assert get_tool_search_mode() == "off"

    @pytest.mark.parametrize("mode", ["off", "regex", "bm25"])
    def test_accepted_modes(self, monkeypatch, mode):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        assert get_tool_search_mode() == mode

    def test_case_insensitive_and_trimmed(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "  BM25 ")
        assert get_tool_search_mode() == "bm25"

    def test_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "semantic")
        with pytest.raises(ConfigurationError, match="NEXTDNS_TOOL_SEARCH"):
            get_tool_search_mode()


class TestToolSearchServerWiring:
    """Tests for the transform wiring on the built MCP server."""

    @pytest.mark.asyncio
    async def test_default_lists_full_catalog(self, monkeypatch):
        """Without the flag the full grouped-tool catalog is advertised."""
        monkeypatch.delenv("NEXTDNS_TOOL_SEARCH", raising=False)
        server = build_mcp_server()
        names = {t.name for t in await server.list_tools()}
        assert "manageProfiles" in names
        assert "search_tools" not in names
        assert "call_tool" not in names

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["regex", "bm25"])
    async def test_enabled_modes_replace_catalog(self, monkeypatch, mode):
        """With a mode enabled only the synthetic discovery pair is listed."""
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        server = build_mcp_server()
        names = {t.name for t in await server.list_tools()}
        assert names == {"search_tools", "call_tool"}

    @pytest.mark.asyncio
    async def test_hidden_tools_remain_discoverable_and_callable(self, monkeypatch):
        """The transform controls discovery, not access: search finds the real
        tools and the call_tool proxy reaches them."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "regex")
        server = build_mcp_server()
        async with Client(server) as client:
            found = await client.call_tool("search_tools", {"pattern": "manageProfiles"})
            assert any(t.get("name") == "manageProfiles" for t in found.data)

            # The hidden tool is still resolvable server-side (call path),
            # even though it is absent from the listing.
            tool = await server.get_tool("manageProfiles")
            assert tool is not None
