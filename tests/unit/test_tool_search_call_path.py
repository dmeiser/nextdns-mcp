"""Unit tests for the tool-search call path and answer shaping.

Covers ranking on the words the catalog owns, unknown-name reuse, the
generic-verb gate, memoized catalog structures, compaction cache keying,
answer byte budgets, the write gate on the call path, and shape repair.
The mode/ranking half of the suite lives in ``test_tool_search.py``.
"""

import json
import re

import pytest

from nextdns_mcp import tool_search as ts
from nextdns_mcp import tool_search_answer as search_answer
from nextdns_mcp import tool_search_shape as search_shape
from nextdns_mcp.server import build_mcp_server


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
            # `profile_id=87c444` is the shape the strip's comment cites and the
            # one the old exact-token match never stripped: the regex holds the
            # compound token while the scope set holds the split words.
            ("delete profile_id=87c444", "delete"),
            ("update profile_id 87c444 now", "update now"),
        ],
    )
    def test_a_scope_reference_comes_out_and_a_subject_stays(self, query, kept):
        """`profile 87c444` names which account, not which tool."""
        stripped = ts.strip_scope_mentions(query, {"profile"})
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
        from nextdns_mcp.tool_search_rank import _scope_words

        scope = _scope_words(tools)
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
        ranked = [tool.name for tool in ts.rank_by_name_overlap(tools, "read the current settings")]
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
            # The bare form of the flagship query: the shared-word discount ties
            # deleteProfile and deleteRewrite on the verb, and only the
            # undiscounted name-overlap tie-break puts the name the caller
            # actually spelled first (qwen FUNC-3; the scope-referenced form
            # above was pinned, the bare one - the live mis-call path - now is).
            ("delete profile", "deleteProfile"),
            # A generic leading verb no longer carries a tool into the top on
            # its own: "get profile" reaches getProfile because the query also
            # carries its object word, not to whichever get* tool has the
            # friendlier prose (gemini ARCH-2).
            ("get profile", "getProfile"),
        ],
    )
    async def test_a_query_in_the_shape_the_actor_writes_answers_the_right_tool(self, monkeypatch, query, first):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": query}, raise_on_error=False)
        assert next(tool["name"] for tool in found.data) == first

    async def test_a_bare_verb_query_ranks_on_its_object_word_not_the_verb(self, monkeypatch):
        """'get rewrites' answers listRewrites, not a get* tool reached through `get`.

        The verb bonus used to be unconditional, and `get` (thirty-odd names)
        dragged every ``get*`` tool in: getPrivacySettings scored above
        listRewrites on "get" alone while "rewrites" - the word the query
        actually contributed - went to the tool the caller meant (gemini
        ARCH-2). listRewrites must rank above every tool reached only through
        the generic verb.
        """
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        from fastmcp import Client

        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "get rewrites"}, raise_on_error=False)
        names = [tool["name"] for tool in found.data]
        assert "listRewrites" in names
        if "getPrivacySettings" in names:
            assert names.index("listRewrites") < names.index("getPrivacySettings")


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
        closest = [tool.name for tool in search_shape._closest_tools("analytics.plot", self._catalog())]
        assert closest[0] == "plotAnalytics"

    def test_a_name_that_carries_no_catalog_word_matches_nothing(self):
        assert search_shape._closest_tools("zzzqqq", self._catalog()) == []

    def test_the_required_arguments_come_from_the_tools_own_schema(self):
        """The correction names what the call cannot leave out, read from that tool."""
        by_name = {tool.name: tool for tool in self._catalog()}
        assert search_shape._required_arguments(by_name["addToDenylist"]) == "profile_id, id"
        assert search_shape._required_arguments(by_name["deleteRewrite"]) == "profile_id, entry_id"

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


class TestExactPublishedCallsSkipTheCatalog:
    """PERF-9: an exact published name needs no catalog read to be resolved."""

    @staticmethod
    def _context(name: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            message=SimpleNamespace(name="call_tool", arguments={"name": name, "arguments": {}}),
            fastmcp_context=object(),
        )

    @pytest.mark.parametrize(
        "name",
        [
            "listProfiles",
            "plotAnalytics",
            "dohLookup",
            "overviewDashboard",
            "generate_prefab_ui",
            "search_prefab_components",
        ],
    )
    async def test_an_exact_published_name_does_not_read_the_catalog(self, name):
        class CountingTransform:
            def __init__(self):
                self.calls = 0

            async def get_tool_catalog(self, context):
                self.calls += 1
                return []

        transform = CountingTransform()
        middleware = ts.CallToolShapeMiddleware(
            transform=transform,
            endpoints={"listProfiles": ("GET", "/profiles")},
            tool_names=[
                "plotAnalytics",
                "dohLookup",
                "overviewDashboard",
                "generate_prefab_ui",
                "search_prefab_components",
            ],
        )

        async def call_next(context):
            return "ran"

        assert await middleware.on_call_tool(self._context(name), call_next) == "ran"
        assert transform.calls == 0

    async def test_a_non_exact_name_still_uses_the_resolution_path(self):
        from fastmcp.tools.base import Tool

        class CountingTransform:
            def __init__(self):
                self.calls = 0

            async def get_tool_catalog(self, context):
                self.calls += 1
                return [Tool.from_function(lambda: None, name="getProfile")]

        transform = CountingTransform()
        middleware = ts.CallToolShapeMiddleware(transform=transform, endpoints={}, tool_names=["getProfile"])
        context = self._context("get profile")

        async def call_next(context):
            return "ran"

        assert await middleware.on_call_tool(context, call_next) == "ran"
        assert transform.calls == 1
        assert context.message.arguments["name"] == "getProfile"


