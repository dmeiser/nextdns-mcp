"""Unit tests for NEXTDNS_TOOL_SEARCH progressive tool disclosure.

Covers the config parser (``get_tool_search_mode``) and the server wiring in
``create_mcp_server``/``build_mcp_server`` that swaps FastMCP's search
transforms in: with a mode enabled, ``tools/list`` exposes only the synthetic
``search_tools`` + ``call_tool`` pair, while the real tools — the ones generated
from the OpenAPI document, plus the two hand-written ones — stay callable
through the ``call_tool`` proxy.
"""

import json

import pytest

from nextdns_mcp import tool_search as ts
from nextdns_mcp.config import ConfigurationError, get_tool_search_mode
from nextdns_mcp.server import build_mcp_server, published_tool_names
from nextdns_mcp.spec_tools import spec_tool_names


class TestGetToolSearchMode:
    """Tests for the NEXTDNS_TOOL_SEARCH parser."""

    def test_default_is_bm25(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_TOOL_SEARCH", raising=False)
        assert get_tool_search_mode() == "bm25"

    def test_empty_value_is_bm25(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "")
        assert get_tool_search_mode() == "bm25"

    @pytest.mark.parametrize("mode", ["off", "bm25"])
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

        assert _words("queryTimeSeries") == {"query", "time", "sery"}
        # "analytics" loses its trailing s the same way "Domains" does, which is what
        # lets a caller write either spelling and still match the tool's name.
        assert _words("getAnalyticsDomains") == {"get", "analytic", "domain"}
        assert _words("delete profile") == {"delete", "profile"}
        # '-ies' folds to 'y' on both sides, so a plural query reaches a tool
        # whose name and prose spell it singular ('categories' names 'Category').
        assert _words("categories") == {"category"}
        assert "category" in _words("getPrivacyCategories")

    def test_a_word_is_shared_by_share_not_by_count(self):
        """Two carriers out of four cancel a word; two out of forty do not.

        The rule was calibrated against an eight-tool catalog, where two carriers was
        a quarter of everything. On the 78-operation document nearly every verb and
        noun has a second carrier, which cancelled the words that actually say which
        tool is meant and left the ordering to alphabetical ties.
        """
        from nextdns_mcp.tool_search import _shared_words

        small = {f"tool{i}": ({"denylist"} if i < 2 else set()) for i in range(4)}
        large = {f"tool{i}": ({"denylist"} if i < 2 else set()) for i in range(40)}
        assert "denylist" in _shared_words(small)
        assert "denylist" not in _shared_words(large)

    def test_a_word_the_whole_catalog_carries_is_still_shared(self):
        from nextdns_mcp.tool_search import _shared_words

        every = {f"tool{i}": {"profile"} for i in range(40)}
        assert "profile" in _shared_words(every)

    def test_a_name_leads_with_the_verb_that_decides_between_two_tools(self):
        """`addToDenylist` and `updateDenylistEntry` share their object, not their verb.

        Ranking them on shared words alone answers the update tool to a caller who
        asked to add, because "entry" is in one name while the verb is discounted as
        common. The word a name leads with is never discounted, and counts for more
        than any other word in it.
        """
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search import _rank_by_name_overlap

        tools = [
            Tool.from_function(lambda: None, name="getDenylist"),
            Tool.from_function(lambda: None, name="updateDenylistEntry"),
            Tool.from_function(lambda: None, name="replaceDenylist"),
            Tool.from_function(lambda: None, name="addToDenylist"),
        ]
        assert _rank_by_name_overlap(tools, "add entry to denylist")[0].name == "addToDenylist"
        assert _rank_by_name_overlap(tools, "change an entry in the denylist")[0].name == "updateDenylistEntry"

    def test_rank_prefers_name_overlap_and_keeps_underlying_order_on_ties(self):
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search import _rank_by_name_overlap

        tools = [
            Tool.from_function(lambda: None, name="getAnalyticsDomains"),
            Tool.from_function(lambda: None, name="listProfiles"),
            Tool.from_function(lambda: None, name="plotAnalytics"),
        ]
        ranked = [t.name for t in _rank_by_name_overlap(tools, "generate analytics plot")]
        assert ranked == ["plotAnalytics", "getAnalyticsDomains", "listProfiles"]

    @pytest.mark.asyncio
    async def test_plot_query_ranks_plot_analytics_first(self, monkeypatch):
        """'generate analytics plot' used to put the analytics query tools ahead of it."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "generate analytics plot"})
            names = [t.get("name") for t in found.data]
            assert names[0] == "plotAnalytics"

    @pytest.mark.asyncio
    async def test_fallback_results_are_name_ranked_too(self, monkeypatch):
        """When nothing matched, the caller's words are the only signal left."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "doh"})
            assert found.data[0].get("name") == "dohLookup"

    @pytest.mark.asyncio
    async def test_a_tool_named_for_the_query_is_never_crowded_out(self, monkeypatch):
        """BM25 ranks prose; a longer description must not drop the tool that is
        literally named for the request (seen when full docstrings went live)."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "doh"})
        assert "dohLookup" in [tool.get("name") for tool in found.data]


class TestMorphologicalQueryExpansion:
    """Lexical search has to bridge plurals: live gemma 12b queries failed on them.

    'parental categories' never surfaced updateParentalControlCategoryEntry and
    'update profile privacy_natives' never surfaced replacePrivacyNatives, so
    the model bulk-replaced through the only write tool the search had shown.
    """

    def test_variant_words_adds_catalog_partners(self):
        from nextdns_mcp.tool_search import _variant_words

        vocabulary = {"category", "categories", "native", "natives"}
        assert _variant_words("categories natives", vocabulary) == {"category", "native"}

    def test_variant_words_ignores_partners_the_catalog_never_wrote(self):
        from nextdns_mcp.tool_search import _variant_words

        assert _variant_words("categories aa", {"categories"}) == set()

    def test_variant_words_adds_plural_of_a_singular_word(self):
        from nextdns_mcp.tool_search import _variant_words

        assert _variant_words("category entry", {"categories", "entries"}) == {"categories", "entries"}

    def test_variant_words_skips_words_that_name_every_tool(self):
        from nextdns_mcp.tool_search import _variant_words

        vocabulary = {"profile", "profiles", "category", "categories"}
        assert _variant_words("delete profile categories", vocabulary, exclude={"profile"}) == {"category"}

    def test_bm25_concatenates_variants_into_the_query(self):
        from nextdns_mcp.tool_search import FallbackBM25SearchTransform

        assert (
            FallbackBM25SearchTransform()._augment_query("parental categories", {"category"})
            == "parental categories category"
        )

    def test_augmentation_without_variants_leaves_the_query_alone(self):
        from nextdns_mcp.tool_search import FallbackBM25SearchTransform

        assert FallbackBM25SearchTransform()._augment_query("natives", set()) == "natives"

    @pytest.mark.asyncio
    async def test_a_plural_noun_query_reaches_the_singular_named_tool(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "parental categories"})
        assert "updateParentalControlCategoryEntry" in [tool.get("name") for tool in found.data]

    @pytest.mark.asyncio
    async def test_a_plural_query_reaches_the_bulk_replace_tool(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "update profile privacy_natives"})
        assert "replacePrivacyNatives" in [tool.get("name") for tool in found.data]


class TestPublishedDescriptions:
    """What a caller sees after search_tools is the tool's `description` field."""

    @pytest.mark.asyncio
    async def test_the_whole_docstring_is_published_not_just_its_first_section(self, monkeypatch):
        """FastMCP publishes only the first text section, splitting at the first
        `Header:` line, so a hand-written tool has to hand over its whole docstring
        itself. For `plotAnalytics` the part that used to be cut is the accepted
        interval range and the worked example - exactly what a caller needs to make
        a call that is not rejected."""
        import inspect

        from fastmcp import Client

        from nextdns_mcp.tools.plots import plotAnalytics

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        published = " ".join(described["plotAnalytics"].split())
        docstring = " ".join((inspect.getdoc(plotAnalytics) or "").split())
        assert published == docstring
        # Named on purpose: the parts that were being dropped.
        assert "interval`` must be between 60 and 86400 seconds" in published
        assert "Examples:" in published

    @pytest.mark.asyncio
    async def test_the_discovered_tool_carries_the_contract_that_decides_the_call(self, monkeypatch):
        """Under disclosure the search answer is where the contract is read, so it
        has to carry the endpoint the name executes and the full input schema, which
        is never cut. Long prose is what gets bounded."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "denylist entry"})
        result = next(tool for tool in found.data if tool.get("name") == "updateDenylistEntry")
        description = " ".join((result.get("description") or "").split())
        for marker in ("denylist", "entry_id", "NextDNS API: PATCH"):
            assert marker in description
        schema = result.get("inputSchema") or {}
        assert sorted(schema.get("properties", {})) == ["active", "entry_id", "profile_id"]
        assert schema.get("required") == ["profile_id", "entry_id"]

    @pytest.mark.asyncio
    async def test_a_search_answer_stays_small_enough_to_be_worth_reading(self, monkeypatch):
        """Measured live: with the full descriptions and four results the answer
        was 10-18 KB for a one-line task, and the run stalled 27 times in 29
        steps. The payload is part of the contract, not an implementation detail.
        The count is CATALOG_MAX_RESULTS (six: a noun-only query must be able to
        show a whole operation family); the byte budget is what actually bounds
        the payload, and the compactor enforces it regardless of the count.
        """
        import json

        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            for query in ("add entry to allowlist", "analytics top domains", "query logs last hour"):
                found = await client.call_tool("search_tools", {"query": query})
                assert len(json.dumps(found.data)) < 8500, query
                assert len(found.data) <= ts.CATALOG_MAX_RESULTS, query

    def test_a_description_with_no_line_break_is_still_cut_at_the_budget(self):
        """Cutting at a line boundary is a preference, not a licence to overflow.

        A description that is one long line has no boundary to cut at, and the
        budget exists because the answer's size is what cost the run its steps;
        the fallback is a hard cut at the limit.
        """
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search import SEARCH_DESCRIPTION_CHARS, _compact_search_results

        one_line = "parameter details. " * 300  # > budget, and not a single newline
        assert len(one_line) > SEARCH_DESCRIPTION_CHARS
        tool = Tool.from_function(lambda: None, name="wideTool", description=one_line)

        [result] = _compact_search_results([tool])
        assert result["description"].endswith("[description truncated]")
        assert len(result["description"]) <= SEARCH_DESCRIPTION_CHARS + len("\n[description truncated]")
        # Cut at the budget, and nothing before it is lost: the excerpt starts at the beginning.
        assert result["description"].startswith("parameter details.")

    def test_an_unresolvable_name_against_an_empty_catalog_answers_nothing(self):
        """No catalog, no suggestion — and no crash on the ranking's own bookkeeping."""
        from nextdns_mcp.tool_search import _closest_tools

        assert _closest_tools("addToDenylist", []) == []


class TestSearchPathGuards:
    """The small refusals on the search path, where an empty input has an answer.

    Each of these is the case where there is nothing to rank: a query with no words,
    a name with no words, a catalog with fewer than two tools, a middleware with no
    transform to read the catalog from. An empty answer is correct; guessing is not.
    """

    def test_a_query_with_no_words_leaves_the_order_as_it_was(self):
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search import _rank_by_name_overlap

        tools = [
            Tool.from_function(lambda: None, name="getDenylist"),
            Tool.from_function(lambda: None, name="addToDenylist"),
        ]
        assert [tool.name for tool in _rank_by_name_overlap(tools, "")] == ["getDenylist", "addToDenylist"]
        assert [tool.name for tool in _rank_by_name_overlap(tools, "   ")] == ["getDenylist", "addToDenylist"]

    def test_a_name_with_no_words_has_no_leading_word(self):
        """The verb is the first word; a name that has none has nothing to score."""
        from nextdns_mcp.tool_search import _leading_word

        assert _leading_word("") == ""
        assert _leading_word("---") == ""
        # A name that starts with a separator still leads with its verb.
        assert _leading_word("_addToDenylist") == "add"

    def test_a_one_tool_catalog_has_no_scope_words(self):
        """Nothing is shared by half a catalog of one, so nothing is discounted.

        With a single tool there is no "which tool does this word mean" to answer,
        and discounting its words would leave the query with no signal at all.
        """
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search import _scope_words

        assert _scope_words([]) == set()
        assert _scope_words([Tool.from_function(lambda: None, name="getDenylist")]) == set()

    async def test_the_shape_middleware_without_a_transform_reads_no_catalog(self):
        """`call_tool` shaping needs the live catalog to know a name's arguments.

        With no transform — or a context that carries no FastMCP context — there is
        nothing to read, and the call must go through unshaped rather than fail on
        the way to reading it.
        """
        from types import SimpleNamespace

        from nextdns_mcp.tool_search import CallToolShapeMiddleware

        assert await CallToolShapeMiddleware(None)._catalog(SimpleNamespace()) == ()
        transform = SimpleNamespace(get_tool_catalog=lambda context: (_ for _ in ()).throw(RuntimeError("no ctx")))
        assert await CallToolShapeMiddleware(transform)._catalog(SimpleNamespace(fastmcp_context=None)) == ()


class TestSyntheticToolDescriptions:
    """Under disclosure the two synthetic tools are the only always-visible text."""

    @pytest.mark.asyncio
    async def test_they_describe_the_discovery_mechanism(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        # Prose wraps, so compare on collapsed whitespace.
        search = " ".join(described["search_tools"].split()).casefold()
        assert "did not return" in search
        assert "exact name" in described["call_tool"].casefold()

    @pytest.mark.asyncio
    async def test_they_state_the_shape_of_the_answer_and_the_name_it_wants(self, monkeypatch):
        """The most common way a call never reaches a tool is a shape mistake.

        Observed live: `call_tool(name=<the whole search result array>)`. The
        descriptions have to say what search_tools returns and which part of it
        `name` takes, because nothing else in the disclosure flow does. The
        array is also resolved now (see TestSearchAnswerAsName), so the
        description states that as what the server does rather than warning
        against a mistake callers kept making anyway.
        """
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        # Prose wraps, so compare on collapsed whitespace.
        search = " ".join(described["search_tools"].split()).casefold()
        call = " ".join(described["call_tool"].split()).casefold()
        assert "array" in search
        assert "one search result's `name` field" in call
        assert "highest-ranked tool in it" in call

    @pytest.mark.asyncio
    async def test_they_instruct_no_behaviour_and_name_no_client_or_real_tool(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        blob = " ".join(described.values()).casefold()
        for marker in (
            "opencode",
            "claude",
            "code mode",
            "listprofiles",
            "getdenylist",
            "never guess",
            "instead of repeating",
        ):
            assert marker not in blob, f"synthetic tool description mentions {marker!r}"


class TestToolSearchServerWiring:
    """Tests for the transform wiring on the built MCP server."""

    @pytest.mark.asyncio
    async def test_default_is_bm25_disclosure(self, monkeypatch):
        """An unset flag gives bm25, the mode every live validation ran under."""
        monkeypatch.delenv("NEXTDNS_TOOL_SEARCH", raising=False)
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        names = {t.name for t in await server.list_tools()}
        assert names == {"search_tools", "call_tool"}

    @pytest.mark.asyncio
    async def test_off_lists_full_catalog(self, monkeypatch):
        """The explicit opt-out advertises the whole generated catalog."""
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        names = {t.name for t in await server.list_tools()}
        assert names == set(published_tool_names())
        assert {"listProfiles", "addToDenylist", "plotAnalytics", "dohLookup"} <= names
        assert "search_tools" not in names
        assert "call_tool" not in names

    @pytest.mark.asyncio
    async def test_enabled_mode_replaces_catalog(self, monkeypatch):
        """With bm25 only the synthetic discovery pair is listed."""
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        names = {t.name for t in await server.list_tools()}
        assert names == {"search_tools", "call_tool"}

    @pytest.mark.asyncio
    async def test_hidden_tools_remain_discoverable_and_callable(self, monkeypatch):
        """The transform controls discovery, not access: search finds the real
        tools and the call_tool proxy reaches them."""
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        async with Client(server) as client:
            found = await client.call_tool("search_tools", {"query": "addToDenylist"})
            assert any(t.get("name") == "addToDenylist" for t in found.data)

            # The hidden tool is still resolvable server-side (call path),
            # even though it is absent from the listing.
            tool = await server.get_tool("addToDenylist")
            assert tool is not None


class TestSearchFallback:
    """Empty-result fallback: guessed-name queries still surface the catalog."""

    @pytest.mark.asyncio
    async def test_unmatchable_query_falls_back_to_a_usable_catalog(self, monkeypatch):
        """A guessed tool name sharing no tokens with any tool must not return empty.

        This is the small model's dominant failure: it searches for a name it
        invented (e.g. 'updateProfileZzz'), gets nothing, then calls that invented
        name and fails. The fallback answers with real tools instead of an empty
        list, and keeps the answer bounded so it stays readable.
        """
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        async with Client(server) as client:
            found = await client.call_tool("search_tools", {"query": "updateProfileZzz"})
            names = [t.get("name") for t in found.data]

        assert names, "an unmatchable query returned nothing"
        # Everything it falls back to is a tool this server will actually run, and
        # at least some of it is generated from the spec: the fallback has to
        # surface the API surface, not just the two hand-written tools.
        assert set(names) <= set(published_tool_names())
        assert set(names) & set(spec_tool_names())
        assert len(names) <= ts.CATALOG_MAX_RESULTS

    def test_build_transform_types(self):
        from nextdns_mcp.tool_search import FallbackBM25SearchTransform, build_search_transform

        assert isinstance(build_search_transform("bm25"), FallbackBM25SearchTransform)
        assert build_search_transform("off") is None


class TestSearchAnswerAsName:
    """`name: result.result` is what the disclosure flow actually produces.

    Live runs, repeatedly: the caller pipes the search answer straight into
    call_tool's `name`. FastMCP refuses it against that parameter's own
    `name: str`, so the step ends with no tool call anywhere - the single most
    common way a call never reaches a tool.
    """

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

    async def test_the_whole_answer_resolves_to_its_top_ranked_tool(self):
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "list profiles"})
            assert found.data[0]["name"] == "listProfiles"
            result = await client.call_tool("call_tool", {"name": found.data, "arguments": {}})
        assert "/profiles" in json.dumps(getattr(result, "structured_content", None) or {})

    async def test_one_result_object_resolves_to_its_name(self):
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "list profiles"})
            result = await client.call_tool("call_tool", {"name": found.data[0], "arguments": {}})
        assert "/profiles" in json.dumps(getattr(result, "structured_content", None) or {})

    async def test_a_name_that_says_nothing_still_fails_the_schema(self):
        """Tolerance has a limit: nothing to read means the original rejection."""
        from fastmcp import Client

        from nextdns_mcp.tool_search import _name_from_search_answer

        assert _name_from_search_answer(42) == ""
        assert _name_from_search_answer([{"id": 1}]) == ""
        async with Client(build_mcp_server()) as client:
            with pytest.raises(Exception, match="Input should be"):
                await client.call_tool("call_tool", {"name": 42, "arguments": {}})

    async def test_a_plain_name_is_left_exactly_as_sent(self):
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            result = await client.call_tool("call_tool", {"name": "listProfiles", "arguments": {}})
        assert "/profiles" in json.dumps(getattr(result, "structured_content", None) or {})


