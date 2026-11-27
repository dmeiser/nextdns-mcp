"""Progressive-disclosure search transforms tuned for small models.

FastMCP's ``BM25SearchTransform`` powers progressive tool disclosure:
``tools/list`` exposes only ``search_tools`` + ``call_tool``, and the model
discovers the real tools by searching. (FastMCP also offers a regex transform;
it is not mounted here — it ranks nothing, and no live validation ran under
it, so it was removed before shipping.)

The transform can return an EMPTY list when a query shares no tokens with any
tool, and it ranks by prose similarity rather than by what the caller asked to
*do*. Small models reliably query with guessed tool names (``updateProfile``,
``replace_parental_services``, ``add dns rewrite record``) rather than task
descriptions, so the search comes back empty and the model then invents a
non-existent tool and fails. This module adds two generic, task-independent
safety nets. Neither is a task hint — both behave identically for every query —
and both are client-agnostic (they live in the MCP server, not in a prompt):

1. Full-catalog fallback: when a search matches nothing, return the catalog.
2. Name-aware re-rank: a tool whose own NAME contains the caller's words ranks
   first, with BM25's order preserved on ties.

The serving side is split across three modules with distinct owners:

- :mod:`nextdns_mcp.tool_search_rank` — the ranking machinery (word folding,
  morphological variants, shared-word discounting, the name-overlap re-rank).
- :mod:`nextdns_mcp.tool_search_answer` — search-answer construction (the
  per-result compaction, the description cut, the byte budget, the serializer).
- :mod:`nextdns_mcp.tool_search_shape` — call-shape handling (resolving what a
  caller meant, lifting misplaced parameters, the write refusals).

This module mounts them: the transform's ``_search`` wires the ranking into
FastMCP's hook, and the fallback and the answer rendering hang off it. The two
synthetic entry points keep FastMCP's fixed names: ``search_tools`` is
plural and ``call_tool`` singular — the asymmetry is FastMCP's contract, not a
local inconsistency to rename.

SPDX-License-Identifier: MIT
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool

from .config import ConfigurationError
from .tool_search_answer import (
    SEARCH_ANSWER_MAX_BYTES,
    SEARCH_FALLBACK_MAX_BYTES,
    _budget_results,
    _compact_entry,
    _fallback_answer,
)
from .tool_search_rank import (
    _catalog_words_memo,
    _CatalogWordMemos,
    _CatalogWords,
    _named_tools,
    _rank_by_name_overlap,
    _strip_scope_mentions,
    _variant_words,
    _words,
)
from .tool_search_shape import _is_write_tool

CATALOG_MAX_RESULTS = 6

# Written for the model that reads them, and client-agnostic: nothing here
# assumes an agent CLI, a code-mode sandbox, or any particular call syntax, and
# nothing instructs the model how to behave — it describes what this server does.
# The argument shape is spelled out because it is part of the contract, and
# getting it wrong is the single most common way a call never reaches a tool:
# observed live, a model passed a bare string to search_tools and two objects to
# call_tool, and concluded from the rejections that the server had no tools.
_SEARCH_TOOL_DESCRIPTION = """Search this server's tools with the words of the task.

Takes one argument object, {"query": "<words of the task>"}. The answer is an
array of the matching tools in rank order, each an object with its exact `name`,
its `description`, and its full input schema. A name that search_tools did not
return, and that the server's instructions do not list, is not a tool of this
server.
"""

_CALL_TOOL_DESCRIPTION = """Run one of this server's tools by the exact name returned by search_tools.

