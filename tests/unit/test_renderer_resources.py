"""Unit tests for the composite tools' Prefab renderer resource (issue #751).

``plotAnalytics`` and ``overviewDashboard`` advertise a
``ui://prefab/renderer/<digest>/renderer.html`` resource that FastMCP
synthesizes on demand from the tools a listing returns — and the ``bm25``
search transform hides every real tool from that listing, so under the default
disclosure mode ``resources/list`` came back empty and ``resources/read`` on
the advertised URI failed with ``Resource not found``.
``build_mcp_server`` registers the renderer explicitly; these tests pin that
listing and reading work under every disclosure mode, that the disclosed tool
catalog is untouched, and that the registered URI is the one the tools
advertise on the wire.
"""

import re

import pytest
from fastmcp import Client
from fastmcp.utilities.mime import UI_MIME_TYPE

from nextdns_mcp.server import build_mcp_server

_RENDERER_URI = "ui://nextdns/renderer.html"


def _renderer_uris(uris: list[str]) -> list[str]:
    return [uri for uri in uris if uri == _RENDERER_URI]


class TestCompositeRendererResource:
    """The renderer resource must list and read under both disclosure modes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bm25", "off", None])
    async def test_renderer_is_listed_once_and_readable(self, mock_api_key, monkeypatch, mode):
        """One renderer resource is listed and its URI reads as renderer HTML.

        ``None`` exercises the unset default, which resolves to ``bm25`` —
        the mode the defect was reported under.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        if mode is None:
            monkeypatch.delenv("NEXTDNS_TOOL_SEARCH", raising=False)
        else:
            monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)

        server = build_mcp_server()
        async with Client(server) as client:
            listed = await client.list_resources()
            renderers = _renderer_uris([str(resource.uri) for resource in listed])
            assert len(renderers) == 1, f"expected exactly one renderer resource, got {renderers}"
            assert renderers[0] == _RENDERER_URI

            content = await client.read_resource(renderers[0])

        assert content[0].mime_type == UI_MIME_TYPE
        assert "<html" in content[0].text.lower()

    @pytest.mark.asyncio
    async def test_disclosed_tool_catalog_is_preserved(self, mock_api_key, monkeypatch):
        """Registering the renderer must not leak real tools into tools/list."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

        server = build_mcp_server()
        async with Client(server) as client:
            tools = await client.list_tools()

        assert {tool.name for tool in tools} == {"search_tools", "call_tool"}

    @pytest.mark.asyncio
    async def test_registered_uri_matches_the_advertised_one(self, mock_api_key, monkeypatch):
        """The listed renderer URI is byte-identical to the one the app tools advertise.

        Under ``off`` the tools reach ``tools/list`` with their UI metadata
        rewritten to the wire URI; the registered resource must carry exactly
        that URI or a host following the advertisement would read a different
        resource than the one listed.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

        server = build_mcp_server()
        async with Client(server) as client:
            tools = await client.list_tools()
            listed = await client.list_resources()

        advertised = {
            tool.meta["ui"]["resourceUri"] for tool in tools if tool.name in ("plotAnalytics", "overviewDashboard")
        }
        assert len(advertised) == 1, "both app tools share one renderer configuration"
        assert _renderer_uris([str(resource.uri) for resource in listed]) == list(advertised)