class TestMistypedToolName:
    """A name typed from memory, not copied from a search result.

    Live, from one plots step: `call_tool(name: 'analytics/plot')`, then
    `Unknown tool: 'analytics/plot'` - and under disclosure the caller cannot
    see the names that do exist, so the dead end is total.
    """

    def test_separators_and_case_are_not_part_of_a_name(self):
        names = ["getDenylist", "listRewrites", "plotAnalytics"]
        assert ts._match_tool_name("get_denylist", names) == "getDenylist"
        assert ts._match_tool_name("getdenylist", names) == "getDenylist"

    def test_the_same_words_in_another_order_are_the_same_tool(self):
        names = ["listProfiles", "getAnalyticsDomains", "plotAnalytics"]
        assert ts._match_tool_name("analytics/plot", names) == "plotAnalytics"

    def test_an_ambiguous_or_unmatched_name_is_left_alone(self):
        assert ts._match_tool_name("get", ["getDenylist", "getLogs"]) == ""
        assert ts._match_tool_name("totallyDifferent", ["getDenylist"]) == ""
        assert ts._match_tool_name("getDenylist", ["getDenylist"]) == ""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

    async def test_a_normalised_name_reaches_the_tool(self):
        """`get_denylist` is the same call as `getDenylist`, so it runs."""
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(
                "call_tool", {"name": "get_denylist", "arguments": {"profile_id": "abc123"}}
            )
        content = json.dumps(getattr(result, "structured_content", None) or {})
        assert "/profiles/abc123/denylist" in content, content

    async def test_a_name_that_means_nothing_says_where_names_come_from(self):
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            with pytest.raises(Exception, match="is not a tool of this server") as raised:
                await client.call_tool("call_tool", {"name": "analytics", "arguments": {}})
        assert "search_tools" in str(raised.value)