Takes exactly one argument object, {"name": "<tool>", "arguments": {<that tool's
parameters>}}; the name and the arguments are not two separate inputs. `name`
selects one tool: one search result's `name` field. If the search answer itself
is passed instead - the array of results, or one result object - it resolves to
the highest-ranked tool in it, because the answer is in rank order; a
multi-result answer whose highest-ranked tool is a write is refused rather than
executed, and the refusal names the candidates so the call can be re-sent with
one name. Anything search_tools did not return is not a tool of this server. A
failed call returns a JSON payload with an error `code` and, when available, a
`hint`.
"""


class FallbackBM25SearchTransform(BM25SearchTransform):
    """BM25 search with a full-catalog fallback and name-aware ranking.

    Keeps the disclosure flow intact (tools/list still hides the real tools;
    the model must still search then call_tool) but makes discovery robust to
    unhelpful queries. FastMCP keeps ``_search``, ``_make_search_tool``,
    ``_make_call_tool`` and ``_render_results`` private, and
    ``BaseSearchTransform.__init__`` sets ``_max_results`` privately too; this
    subclass borrows exactly that surface, on the only subclass there is, and
    keeps the ``fastmcp>=4.1.8`` floor with it - the annotations below name the
    borrowed attributes so a FastMCP bump finds them instead of failing at
    request time.

    The catalog-derived word structures, the compacted per-tool payloads, and
    the two synthetic Tools are memoized on the instance: all of them are
    constants of a static catalog, and rebuilding them per query was the
    measured hot path.
    """

    _max_results: int  # private attribute set by BaseSearchTransform.__init__

    def __init__(
        self,
        *,
        max_results: int = 5,
        always_visible: list[str] | None = None,
        search_tool_name: str = "search_tools",
        call_tool_name: str = "call_tool",
        search_result_serializer: Any = None,
        endpoints: Mapping[str, tuple[str, str]],
    ) -> None:
        super().__init__(
            max_results=max_results,
            always_visible=always_visible,
            search_tool_name=search_tool_name,
            call_tool_name=call_tool_name,
            search_result_serializer=search_result_serializer,
        )
        # tool name -> (method, path template), from the server's single route
        # parse. The full-catalog fallback classifies writes from this map; a
        # name it does not carry - the three composite operations and the
        # generative-UI pair - is classified from its own readOnlyHint.
        self._endpoints: dict[str, tuple[str, str]] = dict(endpoints)
        self._word_memos = _CatalogWordMemos()
        # tool name -> (fingerprint, pins, compacted payload, byte cost); see
        # _cached_compaction for why the key is the name and not the instance.
        self._compacted_cache: dict[str, tuple[tuple[Any, ...], tuple[Any, ...], dict[str, Any], int]] = {}
        self._synthetic_tools: dict[str, Tool] = {}

    def _catalog_words(self, tools: Sequence[Tool]) -> _CatalogWords:
        """The catalog's word structures, memoized by its tool names (see _catalog_words_memo)."""
        return _catalog_words_memo(self._word_memos, tools)

    def _augment_query(self, query: str, variants: set[str]) -> str:
        """Widen a lexical query with the catalog's own other spellings.

        bm25 indexes raw text with no stemming, so the partner spellings ride
        along as extra terms (see _variant_words). Without variants, the
        caller's query is exactly what was asked and stays untouched.
        """
        if not variants:
            return query
        return " ".join([query, *sorted(variants)])

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        """BM25 with named-tool rescue and fallback, then the name re-rank.

        The re-rank runs over the FULL merged list and the slice comes last:
        slicing to max_results first cut tools that belonged in the answer
        before the name scorer ever saw them, worst on the fallback path where
        the ranking is the only signal left.
        """
        # A scope reference is scored out before anything else, because it is
        # the one phrase every task in this API contains and the one that most
        # often points the ranking at the wrong tool.
        query = query[:512] if isinstance(query, str) else query
        catalog = self._catalog_words(tools)
        stripped = _strip_scope_mentions(query, catalog.scope)
        # Only the scoring query widens; the name-inclusion below stays on the
        # caller's own words, which is what "named" has always meant. Shared
        # scope words are never widened - they name every tool at once.
        scored = self._augment_query(
            stripped,
            _variant_words(stripped, catalog.raw_names, exclude=catalog.name_shared | catalog.vocab_shared),
        )
        results = list(await super()._search(tools, scored) or [])
        # A tool whose own name carries the caller's word is never left out of
        # the answer. BM25 ranks prose, so which prose wins changes when a
        # description gets longer: publishing the full docstrings once moved
        # the `doh` match off the tool named for it, and that tool dropped out
        # of the result set entirely.
        words = _words(stripped)
        named = _named_tools(
            words,
            catalog.names,
            catalog.vocabulary,
            catalog.name_shared,
            catalog.vocab_shared,
            catalog.leading,
            catalog.generic_verbs,
        )
        if not results:
            # Nothing matched at all: the caller's words are the only signal
            # left, so answer with the catalog and let the name order sort it.
            # Write/read classification comes from the endpoint map (built once
            # from the shipped spec's routes), not from name shapes: the
            # heuristic this replaced read every list* tool as a write. A name
            # the map does not carry is classified from its own readOnlyHint,
            # so a tool that runs code is never sorted in with the reads.
            results = sorted(
                tools,
                key=lambda t: (_is_write_tool(getattr(t, "name", ""), self._endpoints, t), getattr(t, "name", "")),
            )
            # The fallback IS the catalog: an unmatchable query gets every tool
            # (reads first), not a six-entry sample - a small model that finds
            # nothing needs the surface, and six entries of it taught it the
            # catalog was six add* writes. Serialization holds the fallback to
            # its own whole-catalog budget (SEARCH_FALLBACK_MAX_BYTES) instead
            # of the ranked-answer 8 KB, which silently dropped most entries.
            fell_back = True
        else:
            fell_back = False
        merged = _merge_named_results(results, tools, named)
        shared = (catalog.name_shared, catalog.vocab_shared)
        ranked = _rank_by_name_overlap(merged, stripped, catalog.names, catalog.vocabulary, shared)
        _fallback_answer.set(fell_back)
        return ranked if fell_back else ranked[: self._max_results]

    def _make_search_tool(self) -> Tool:
        """FastMCP's search tool, with this server's discovery contract on it.

        Under disclosure these two synthetic tools are the only entries in
        ``tools/list``, so their descriptions are the always-visible place to
        state how the server is meant to be used. FastMCP's own wording is a
        one-liner that never says the catalog is hidden or that guessing fails.
        FastMCP rebuilds a Tool on every listing; these two are constants of
        this transform, so the first build is the one kept.
        """
        tool = self._synthetic_tools.get("search")
        if tool is None:
            tool = super()._make_search_tool()
            tool.description = _SEARCH_TOOL_DESCRIPTION
            self._synthetic_tools["search"] = tool
        return tool

    def _make_call_tool(self) -> Tool:
        """FastMCP's call_tool proxy, with this server's contract on it (cached)."""
        tool = self._synthetic_tools.get("call")
        if tool is None:
            tool = super()._make_call_tool()
            tool.description = _CALL_TOOL_DESCRIPTION
            self._synthetic_tools["call"] = tool
        return tool

    async def _render_results(self, tools: Sequence[Tool]) -> Any:
        """The search answer, rendered from the cached compaction (see _serialize_results)."""
        return self._serialize_results(tools)

    def _serialize_results(self, tools: Sequence[Tool]) -> list[dict[str, Any]]:
        """FastMCP's search shape, from the cache, under the mode's byte budget.

        Ranked answers are held to SEARCH_ANSWER_MAX_BYTES; the full-catalog
        fallback (every tool, reads first) is held to SEARCH_FALLBACK_MAX_BYTES
        and must contain every catalog entry - a fallback that dropped entries
        silently taught the caller the catalog was smaller than it is.
        The answer is a fresh LIST per render, so a caller that reorders,
        replaces, or clears the answer it holds cannot change the next one. Its
        entries are the cached payloads themselves. The cache is read-only by
        convention, not by enforcement: nothing on the mounted path writes to
        an answer - FastMCP serializes the returned list straight to the wire,
        so every client receives its own decoded copy - but an in-process
        caller that edits a returned entry writes into the cache, and the write
        is visible to later answers for that tool. That is the same boundary
        the compacted payload already had one level down, where `inputSchema`
        is the cache's own object either way; the per-entry shallow copy this
        replaced paid a dict per kept entry per answer to fence only the top
        level of it.
        """
        budget = SEARCH_FALLBACK_MAX_BYTES if _fallback_answer.get() else SEARCH_ANSWER_MAX_BYTES
        try:
            return _budget_results([self._cached_compaction(tool) for tool in tools], budget)
        finally:
            _fallback_answer.set(False)

    def _cached_compaction(self, tool: Tool) -> tuple[dict[str, Any], int]:
        """One tool's compacted payload and byte cost, cached per tool name.

        Keyed on the tool's NAME with a metadata fingerprint (see
        _tool_fingerprint), not on the Tool instance: FastMCP hands the search
        catalog a fresh ``model_copy`` of every app tool on every read, so an
        instance key missed the cache on each search and leaked one entry per
        app tool per query for the life of the server. A fingerprint hit
        reuses the payload; a miss - a re-registered tool, a changed
        description - rebuilds it and REPLACES the entry, so the cache holds
        one entry per tool name however often the catalog is re-read.

        What an answer receives is the cached payload dict itself, handed out
        as-is. The cache is read-only by convention, not enforcement: nothing
        in this module writes to a payload after it is built, and isolation
        for callers is guaranteed at the wire, not inside the process (see
        _serialize_results for the boundary an in-process write crosses).
        """
        fingerprint = _tool_fingerprint(tool)
        cached = self._compacted_cache.get(tool.name)
        if cached is None or cached[0] != fingerprint:
            dumped, cost = _compact_entry(tool)
            # The identity-compared fingerprint slots ride along as strong
            # references, so an id cannot be recycled under a live entry.
            pins = (
                tool.parameters,
                tool.output_schema,
                tool.annotations,
                tool.icons,
                tool.execution,
                tool.task_config,
            )
            cached = (fingerprint, pins, dumped, cost)
            self._compacted_cache[tool.name] = cached
        return cached[2], cached[3]