class TestUnknownNameReusesTheCatalogsWordData:
    """PERF-3: the unknown-name path must not rebuild the catalog's word data per call.

    Measured at the intake probe, every unknown ``call_tool`` name re-ran the
    flat and word normalizations over the whole catalog and rebuilt the
    name-word, vocabulary, shared-word, and generic-leading structures the
    suggestion ranking binds. With the catalog unchanged and its data built,
    a subsequent unknown request — bare name or search-answer shape — must
    perform zero catalog-side normalizations and zero ranking-data rebuilds,
    while the refusal and the ranked suggestion come out exactly as before.
    """

    @pytest.fixture
    def tools(self):
        from fastmcp.tools.base import Tool

        return [
            Tool.from_function(lambda: None, name="getDenylist"),
            Tool.from_function(lambda: None, name="plotAnalytics"),
        ]

    @pytest.fixture
    def middleware(self, tools):
        class _StaticTransform:
            async def get_tool_catalog(self, context):
                return list(tools)

        return ts.CallToolShapeMiddleware(transform=_StaticTransform(), endpoints={})

    @pytest.fixture
    def work_counts(self, monkeypatch):
        """Counters on the catalog-side rebuild entry points and normalizations.

        Each name is patched in the module whose code actually calls it: the
        memo helper and the structure builders run inside
        ``tool_search_rank``, and the normalizers run on the rank build and
        on the shape-side matching.
        """
        from collections import Counter

        import nextdns_mcp.tool_search_rank as rank
        import nextdns_mcp.tool_search_shape as shape

        counts: Counter[str] = Counter()
        for module, name in (
            (rank, "_catalog_words_for"),
            (rank, "_name_words"),
            (rank, "_vocabulary_words"),
            (rank, "_shared_word_sets"),
            (rank, "_generic_leading_words"),
        ):
            original = getattr(module, name)

            def count_rebuild(*args, _original=original, _name=name, **kwargs):
                counts[_name] += 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(module, name, count_rebuild)

        normalized: list[str] = []
        for module, name in (
            (rank, "_flat_key"),
            (rank, "comparable_words"),
            (shape, "_flat_key"),
            (shape, "_words"),
        ):
            original = getattr(module, name)

            def count_normalization(text, _original=original, _normalized=normalized):
                _normalized.append(text)
                return _original(text)

            monkeypatch.setattr(module, name, count_normalization)
        return counts, normalized

    @staticmethod
    async def _resolve(middleware, name):
        from types import SimpleNamespace

        from fastmcp.exceptions import ToolError

        context = SimpleNamespace(
            message=SimpleNamespace(name="call_tool", arguments={"name": name, "arguments": {}}),
            fastmcp_context=object(),
        )

        async def call_next(context):
            return "ran"

        try:
            return await middleware.on_call_tool(context, call_next)
        except ToolError as raised:
            return raised

    async def test_a_repeated_unknown_name_rebuilds_nothing(self, middleware, work_counts):
        from fastmcp.exceptions import ToolError

        counts, normalized = work_counts

        first = await self._resolve(middleware, "generate analytics plot")
        assert isinstance(first, ToolError)
        assert "plotAnalytics takes" in str(first)
        # The first unknown request built the catalog's data: both candidates
        # were flat-normalized and word-tokenized exactly once.
        assert counts["_catalog_words_for"] == 1
        assert set(normalized) == {"getDenylist", "plotAnalytics", "generate analytics plot"}

        normalized.clear()
        snapshot = dict(counts)
        second = await self._resolve(middleware, "generate analytics plot")

        assert str(second) == str(first)
        # Zero catalog-side normalizations: only the typed name itself was
        # folded and tokenized, no candidate name was touched.
        assert set(normalized) == {"generate analytics plot"}
        # Zero ranking-data rebuilds: no memo entry point fired again.
        assert dict(counts) == snapshot

    async def test_the_search_answer_shape_reuses_the_built_data(self, middleware, work_counts):
        counts, normalized = work_counts
        bare = await self._resolve(middleware, "generate analytics plot")
        normalized.clear()
        snapshot = dict(counts)

        answered = await self._resolve(middleware, [{"name": "generate analytics plot"}])

        assert str(answered) == str(bare)
        assert set(normalized) == {"generate analytics plot"}
        assert dict(counts) == snapshot

    async def test_an_exact_name_skips_the_catalog_data_entirely(self, middleware, work_counts):
        counts, normalized = work_counts

        result = await self._resolve(middleware, [{"name": "plotAnalytics"}])

        assert result == "ran"
        assert counts["_catalog_words_for"] == 0
        assert normalized == []

    def test_an_exact_name_resolves_from_the_map_without_matching(self, tools):
        """The resolver answers an exact name from the map alone — no word data."""
        by_name = {tool.name: tool for tool in tools}
        assert search_shape._resolved_entry("plotAnalytics", by_name) == ("plotAnalytics", by_name["plotAnalytics"])

    def test_the_unmemoized_fallback_normalizes_each_candidate_once(self, tools, work_counts):
        """Direct callers without a catalog keep the linear O(catalog) contract.

        The hoisted lookup must not rebuild per comprehension iteration: on
        the fallback path each candidate is flat-normalized once and
        tokenized once, whatever the catalog size.
        """
        from collections import Counter

        counts, normalized = work_counts
        names = [tool.name for tool in tools]

        assert search_shape._match_tool_name("generate analytics plot", names) == ""

        per_candidate = Counter(text for text in normalized if text in set(names))
        assert per_candidate == Counter({"getDenylist": 2, "plotAnalytics": 2})
        # The fallback builds just the two comparison dicts — no ranking-data
        # rebuild at all.
        assert counts["_catalog_words_for"] == 0
        assert counts["_name_words"] == 0
        assert counts["_vocabulary_words"] == 0

    async def test_the_real_server_builds_the_data_once_across_unknown_calls(self, monkeypatch):
        """End to end on the built server: two unknown calls, one catalog build."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

        from fastmcp import Client

        import nextdns_mcp.tool_search_rank as rank

        builds = 0
        original = rank._catalog_words_for

        def count_build(tools):
            nonlocal builds
            builds += 1
            return original(tools)

        monkeypatch.setattr(rank, "_catalog_words_for", count_build)

        async with Client(build_mcp_server()) as client:
            for _ in range(2):
                with pytest.raises(Exception, match="Its words match") as raised:
                    await client.call_tool("call_tool", {"name": "generate analytics plot", "arguments": {}})
        assert "plotAnalytics takes" in str(raised.value)
        assert builds == 1


class TestTheGenericVerbGate:
    """A verb shared by a dozen names says what to do, not which tool (gemini ARCH-2).

    The leading-verb exemption used to be unconditional: "get rewrites" scored
    getPrivacySettings on `get` alone and outranked listRewrites, and "get
    profile" ranked getPrivacySettings above getProfile. A verb that leads more
    than one name now counts - hit, bonus, and inclusion in the answer - only
    when a second query word also reaches that tool.
    """

    def test_a_generic_verb_alone_scores_nothing(self):
        from nextdns_mcp.tool_search_rank import _words_score

        args = (
            {"get", "rewrite"},
            {"get", "privacy", "settings"},
            {"profile", "id"},
            {"profile"},
            {"profile", "id"},
            "get",
        )
        gated = _words_score(*args, frozenset({"get"}))
        assert gated == 0
        # Without the gate (the module-level default) the old full exemption is
        # intact, so callers that pass no generic set keep the old arithmetic.
        assert _words_score(*args) == 3

    def test_a_second_word_the_tool_carries_restores_the_verb(self):
        from nextdns_mcp.tool_search_rank import _words_score

        score = _words_score(
            {"get", "privacy"},
            {"get", "privacy", "settings"},
            {"profile", "id"},
            {"profile"},
            {"profile", "id"},
            "get",
            frozenset({"get"}),
        )
        assert score == 5  # name hits {get, privacy} * 2 + the verb's extra

    def test_a_verb_unique_to_one_name_is_that_tool_word_not_a_generic(self):
        """dohLookup leads with `doh`; no other name does - the gate never fires."""
        from nextdns_mcp.tool_search_rank import _generic_leading_words

        names = {"dohLookup": {"doh", "lookup"}, "getProfile": {"get", "profile"}, "getLogs": {"get", "log"}}
        assert _generic_leading_words(names) == {"get"}

    def test_a_tool_with_no_name_words_scores_nothing(self):
        from nextdns_mcp.tool_search_rank import _words_score

        assert _words_score({"x"}, None, None, set(), set(), "") == 0


class TestTieBreakOnUndiscountedNameOverlap:
    """Score ties break on how many query words the NAME simply contains (qwen FUNC-3).

    The shared-word discount ties deleteProfile and deleteRewrite for "delete
    profile" - both match on the verb, "profile" is discounted - and the
    fallback to BM25/registration order answered deleteRewrite. The raw name
    overlap says the caller spelled deleteProfile itself.
    """

    def test_a_tie_breaks_for_the_tool_whose_name_holds_more_query_words(self):
        from fastmcp.tools.base import Tool

        from nextdns_mcp.tool_search_rank import rank_by_name_overlap

        tools = [
            Tool.from_function(lambda: None, name="deleteRewrite"),
            Tool.from_function(lambda: None, name="deleteProfile"),
        ]
        # deleteRewrite is listed first and ties on the weighted score; the
        # tie-break must not let registration order answer for the name itself.
        assert rank_by_name_overlap(tools, "delete profile")[0].name == "deleteProfile"


class TestMemoizedCatalogStructures:
    """Catalog constants are computed once per catalog identity, not per query (PERF-2/3/5, L).

    The word memo keys on the catalog's tool NAMES: the ordered-names tuple is
    the hot-path key (no sort), and the sorted-names store it falls back to
    keeps one memo shared across orderings of the same catalog. The
    compaction cache keys on the tool NAME with a metadata fingerprint (see
    TestCompactionCacheKeying). A replaced catalog is a new name-set and
    rebuilds.
    """

    def test_catalog_words_are_memoized_by_tool_names(self):
        from fastmcp.tools.base import Tool

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        tools = [
            Tool.from_function(lambda: None, name="aTool"),
            Tool.from_function(lambda: None, name="bTool"),
        ]
        first = transform._catalog_words(tools)
        # Same names, any order: the same memo object, not a recompute.
        assert transform._catalog_words(list(reversed(tools))) is first
        # A different catalog (different name set) is a different memo.
        assert transform._catalog_words(tools[:1]) is not first

    def test_warm_catalog_word_lookup_pays_no_sorts(self, monkeypatch):
        """A repeated lookup against an unchanged catalog never re-sorts the memo key (PERF-5)."""
        from fastmcp.tools.base import Tool

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        tools = [
            Tool.from_function(lambda: None, name="aTool"),
            Tool.from_function(lambda: None, name="bTool"),
        ]
        first = transform._catalog_words(tools)  # cold: builds the words and sorts once
        sorts = 0
        real_sorted = sorted

        def counting_sorted(*args, **kwargs):
            nonlocal sorts
            sorts += 1
            return real_sorted(*args, **kwargs)

        monkeypatch.setattr("builtins.sorted", counting_sorted)
        # The hot path: the same effective catalog on every call (FastMCP hands
        # the search a fresh sequence of the same instances each time).
        for _ in range(3):
            assert transform._catalog_words(list(tools)) is first
        assert sorts == 0

    async def test_repeated_renders_copy_no_entry_out_of_the_cache(self):
        """A warm render rebuilds the answer list, never its entries.

        The identity check is what fails if a render starts copying each kept
        entry again; the encoded answer has to stay what it was, so the entry is
        compared as the caller reads it.
        """
        from fastmcp.tools.base import Tool

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        tool = Tool.from_function(lambda: None, name="aTool", description="does a thing")
        first = await transform._render_results([tool])
        second = await transform._render_results([tool])
        assert second[0] is first[0]
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    async def test_the_answer_list_belongs_to_the_caller_that_received_it(self):
        """Editing or clearing one answer cannot change the next one.

        The caller owns the list; the entries are the cache's. A render that
        handed back a memoized list would let one caller's edit outlive its own
        answer, which is what the fresh list in _serialize_results prevents.
        """
        from fastmcp.tools.base import Tool

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        tools = [
            Tool.from_function(lambda: None, name="aTool", description="does a thing"),
            Tool.from_function(lambda: None, name="bTool", description="does another thing"),
        ]
        answer = await transform._render_results(tools)
        answer[0] = {"name": "not-a-tool"}
        answer.append({"name": "injected"})
        del answer[1]
        answer.clear()
        assert [t["name"] for t in await transform._render_results(tools)] == ["aTool", "bTool"]
        assert answer == []

    def test_synthetic_tools_are_built_once(self):
        """FastMCP rebuilds them per listing; they are constants of the transform."""
        transform = ts.FallbackBM25SearchTransform(endpoints={})
        assert transform._make_search_tool() is transform._make_search_tool()
        assert transform._make_call_tool() is transform._make_call_tool()
        assert transform._make_search_tool() is not transform._make_call_tool()


class TestCompactionCacheKeying:
    """The compaction cache keys on tool NAME, not Tool instance (PERF-4).

    FastMCP hands the search catalog a fresh ``model_copy`` of every app tool
    on every read, so an instance key missed the cache on each search and
    leaked one entry per app tool per query for the life of the server. The
    fixed cache holds one entry per tool name: a per-read copy of an
    unchanged tool hits it, and changed metadata replaces the entry.
    """

    async def test_repeated_searches_hold_the_cache_size_steady(self, monkeypatch):
        """The reported leak: three rounds of the two UI-tool searches.

        With the instance key the cache grew 12 -> 14 -> 16 over the rounds,
        one fresh entry per app tool per round. The fixed cache is stable
        after the first round, and both tools are still returned every time.
        """
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        transform = next(t for t in server._transforms if isinstance(t, ts.FallbackBM25SearchTransform))
        queries = ("generate analytics plot", "show me a dashboard of my account")
        async with Client(server) as client:
            sizes = []
            for _ in range(3):
                for query in queries:
                    found = await client.call_tool("search_tools", {"query": query}, raise_on_error=False)
                    assert found.data, f"no results for {query!r}"
                sizes.append(len(transform._compacted_cache))
        assert sizes[0] == sizes[1] == sizes[2], f"the cache grew across identical rounds: {sizes}"
        assert "plotAnalytics" in transform._compacted_cache
        assert "overviewDashboard" in transform._compacted_cache

    def test_a_per_read_copy_of_an_app_tool_hits_the_same_entry(self):
        """The app-tool shape: a shallow model_copy that rebuilds only meta.

        FastMCP's per-read copy shares every structure with the registered
        tool and rebuilds the meta dict with the same content; that copy must
        hit the cache, not open a new entry.
        """
        from fastmcp.tools.base import Tool

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        tool = Tool.from_function(
            lambda: None,
            name="aTool",
            description="does a thing",
            meta={"ui": {"resourceUri": "ui://renderer"}},
        )
        first, _ = transform._cached_compaction(tool)
        copy = tool.model_copy(update={"meta": {"ui": {"resourceUri": "ui://renderer"}}})
        second, _ = transform._cached_compaction(copy)
        assert second is first
        assert len(transform._compacted_cache) == 1

    def test_changed_metadata_replaces_the_entry_under_the_same_name(self):
        """A re-registered tool with changed metadata refreshes the payload."""
        from fastmcp.tools.base import Tool

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        before = Tool.from_function(lambda: None, name="aTool", description="does a thing")
        first, _ = transform._cached_compaction(before)
        assert len(transform._compacted_cache) == 1
        after = Tool.from_function(lambda: None, name="aTool", description="does another thing")
        second, _ = transform._cached_compaction(after)
        assert second is not first
        assert second["description"] == "does another thing"
        assert len(transform._compacted_cache) == 1


class TestAnswerByteBudget:
    """The whole-answer budget counts BYTES and skips rather than breaks (PRIN-11/PERF-8)."""

    def test_the_budget_measures_bytes_not_characters(self):
        from fastmcp.tools.base import Tool

        tool = Tool.from_function(lambda: None, name="accentedTool", description="café " * 400)
        dumped, cost = ts.compact_entry(tool)
        # The cost is the UTF-8 byte length of the wire JSON (unescaped, as the
        # MCP payload carries it), not the character count its dumps reported:
        # each é is one character and two bytes.
        wire = json.dumps(dumped, sort_keys=True, ensure_ascii=False)
        assert cost == len(wire.encode("utf-8"))
        assert cost > len(wire)

    def test_an_oversized_middle_result_does_not_suppress_the_smaller_ones(self, monkeypatch):
        """Skip-and-continue: breaking at the first overflow hid the small results after it."""
        from fastmcp.tools.base import Tool

        import nextdns_mcp.tool_search_answer

        small_one = Tool.from_function(lambda: None, name="smallOne", description="a")
        huge = Tool.from_function(lambda: None, name="hugeOne", description="x" * 5000)
        small_two = Tool.from_function(lambda: None, name="smallTwo", description="b")
        cost_one = ts.compact_entry(small_one)[1]
        cost_huge = ts.compact_entry(huge)[1]
        cost_two = ts.compact_entry(small_two)[1]
        assert cost_huge > cost_one and cost_huge > cost_two
        # The budget fits exactly the two small results, framing comma counted.
        monkeypatch.setattr(nextdns_mcp.tool_search_answer, "SEARCH_ANSWER_MAX_BYTES", cost_one + len(",") + cost_two)
        names = [result["name"] for result in search_answer._compact_search_results([small_one, huge, small_two])]
        assert names == ["smallOne", "smallTwo"]

    def test_the_first_result_is_kept_even_when_it_alone_overflows(self, monkeypatch):
        """An empty answer is worse than a big one: the head stays no matter its size."""
        from fastmcp.tools.base import Tool

        import nextdns_mcp.tool_search_answer

        monkeypatch.setattr(nextdns_mcp.tool_search_answer, "SEARCH_ANSWER_MAX_BYTES", 1)
        huge = Tool.from_function(lambda: None, name="hugeOne", description="x" * 5000)
        small = Tool.from_function(lambda: None, name="smallOne", description="b")
        assert [result["name"] for result in search_answer._compact_search_results([huge, small])] == ["hugeOne"]

    async def test_the_cached_render_encodes_what_the_uncached_reference_encodes(self):
        """The memoized serializer must not drift from the uncached one, in either mode.

        Both budgets are exercised: the ranked answer under the 8 KB ceiling and
        the full-catalog fallback under its own. Entry sets are compared as
        encoded JSON, since the ranked path re-orders what it keeps.
        """
        from fastmcp.tools.base import Tool

        def encoded(answer: list[dict[str, object]]) -> list[str]:
            return sorted(json.dumps(entry, sort_keys=True) for entry in answer)

        transform = ts.FallbackBM25SearchTransform(endpoints={})
        tools = [
            Tool.from_function(lambda: None, name="listProfiles", description="List the profiles on the account."),
            Tool.from_function(lambda: None, name="getDenylist", description="Read the denylist entries of a profile."),
        ]
        expected = encoded(search_answer._compact_search_results(tools))
        assert encoded(await transform._render_results(tools)) == expected
        assert encoded(await transform._render_results(await transform._search(tools, "profile"))) == expected
        assert encoded(await transform._render_results(await transform._search(tools, "zzz unmatchable"))) == expected


class TestMultiEntryAnswerWriteGate:
    """A multi-result answer whose head is a write is refused, not executed (SEC-6).

    Piping the whole search answer into ``name`` used to run whatever ranked
    first, including ``deleteProfile``. A single-entry answer still resolves
    (the caller had one reading); a read at the head still resolves; the write
    head is refused with the candidates named so the caller picks. Read-only
    comes from the endpoint map, and a name the map does not carry (the local
    operations) reads.
    """

    @staticmethod
    def _tool(name: str):
        from fastmcp.tools.base import Tool

        return Tool.from_function(lambda: None, name=name)

    async def test_a_multi_entry_answer_headed_by_a_write_is_refused_with_the_candidates(self):
        from fastmcp.exceptions import ToolError

        middleware = ts.CallToolShapeMiddleware(endpoints={"deleteProfile": ("DELETE", "/profiles/{profile_id}")})
        answer = [{"name": "deleteProfile"}, {"name": "listProfiles"}, {"name": "getProfile"}]
        with pytest.raises(ToolError, match="deleteProfile") as raised:
            await middleware._resolve_tool_name(answer, [])
        message = str(raised.value)
        for candidate in ("listProfiles", "getProfile"):
            assert candidate in message
        assert "write" in message

    async def test_a_multi_entry_answer_headed_by_a_read_resolves_to_the_head(self):
        middleware = ts.CallToolShapeMiddleware(
            endpoints={"deleteProfile": ("DELETE", "/p"), "listProfiles": ("GET", "/profiles")}
        )
        lister = self._tool("listProfiles")
        resolved, target = await middleware._resolve_tool_name(
            [{"name": "listProfiles"}, {"name": "deleteProfile"}], [lister, self._tool("deleteProfile")]
        )
        assert resolved == "listProfiles"
        assert target is lister

    async def test_a_single_entry_answer_resolves_even_when_the_tool_writes(self):
        """One reading is not a guess: the caller chose that entry."""
        middleware = ts.CallToolShapeMiddleware(endpoints={"deleteProfile": ("DELETE", "/p")})
        deleter = self._tool("deleteProfile")
        resolved, target = await middleware._resolve_tool_name([{"name": "deleteProfile"}], [deleter])
        assert resolved == "deleteProfile"
        assert target is deleter

    async def test_a_name_absent_from_the_endpoint_map_reads(self):
        """plotAnalytics/dohLookup are not spec routes; the gate must not treat them as writes."""
        middleware = ts.CallToolShapeMiddleware(endpoints={"deleteProfile": ("DELETE", "/p")})
        plot = self._tool("plotAnalytics")
        resolved, target = await middleware._resolve_tool_name(
            [{"name": "plotAnalytics"}, {"name": "getProfile"}], [plot]
        )
        assert resolved == "plotAnalytics"
        assert target is plot


class TestCallPathCatalogLookup:
    """The call path resolves names against the current request's catalog (PERF-5).

    FastMCP rebuilds the catalog's ``Tool`` shells on every read, so the
    name -> tool lookup is built per call: at the shipped catalog that build
    (~4.8 us over 81 tools) is cheaper than any key sound enough to invalidate
    a tool re-registered under the same name (~11.5 us), and an unmemoized
    lookup cannot serve a replaced tool. What is memoized on this path is the
    catalog's word data (~1.9 ms to build), shared with the search path
    through ``_catalog_words_memo`` and keyed so a warm catalog pays no sort.
    """

    @staticmethod
    def _tool(name: str):
        from fastmcp.tools.base import Tool

        return Tool.from_function(lambda: None, name=name)

    async def test_a_same_name_replacement_resolves_to_the_current_tool(self):
        """A tool re-registered under the same name with a new schema is the one resolution lands on."""
        from fastmcp.tools.base import Tool

        def old_profile() -> str:
            return "old"

        def new_profile(profile_id: str) -> str:
            return profile_id

        middleware = ts.CallToolShapeMiddleware(endpoints={"getProfile": ("GET", "/p")})
        original = Tool.from_function(old_profile, name="getProfile")
        await middleware._resolve_tool_name("getProfile", [original])
        replacement = Tool.from_function(new_profile, name="getProfile")
        resolved, target = await middleware._resolve_tool_name("getProfile", [replacement])
        assert resolved == "getProfile"
        assert target is replacement

    async def test_repeated_resolution_hands_back_the_current_entry_each_time(self):
        """Two reads of an unchanged catalog hand back that read's own entry, not a cached one."""
        middleware = ts.CallToolShapeMiddleware(endpoints={"getProfile": ("GET", "/profiles/{profile_id}")})
        first_read = [self._tool("getProfile"), self._tool("listProfiles")]
        _, first_target = await middleware._resolve_tool_name("getProfile", first_read)
        second_read = [self._tool("getProfile"), self._tool("listProfiles")]
        resolved, second_target = await middleware._resolve_tool_name("getProfile", second_read)
        assert resolved == "getProfile"
        assert second_target is second_read[0]
        assert second_target is not first_target

    def test_the_middleware_word_memo_pays_no_sorts_when_warm(self, monkeypatch):
        """The call path's word memo hits on an unchanged catalog without re-sorting the key."""
        from fastmcp.tools.base import Tool

        middleware = ts.CallToolShapeMiddleware(endpoints={})
        tools = [
            Tool.from_function(lambda: None, name="aTool"),
            Tool.from_function(lambda: None, name="bTool"),
        ]
        first = middleware._catalog_words(tools)  # cold: builds the words and sorts once
        sorts = 0
        real_sorted = sorted

        def counting_sorted(*args, **kwargs):
            nonlocal sorts
            sorts += 1
            return real_sorted(*args, **kwargs)

        monkeypatch.setattr("builtins.sorted", counting_sorted)
        for _ in range(3):
            assert middleware._catalog_words(list(tools)) is first
        assert sorts == 0
        # A different name set is a different memo, and it sorts once.
        assert middleware._catalog_words(tools[:1]) is not first
        assert sorts == 1