class TestParametersSentBesideTheName:
    """`call_tool({name, profile_id})` - the parameter, sent beside the wrapper.

    Live, 16 times in one run, all on the profile delete step: the caller named
    the tool and gave its parameters beside `name` instead of inside
    `arguments`. The filter that drops unknown fields dropped them, and the
    server answered about a required argument the call had just supplied.
    """

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

    async def test_the_parameters_reach_the_tool_they_belong_to(self):
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            result = await client.call_tool("call_tool", {"name": "getDenylist", "profile_id": "abc123"})
        content = json.dumps(getattr(result, "structured_content", None) or {})
        assert "/profiles/abc123/denylist" in content, content

    async def test_what_is_inside_arguments_stays_wins(self):
        """Lifting fills a gap; it never overwrites what was sent correctly."""
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(
                "call_tool",
                {"name": "getDenylist", "profile_id": "zzz999", "arguments": {"profile_id": "abc123"}},
            )
        content = json.dumps(getattr(result, "structured_content", None) or {})
        assert "/profiles/abc123/denylist" in content, content
        assert "zzz999" not in content, content

    async def test_a_key_the_named_tool_does_not_declare_is_still_reported_as_ignored(self):
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(
                "call_tool", {"name": "getDenylist", "profile_id": "abc123", "totally_made_up": 1}
            )
        assert "totally_made_up" in json.dumps(getattr(result, "structured_content", None) or {})