def _meta_key(meta: Mapping[str, Any] | None) -> str | None:
    """A content key for a tool's meta dict, which a per-read copy rebuilds.

    The wire meta of an app tool is a fresh dict on every catalog read with
    the same content, so identity cannot key it; the dict is small (the UI
    wiring), so content can. ``default=str`` keeps a non-JSON value from
    failing the render the key only guards.
    """
    if not meta:
        return None
    return json.dumps(meta, sort_keys=True, default=str)


def _tool_fingerprint(tool: Tool) -> tuple[Any, ...]:
    """A cheap change-detector for the metadata ``_compact_entry`` renders.

    Compares by IDENTITY the structures a shallow ``model_copy`` shares with
    the registered tool (the schemas, annotations, icons, execution and task
    config - the heavy fields, and the reason a per-read copy of an unchanged
    tool must hit the cache), and by CONTENT the small values a copy rebuilds
    or the dump derives (name, version, title, description, tags, and the
    wire meta). The entry pins the identity-compared objects, so an id cannot
    be recycled under a live entry; a re-registered tool with new metadata
    misses and replaces. An in-place mutation of a shared schema is not
    detected - the instance key this replaced had the same blind spot.
    """
    return (
        tool.name,
        tool.version,
        tool.title,
        tool.description,
        id(tool.parameters),
        id(tool.output_schema),
        id(tool.annotations),
        id(tool.icons),
        id(tool.execution),
        id(tool.task_config),
        tuple(sorted(tool.tags)),
        _meta_key(tool.meta),
    )