class TestAWriteOffTheRouteMapIsStillAWrite:
    """A tool the endpoint map does not carry is a write when its own annotations say so.

    ``generate_prefab_ui`` is not a spec route, so it has no HTTP method to read,
    and every name the map lacked was classified as ``GET``. A loose or
    ambiguous match therefore ran sandboxed code the caller never singled out,
    even though the tool is registered with ``read_only_hint=False``. For a name
    off the map the tool's own annotation decides; an annotation that reads
    (the three composite operations) still reads.
    """

    @staticmethod
    def _generator():
        from fastmcp.tools.base import Tool, ToolAnnotations

        return Tool.from_function(
            lambda code, data="": None,
            name="generate_prefab_ui",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
        )

    async def test_a_loosely_matched_name_for_it_is_refused_not_run(self):
        """Same words, other order: the loose match lands on a write and is refused."""
        from fastmcp.exceptions import ToolError

        generator = self._generator()
        middleware = ts.CallToolShapeMiddleware(endpoints={"listProfiles": ("GET", "/profiles")})
        with pytest.raises(ToolError, match="generate_prefab_ui") as raised:
            await middleware._resolve_tool_name("ui_prefab_generate", [generator])
        assert "ui_prefab_generate" in str(raised.value)
        # The exact name still resolves: only the loose write is refused.
        resolved, target = await middleware._resolve_tool_name("generate_prefab_ui", [generator])
        assert resolved == "generate_prefab_ui"
        assert target is generator

    async def test_a_multi_entry_answer_headed_by_it_is_refused_not_run(self):
        """Piping the answer into `name` must not reach the sandbox on a guess."""
        from fastmcp.exceptions import ToolError

        generator = self._generator()
        middleware = ts.CallToolShapeMiddleware(endpoints={"listProfiles": ("GET", "/profiles")})
        answer = [{"name": "generate_prefab_ui"}, {"name": "listProfiles"}]
        with pytest.raises(ToolError, match="generate_prefab_ui") as raised:
            await middleware._resolve_tool_name(answer, [generator])
        assert "write" in str(raised.value)

    async def test_a_non_endpoint_tool_annotated_read_only_still_reads(self):
        """The composites are off the map and marked read-only: they keep resolving loosely."""
        from fastmcp.tools.base import Tool, ToolAnnotations

        plot = Tool.from_function(
            lambda: None,
            name="plotAnalytics",
            annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
        )
        middleware = ts.CallToolShapeMiddleware(endpoints={"deleteProfile": ("DELETE", "/p")})
        resolved, target = await middleware._resolve_tool_name("analytics_plot", [plot])
        assert resolved == "plotAnalytics"
        assert target is plot