class TestRankingOnTheWordsTheCatalogOwns:
    """With `name: result.result` resolving to the top-ranked tool, ranking chooses the tool.

    The queries below are the shape the actor writes: a verb, the object it names,
    and the profile the work is on. `profile 87c444` says which account, not which
    tool, so it is scored out before anything ranks; the subject words stay, because
    they are the only signal about what the caller wants done.
    """

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    @pytest.mark.parametrize(
        ("query", "kept"),
        [
            ("remove zip from security_tlds for profile 87c444", "remove zip from security_tlds for"),
            ("Update the general settings for profile '87c444' to web3", "Update the general settings for to web3"),
            ("delete profile", "delete profile"),
            ("delete profile TEST_PROFILE_20261029", "delete profile TEST_PROFILE_20261029"),
            ("plot aa11aa", "plot aa11aa"),
        ],
    )
    def test_a_scope_reference_comes_out_and_a_subject_stays(self, query, kept):
        """`profile 87c444` names which account, not which tool."""
        stripped = ts._strip_scope_mentions(query, {"profile"})
        assert " ".join(stripped.split()) == " ".join(kept.split())

    def test_the_scope_words_are_read_from_the_catalog(self):
        """profile_id is on every tool; a word two of four carry is not."""
        from fastmcp.tools.base import Tool

        tools = [
            Tool.from_function(lambda profile_id, limit: None, name="getDenylist"),
            Tool.from_function(lambda profile_id, cursor: None, name="listRewrites"),
            Tool.from_function(lambda profile_id, metric: None, name="getAnalyticsDestinationsSeries"),
            Tool.from_function(lambda profile_id, name: None, name="updateProfile"),
        ]
        scope = ts._scope_words(tools)
        assert "profile" in scope
        assert "metric" not in scope

    def test_a_word_unique_to_one_tool_name_is_not_cancelled_by_the_vocabulary(self):
        """`get` is an accepted value of four tools; `settings` belongs to one name."""
        from typing import Literal

        from fastmcp.tools.base import Tool

        def make(name: str) -> Tool:
            def run(operation: Literal["get", "update"] = "get") -> dict:
                return {}

            return Tool.from_function(run, name=name)

        tools = [make("updateSettings"), make("getDenylist"), make("getLogs"), make("updateProfile")]
        ranked = [tool.name for tool in ts._rank_by_name_overlap(tools, "read the current settings")]
        assert ranked[0] == "updateSettings"

    # Three live shapes - "replace privacy_natives with alexa profile <id>",
    # "update performance settings for profile <id>" and "delete profile <id>" - were
    # dropped from this list when a 78-operation catalog answered them with another
    # tool. They are back: the ranking now reads the verb a name leads with, which is
    # the only thing two tools over one object differ on.
    @pytest.mark.parametrize(
        ("query", "first"),
        [
            ("remove zip from security_tlds for profile 87c444", "removeSecurityTLD"),
            ("add entry to denylist profile 87c444", "addToDenylist"),
            ("clear query logs for profile 81b222", "clearLogs"),
            ("add a rewrite for router.home in profile 87c444", "addRewrite"),
            ("list profiles", "listProfiles"),
            ("doh example.com for profile 87c444", "dohLookup"),
            ("analytics plot profile aa11aa ipVersions", "plotAnalytics"),
            ("time series destinations profile 81b222", "getAnalyticsDestinationsSeries"),
            ("replace privacy_natives with alexa profile 87c444", "replacePrivacyNatives"),
            ("update performance settings for profile 51c8a4", "updatePerformanceSettings"),
            ("delete profile TEST_PROFILE_20261029", "deleteProfile"),
        ],
    )
    async def test_a_query_in_the_shape_the_actor_writes_answers_the_right_tool(self, monkeypatch, query, first):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": query})
        assert next(tool["name"] for tool in found.data) == first