def _partition_named(results: Sequence[Tool], named: set[str]) -> tuple[list[Tool], list[Tool]]:
    """Split ranked results into the ones the caller named and the ones it didn't."""
    picked: list[Tool] = []
    rest: list[Tool] = []
    for tool in results:
        (picked if tool.name in named else rest).append(tool)
    return picked, rest


def _merge_named_results(results: Sequence[Tool], tools: Sequence[Tool], named: set[str]) -> list[Tool]:
    """Put every tool the caller named into the answer, keeping BM25's order.

    A tool the caller named is never left out, but one BM25 already ranked keeps
    the place BM25 gave it: when two tools tie on the words they carry, the tie
    should fall back to how well their descriptions matched, not to the order
    the tools happen to be registered in. So: named-and-ranked first, then
    named-but-unranked, then everything else in ranked order.
    """
    picked, rest = _partition_named(results, named)
    ranked_names = {tool.name for tool in results}
    missing = [tool for tool in tools if tool.name in named and tool.name not in ranked_names]
    return picked + missing + rest


def build_search_transform(mode: str, endpoints: Mapping[str, tuple[str, str]]) -> BM25SearchTransform | None:
    """Return the configured search transform for ``mode`` (``off`` -> None).

    ``endpoints`` is the ``tool name -> (method, path template)`` map from the
    server's route parse. The factory wires it into the transform so the
    full-catalog fallback classifies reads and writes from the spec itself -
    the fallback needs the map because a name-shape guess misreads every
    list* tool as a write.

    Any other mode raises: ``config`` validates NEXTDNS_TOOL_SEARCH against
    its own accepted list, and the factory must not turn a mode added there
    into silent "no disclosure" - this raise is the seam where the two lists
    meet, so a mode this module has not implemented fails loudly at startup
    instead of quietly disabling discovery.
    """
    if mode == "bm25":
        return FallbackBM25SearchTransform(max_results=CATALOG_MAX_RESULTS, endpoints=endpoints)
    if mode == "off":
        return None
    raise ConfigurationError(f"Unknown tool-search mode {mode!r}; expected 'bm25' or 'off'.")


# Re-exported for compatibility: the ranking, answer-construction and call-shape
# modules each own their concern; these names keep the original
# ``nextdns_mcp.tool_search`` import surface working for tests and callers.
from fastmcp.exceptions import ToolError

from .constants import is_write_operation
from .tool_search_answer import (  # noqa: F401
    SEARCH_DESCRIPTION_CHARS,
    _compact_search_results,
    _fit_under_budget,
    _truncate_description,
)
from .tool_search_rank import (  # noqa: F401
    _WORD_SPLIT,
    _name_words,
    _tool_properties,
    _vocabulary_words,
    _word_scorer,
)
from .tool_search_shape import (  # noqa: F401
    CallToolShapeMiddleware,
    _ambiguous_write_message,
    _annotate_resolved_from,
    _annotation_says_write,
    _closest_tools,
    _dict_entry_name,
    _dict_or_empty,
    _flat_equal_matches,
    _match_tool_name,
    _moved_into_arguments,
    _name_from_search_answer,
    _named_entries,
    _note_search_answer_name,
    _passthrough_resolution,
    _required_arguments,
    _resolved_entry,
    _supplied_name_label,
    _unknown_tool_message,
    _word_equal_matches,
)
from .utils import schema_required

__all__ = [
    "CATALOG_MAX_RESULTS",
    "SEARCH_ANSWER_MAX_BYTES",
    "SEARCH_DESCRIPTION_CHARS",
    "SEARCH_FALLBACK_MAX_BYTES",
    "CallToolShapeMiddleware",
    "FallbackBM25SearchTransform",
    "ToolError",
    "build_search_transform",
    "is_write_operation",
    "schema_required",
]