def test_the_fallback_sorts_a_write_off_the_map_with_the_writes():
    """The full-catalog answer keeps a sandbox-running tool off its head.

    The fallback sorts reads first using the endpoint map, and a name the map
    does not carry used to sort as a read whatever its annotations said.
    """
    import asyncio

    from fastmcp.tools.base import Tool, ToolAnnotations

    tools = [
        Tool.from_function(
            lambda code, data="": None,
            name="generate_prefab_ui",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
        ),
        Tool.from_function(lambda: None, name="listProfiles"),
    ]
    transform = ts.build_search_transform("bm25", endpoints={"listProfiles": ("GET", "/profiles")})
    assert transform is not None

    async def main():
        ranked = [t.name for t in await transform._search(tools, "qxqv zzxj no such tool words")]
        assert ranked == ["listProfiles", "generate_prefab_ui"]

    asyncio.run(main())


def test_a_piped_answer_headed_by_generate_prefab_ui_is_refused_on_a_live_server(monkeypatch):
    """The exploit end to end: a search answer piped into call_tool must not run code.

    With generative UI on, ``generate_prefab_ui`` is in the catalog and is not a
    spec route. The multi-entry answer is what a client that pipes
    ``search_tools`` output into ``name`` sends, so the refusal has to come from
    the server, not from the caller choosing its entry.
    """
    import asyncio

    from fastmcp import Client

    monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")

    async def main():
        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(
                "call_tool",
                {
                    "name": [{"name": "generate_prefab_ui"}, {"name": "listProfiles"}],
                    "arguments": {"code": "print(1)"},
                },
                raise_on_error=False,
            )
        assert result.is_error
        text = " ".join(getattr(block, "text", "") for block in (result.content or []))
        assert "generate_prefab_ui" in text
        assert "write" in text

    asyncio.run(main())


