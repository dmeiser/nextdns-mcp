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


class TestNameAwareRanking:
    """The caller's own words should surface the tool whose NAME says them."""

    def test_words_splits_camel_case_and_singularizes(self):
        from nextdns_mcp.tool_search import _words

        assert _words("queryTimeSeries") == {"query", "time", "serie"}
        assert _words("manageProfiles") == {"manage", "profile"}
        assert _words("delete profile") == {"delete", "profile"}

    def test_rank_prefers_name_overlap_and_keeps_underlying_order_on_ties(self):
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search import _rank_by_name_overlap

        tools = [
            Tool.from_function(lambda: None, name="queryAnalytics", description="d"),
            Tool.from_function(lambda: None, name="manageProfiles", description="d"),
            Tool.from_function(lambda: None, name="plotAnalytics", description="d"),
        ]
        ranked = [t.name for t in _rank_by_name_overlap(tools, "generate analytics plot")]
        assert ranked == ["plotAnalytics", "queryAnalytics", "manageProfiles"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mode", "params"),
        [
            ("bm25", {"query": "generate analytics plot"}),
            ("regex", {"pattern": "plot"}),
        ],
    )
    async def test_plot_query_ranks_plot_analytics_first(self, monkeypatch, mode, params):
        """'generate analytics plot' used to put queryAnalytics ahead of it."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", params)
            names = [t.get("name") for t in found.data]
            assert names[0] == "plotAnalytics"

    @pytest.mark.asyncio
    async def test_delete_profile_query_ranks_manage_profiles_first(self, monkeypatch):
        """Four unrelated tools used to outrank the tool named for the job."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "delete profile"})
            assert found.data[0].get("name") == "manageProfiles"

    @pytest.mark.asyncio
    async def test_fallback_results_are_name_ranked_too(self, monkeypatch):
        """When nothing matched, the caller's words are the only signal left."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "doh"})
            assert found.data[0].get("name") == "dohLookup"


class TestSyntheticToolDescriptions:
    """Under disclosure the two synthetic tools are the only always-visible text."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bm25", "regex"])
    async def test_they_describe_the_discovery_mechanism(self, monkeypatch, mode):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        assert "advertise" in described["search_tools"].casefold()
        assert "exact name" in described["call_tool"].casefold()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bm25", "regex"])
    async def test_they_state_the_shape_of_the_answer_and_the_name_it_wants(self, monkeypatch, mode):
        """The most common way a call never reaches a tool is a shape mistake.

        Observed live: `call_tool(name=<the whole search result array>)`. The
        descriptions have to say what search_tools returns and which part of it
        `name` takes, because nothing else in the disclosure flow does.
        """
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        # Prose wraps, so compare on collapsed whitespace.
        search = " ".join(described["search_tools"].split()).casefold()
        call = " ".join(described["call_tool"].split()).casefold()
        assert "array" in search
        assert "single string" in call
        assert "not the array" in call

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bm25", "regex"])
    async def test_they_instruct_no_behaviour_and_name_no_client_or_real_tool(self, monkeypatch, mode):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        blob = " ".join(described.values()).casefold()
        for marker in (
            "opencode",
            "claude",
            "code mode",
            "manageprofiles",
            "managelists",
            "never guess",
            "instead of repeating",
        ):
            assert marker not in blob, f"synthetic tool description mentions {marker!r}"


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


class TestSearchFallback:
    """Empty-result fallback: guessed-name queries still surface the catalog."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bm25", "regex"])
    async def test_unmatchable_query_falls_back_to_full_catalog(self, monkeypatch, mode):
        """A guessed tool name sharing no tokens with any tool must not return empty.

        This is the small model's dominant failure: it searches for a name it
        invented (e.g. 'updateProfile'), gets nothing, then calls that invented
        name and fails. The fallback returns the whole catalog instead.
        """
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", mode)
        server = build_mcp_server()
        async with Client(server) as client:
            params = {"query": "updateProfileZzz"} if mode == "bm25" else {"pattern": "updateProfileZzz"}
            found = await client.call_tool("search_tools", params)
            names = {t.get("name") for t in found.data}
            # Fallback surfaces the real grouped tools rather than an empty list.
            assert "manageSettings" in names
            assert "manageLists" in names

    def test_build_transform_types(self):
        from nextdns_mcp.tool_search import (
            FallbackBM25SearchTransform,
            FallbackRegexSearchTransform,
            build_search_transform,
        )

        assert isinstance(build_search_transform("bm25"), FallbackBM25SearchTransform)
        assert isinstance(build_search_transform("regex"), FallbackRegexSearchTransform)
        assert build_search_transform("off") is None