class TestATypedNameSaysWhichNamesThereAre:
    """A caller that types a name has already decided it knows the tool.

    Full run 6, verbatim, one step: `call_tool({name: "plot"})`, then
    "generate", "analytics", "profile", "profile aa11aa", "generate analytics
    plot for profile aa11aa for metric protocols over the last day". 23 such
    names in the run, every one a dead end, because the answer pointed back at
    search_tools and the caller had already passed the point of searching.
    """

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        # This class compares against the real catalog, so it asks for the
        # full listing explicitly; the shipped default (bm25) would publish
        # only the synthetic discovery pair.
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

    def _catalog(self) -> list:
        """The real tools, which is what the shape middleware compares against."""
        import asyncio

        from nextdns_mcp.server import build_mcp_server

        async def listed():
            return list(await build_mcp_server().list_tools())

        return asyncio.run(listed())

    def test_a_name_from_another_api_ranks_on_this_catalogs_words(self):
        """`analytics.plot` is not a tool; its words are plotAnalytics'."""
        closest = [tool.name for tool in ts._closest_tools("analytics.plot", self._catalog())]
        assert closest[0] == "plotAnalytics"

    def test_a_name_that_carries_no_catalog_word_matches_nothing(self):
        assert ts._closest_tools("zzzqqq", self._catalog()) == []

    def test_the_required_arguments_come_from_the_tools_own_schema(self):
        """The correction names what the call cannot leave out, read from that tool."""
        by_name = {tool.name: tool for tool in self._catalog()}
        assert ts._required_arguments(by_name["addToDenylist"]) == "profile_id, id"
        assert ts._required_arguments(by_name["deleteRewrite"]) == "profile_id, entry_id"

    async def test_a_typed_name_is_answered_with_the_names_and_arguments_to_use(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            with pytest.raises(Exception, match="Its words match") as raised:
                await client.call_tool("call_tool", {"name": "generate analytics plot", "arguments": {}})
        message = str(raised.value)
        assert "plotAnalytics takes" in message, message
        assert "search_tools" not in message, message

    async def test_a_name_with_nothing_in_common_still_points_at_search(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        async with Client(build_mcp_server()) as client:
            with pytest.raises(Exception, match="is not a tool of this server") as raised:
                await client.call_tool("call_tool", {"name": "zzzqqqTool", "arguments": {}})
        assert "search_tools(query)" in str(raised.value)