class TestShapeRepairAnnouncesItself:
    """A re-targeted call says so on the payload and in the log (nemotron PRIN-5, qwen ARCH-5).

    Silent repair removes the caller's ability to notice a wrong discovery on a
    server that mutates NextDNS state: the executed name differs from the name
    sent, so the structured content carries `resolved_from` (what the caller
    actually sent, short) and the correction logs at WARNING, not INFO.
    """

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

    async def test_a_normalised_name_is_annotated_on_the_payload_and_warned(self, caplog):
        import logging

        from fastmcp import Client

        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tool_search"):
            async with Client(build_mcp_server()) as client:
                result = await client.call_tool(
                    "call_tool", {"name": "get_denylist", "arguments": {"profile_id": "abc123"}}, raise_on_error=False
                )
        content = json.dumps(getattr(result, "structured_content", None) or {})
        assert "/profiles/abc123/denylist" in content, content
        assert (getattr(result, "structured_content", None) or {}).get("resolved_from") == "get_denylist"
        assert (getattr(result, "structured_content", None) or {}).get("resolved_name") == "getDenylist"
        assert any(
            r.name == "nextdns_mcp.tool_search" and r.levelno == logging.WARNING and "once normalised" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_plain_name_carries_no_annotation_and_no_warning(self, caplog):
        import logging

        from fastmcp import Client

        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tool_search"):
            async with Client(build_mcp_server()) as client:
                result = await client.call_tool(
                    "call_tool", {"name": "getDenylist", "arguments": {"profile_id": "abc123"}}, raise_on_error=False
                )
        assert "resolved_from" not in (getattr(result, "structured_content", None) or {})
        assert not [
            r for r in caplog.records if r.name.startswith("nextdns_mcp.tool_search") and r.levelno >= logging.WARNING
        ]

    async def test_a_lifted_argument_repair_logs_at_warning(self, caplog):
        import logging

        from fastmcp import Client

        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tool_search"):
            async with Client(build_mcp_server()) as client:
                await client.call_tool(
                    "call_tool", {"name": "getDenylist", "profile_id": "abc123"}, raise_on_error=False
                )
        assert any(
            r.name == "nextdns_mcp.tool_search" and r.levelno == logging.WARNING and "beside name" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_search_answer_name_repair_logs_at_warning(self, caplog):
        import logging

        from fastmcp import Client

        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tool_search"):
            async with Client(build_mcp_server()) as client:
                found = await client.call_tool("search_tools", {"query": "list profiles"}, raise_on_error=False)
                await client.call_tool("call_tool", {"name": found.data[0], "arguments": {}}, raise_on_error=False)
        assert any(
            r.name == "nextdns_mcp.tool_search" and r.levelno == logging.WARNING and "top-ranked" in r.getMessage()
            for r in caplog.records
        )

    def test_a_non_dict_payload_passes_the_annotation_through_unchanged(self):
        from types import SimpleNamespace

        result = SimpleNamespace(structured_content=None)
        assert search_shape._annotate_resolved_from(result, "other", "realTool") is result
        assert search_shape._annotate_resolved_from(result, "realTool", "realTool") is result
        assert search_shape._annotate_resolved_from(result, "other", None) is result
        shaped = search_shape._annotate_resolved_from(
            SimpleNamespace(structured_content={"ok": True}), ["x"], "realTool"
        )
        assert shaped.structured_content["resolved_from"] == '["x"]'

    def test_a_prefab_payload_keeps_its_wire_shape_and_gets_a_content_note(self):
        """The re-target is still announced, but not inside the renderer's document.

        A Prefab UI payload (``$prefab``) is the renderer's wire contract, not
        this server's response shape: ``resolved_from``/``resolved_name``
        injected there would be read as document content. The annotation moves
        to the result content, beside the tool's own answer.
        """
        import mcp.types
        from fastmcp.tools import ToolResult

        payload = {"$prefab": {"version": "0.3"}, "view": {}}
        result = ToolResult(content=[mcp.types.TextContent(type="text", text="Rendered")], structured_content=payload)
        shaped = search_shape._annotate_resolved_from(result, "overview_dashboard", "overviewDashboard")
        assert shaped.structured_content == payload
        note = shaped.content[-1]
        assert note.type == "text"
        assert "overviewDashboard" in note.text and "overview_dashboard" in note.text

    async def test_the_description_says_a_write_headed_answer_is_refused_not_run(self, monkeypatch):
        from fastmcp import Client

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        async with Client(build_mcp_server()) as client:
            described = {t.name: (t.description or "") for t in await client.list_tools()}
        call = " ".join(described["call_tool"].split()).casefold()
        assert "refused rather than executed" in call


class TestCatalogReadFailure:
    """A catalog that cannot be read must not break the call it was read to shape (MAINT-5)."""

    async def test_a_failing_catalog_read_still_lets_the_call_proceed(self):
        from types import SimpleNamespace

        def exploding_catalog(context):
            raise RuntimeError("catalog unavailable")

        transform = SimpleNamespace(get_tool_catalog=exploding_catalog)
        context = SimpleNamespace(fastmcp_context=object())
        assert await ts.CallToolShapeMiddleware(transform)._catalog(context) == ()


class TestFastMCPPrivateSurface:
    def test_overridden_private_methods_still_exist(self):
        import fastmcp
        from fastmcp.server.transforms.search import BM25SearchTransform

        for name in ("_search", "_make_search_tool", "_make_call_tool", "_render_results"):
            assert hasattr(BM25SearchTransform, name), name
        # The pyproject floor, not an exact pin: uv.lock decides the resolved
        # version, and this guard fails when it drifts below the floor the
        # borrowed surface was validated against. Leading numeric segments so
        # a pre-release string fails the assert, not the parse.
        version = tuple(int(part) for part in re.findall(r"\d+", fastmcp.__version__)[:3])
        assert version >= (4, 1, 8), fastmcp.__version__


def test_a_loosely_matched_write_name_is_refused_with_the_catalog(monkeypatch):
    """A write runs only under its exact name: a loose match is refused, not run.

    Executing a write the caller never singled out is kept inside the reads:
    the refusal names what the words matched so the caller re-sends the exact
    name. The module's real helpers do the resolving — only the endpoint map
    is supplied, as the server wires it.
    """
    import asyncio

    from fastmcp.tools.base import Tool

    import nextdns_mcp.tool_search as ts

    tools = [Tool.from_function(lambda: None, name="deleteProfile")]
    middleware = ts.CallToolShapeMiddleware(endpoints={"deleteProfile": ("DELETE", "/profiles/{profile_id}")})

    async def main():
        with pytest.raises(ts.ToolError) as excinfo:
            await middleware._resolve_tool_name("profiles_delete", tools)
        message = str(excinfo.value)
        assert "'profiles_delete' is not a tool of this server" in message
        assert "deleteProfile takes" in message
        # The exact name still resolves: only the loose write is refused.
        resolved, target = await middleware._resolve_tool_name("deleteProfile", tools)
        assert resolved == "deleteProfile"
        assert target is tools[0]

    asyncio.run(main())


def test_the_fallback_classifies_writes_from_the_endpoint_map():
    """The no-match fallback sorts reads first using the spec's own methods.

    The name-shape heuristic it replaces read every list* tool as a write, so
    a model that found nothing got a page of mutations. build_search_transform
    wires the route parse into the transform; the fallback answers with the
    GETs ahead of the writes.
    """
    import asyncio

    from fastmcp.tools.base import Tool

    import nextdns_mcp.tool_search as ts

    tools = [
        Tool.from_function(lambda: None, name="addToDenylist"),
        Tool.from_function(lambda: None, name="deleteProfile"),
        Tool.from_function(lambda: None, name="listProfiles"),
        Tool.from_function(lambda: None, name="listRewrites"),
    ]
    endpoints = {
        "addToDenylist": ("POST", "/profiles/{profile_id}/denylist"),
        "deleteProfile": ("DELETE", "/profiles/{profile_id}"),
        "listProfiles": ("GET", "/profiles"),
        "listRewrites": ("GET", "/profiles/{profile_id}/rewrites"),
    }
    transform = ts.build_search_transform("bm25", endpoints=endpoints)
    assert transform is not None
    assert transform._endpoints == endpoints

    async def main():
        ranked = [t.name for t in await transform._search(tools, "qxqv zzxj no such tool words")]
        assert ranked.index("listProfiles") < ranked.index("addToDenylist")
        assert ranked.index("listProfiles") < ranked.index("deleteProfile")
        assert ranked.index("listRewrites") < ranked.index("addToDenylist")
        assert ranked.index("listRewrites") < ranked.index("deleteProfile")

    asyncio.run(main())


def test_the_fallback_reads_a_name_off_the_map_like_the_composites():
    """A name the map does not carry (plotAnalytics, dohLookup) sorts as a read."""
    import asyncio

    from fastmcp.tools.base import Tool

    import nextdns_mcp.tool_search as ts

    tools = [
        Tool.from_function(lambda: None, name="addToDenylist"),
        Tool.from_function(lambda: None, name="dohLookup"),
        Tool.from_function(lambda: None, name="plotAnalytics"),
    ]
    transform = ts.build_search_transform(
        "bm25", endpoints={"addToDenylist": ("POST", "/profiles/{profile_id}/denylist")}
    )
    assert transform is not None

    async def main():
        ranked = [t.name for t in await transform._search(tools, "qxqv zzxj no such tool words")]
        assert ranked.index("dohLookup") < ranked.index("addToDenylist")
        assert ranked.index("plotAnalytics") < ranked.index("addToDenylist")

    asyncio.run(main())


class TestQueryLengthCap:
    """Regression: the scope-strip regex backtracks quadratically on unbounded
    queries (measured: 8 KB ~1.4 s, 64 KB ~88 s, synchronously on every search).
    The query is capped at 512 characters before any scoring runs."""

    @pytest.mark.asyncio
    async def test_a_huge_query_scores_like_its_capped_prefix(self, monkeypatch):
        from fastmcp import Client

        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()
        huge = "profiles settings privacy " + "z" * 100_000
        async with Client(server) as client:
            # Uncapped, the quadratic regex alone needs minutes for 100 KB;
            # this completing at all is the regression signal.
            big = await client.call_tool("search_tools", {"query": huge}, raise_on_error=False)
            capped = await client.call_tool("search_tools", {"query": huge[:512]}, raise_on_error=False)
        assert [t.get("name") for t in big.data] == [t.get("name") for t in capped.data]


class TestAnalyticsBM25RankingContract:
    """Regression test: raw analytics queries must rank getAnalytics* #1 ahead of overviewDashboard."""

    @pytest.mark.asyncio
    async def test_raw_analytics_queries_rank_endpoint_tools_first(self, monkeypatch):
        from fastmcp import Client

        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        server = build_mcp_server()

        async with Client(server) as client:
            cases = [
                ("aggregate analytics domains", "getAnalyticsDomains"),
                ("analytics reasons", "getAnalyticsReasons"),
                ("analytics devices", "getAnalyticsDevices"),
            ]
            for query, expected_first in cases:
                res = await client.call_tool("search_tools", {"query": query})
                results = (res.structured_content or {}).get("result", [])
                assert results, f"No search results for query {query!r}"
                assert results[0]["name"] == expected_first, (
                    f"For query {query!r}, expected #1 to be {expected_first}, got {results[0]['name']}"
                )

            dash_res = await client.call_tool("search_tools", {"query": "show me a dashboard of my account"})
            dash_results = (dash_res.structured_content or {}).get("result", [])
            assert dash_results[0]["name"] == "overviewDashboard"
