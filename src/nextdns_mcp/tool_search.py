"""Progressive-disclosure search transforms tuned for small models.

The FastMCP search transforms (``BM25SearchTransform`` / ``RegexSearchTransform``)
power progressive tool disclosure: ``tools/list`` exposes only ``search_tools``
+ ``call_tool``, and the model discovers the real tools by searching.

Both transforms can return an EMPTY list when a query shares no tokens with any
tool, and both rank by prose similarity rather than by what the caller asked to
*do*. Small models reliably query with guessed tool names (``updateProfile``,
``replace_parental_services``, ``add dns rewrite record``) rather than task
descriptions, so the search comes back empty and the model then invents a
non-existent tool and fails. This module adds two generic, task-independent
safety nets. Neither is a task hint — both behave identically for every query —
and both are client-agnostic (they live in the MCP server, not in a prompt):

1. Full-catalog fallback: when a search matches nothing, return the catalog.
2. Name-aware re-rank: a tool whose own NAME contains the caller's words ranks
   first, with BM25's order preserved on ties.

SPDX-License-Identifier: MIT
"""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.search import BM25SearchTransform, RegexSearchTransform
from fastmcp.tools.base import Tool

logger = logging.getLogger(__name__)

# The NextDNS server exposes a small fixed catalog (the grouped CRUD tools), so
# a search should be able to surface all of it - but not at any cost. Each
# result carries the tool's whole description, and a live run measured the
# answer at 10-18 KB (about 4k tokens) for a one-line task: the actor then
# stalled 27 times across 29 steps, and 13 of those steps never reached a tool
# at all. Four results plus name-matched inclusion keeps the answer near the
# size it was before the full descriptions went live.
CATALOG_MAX_RESULTS = 4

# Per-result budget for the description in a search answer. Sized so the parts
# that decide the call - the list types, the operations, the metrics - fit, and
# the parts that do not (long prose, worked examples) do not ride along on every
# discovery.
SEARCH_DESCRIPTION_CHARS = 1400

# Budget for the whole answer. Four fully-described tools still measured ~10 KB,
# and the run that carried 10-18 KB per discovery stalled 27 times in 29 steps.
SEARCH_ANSWER_MAX_BYTES = 8000

# camelCase / snake_case / non-word splitter, used for both tool names and
# queries so 'queryTimeSeries' and 'query time series' compare equal.
_WORD_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[_\W]+")


def _words(text: str) -> set[str]:
    """Split a tool name or query into comparable words.

    Trailing 's' is stripped from longer words so the plural of a tool's name
    ('manageProfiles' -> 'profile') matches the caller's wording ('delete
    profile'). This is deliberately crude: it only needs to make a name and a
    short query comparable, not to parse language.
    """
    words = set()
    for raw in _WORD_SPLIT.split(text):
        word = raw.casefold()
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        if word:
            words.add(word)
    return words


def _rank_by_name_overlap(tools: Sequence[Tool], query: str) -> list[Tool]:
    """Order matched tools by how many of the query's words appear in their name.

    BM25 ranks by prose similarity, so a long description can outrank the tool
    whose name is literally what the caller asked for: 'generate analytics plot'
    puts queryAnalytics above plotAnalytics, and 'delete profile' puts four other
    tools above manageProfiles. Ties keep the underlying ranking.
    """
    query_words = _words(query)
    if not query_words:
        return list(tools)
    indexed = list(enumerate(tools))
    indexed.sort(key=lambda pair: (-len(query_words & _words(pair[1].name)), pair[0]))
    return [tool for _, tool in indexed]


class _CatalogAwareSearchMixin:
    """Full-catalog fallback for empty results, plus the name-aware re-rank.

    Mixin for the FastMCP search transforms. Keeps the disclosure flow intact
    (tools/list still hides the real tools; the model must still search then
    call_tool) but makes discovery robust to unhelpful queries.
    """

    _max_results: int

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        results = list(await super()._search(tools, query) or [])  # type: ignore[misc]
        # A tool whose own name carries the caller's word is never left out of
        # the answer. BM25 ranks prose, so which prose wins changes when a
        # description gets longer: publishing the full docstrings took `doh`
        # from dohLookup to queryAnalytics, and the tool named for the request
        # dropped out of the result set entirely.
        named = [tool for tool in tools if _words(query) & _words(tool.name)]
        if not results:
            # Nothing matched at all: the caller's words are the only signal
            # left, so answer with the catalog and let the name order sort it.
            results = list(tools)
        seen = {tool.name for tool in named}
        merged = named + [tool for tool in results if tool.name not in seen]
        return _rank_by_name_overlap(merged[: self._max_results], query)

    def _make_search_tool(self) -> Tool:
        """FastMCP's search tool, with this server's discovery contract on it.

        Under disclosure these two synthetic tools are the only entries in
        ``tools/list``, so their descriptions are the always-visible place to
        state how the server is meant to be used. FastMCP's own wording is a
        one-liner that never says the catalog is hidden or that guessing fails.
        """
        tool = super()._make_search_tool()  # type: ignore[misc]
        tool.description = _SEARCH_TOOL_DESCRIPTION
        return tool

    def _make_call_tool(self) -> Tool:
        tool = super()._make_call_tool()  # type: ignore[misc]
        tool.description = _CALL_TOOL_DESCRIPTION
        return tool


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
its `description`, and its full input schema. This server does not advertise
tool names, so a name that was not returned here is not one of its tools.
"""

_CALL_TOOL_DESCRIPTION = """Run one of this server's tools by the exact name returned by search_tools.

Takes exactly one argument object, {"name": "<tool>", "arguments": {<that tool's
parameters>}}; the name and the arguments are not two separate inputs. `name`
selects one tool: one search result's `name` field. If the search answer itself
is passed instead - the array of results, or one result object - it resolves to
the highest-ranked tool in it, because the answer is in rank order. Anything
search_tools did not return is not a tool of this server. A failed call returns
a JSON payload with an error `code` and, when available, a `hint`.
"""


class FallbackBM25SearchTransform(_CatalogAwareSearchMixin, BM25SearchTransform):
    """BM25 search with a full-catalog fallback and name-aware ranking."""


class FallbackRegexSearchTransform(_CatalogAwareSearchMixin, RegexSearchTransform):
    """Regex search with a full-catalog fallback and name-aware ranking."""


def _compact_search_results(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    """FastMCP's search-result shape, with each description cut to a budget.

    A tool's own description is its full contract, and under disclosure the
    search answer is the only place a caller reads it. But the answer is also
    the biggest thing the model has to hold per discovery: with the full text
    and four results it measured 10-18 KB for a one-line task, and the run that
    did that stalled 27 times across 29 steps. So the search payload carries a
    bounded excerpt - cut at a line boundary, never mid-sentence - and the
    input schema, which is the part that decides whether the call is valid, is
    never cut.
    """
    results: list[dict[str, Any]] = []
    spent = 0
    for tool in tools:
        dumped = tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        description = str(dumped.get("description") or "")
        if len(description) > SEARCH_DESCRIPTION_CHARS:
            cut = description.rfind("\n", 0, SEARCH_DESCRIPTION_CHARS)
            if cut < SEARCH_DESCRIPTION_CHARS // 2:
                cut = SEARCH_DESCRIPTION_CHARS
            dumped["description"] = description[:cut].rstrip() + "\n[description truncated]"
        # The whole answer is bounded too: four fully-described tools is still
        # ~10 KB, and it is the lower-ranked ones that are least likely to be
        # the tool the caller wanted. At least one result is always returned.
        cost = len(json.dumps(dumped, sort_keys=True))
        if results and spent + cost > SEARCH_ANSWER_MAX_BYTES:
            break
        spent += cost
        results.append(dumped)
    return results


def _name_from_search_answer(value: Any) -> str:
    """The tool name a caller meant, when it sent the search answer instead.

    The single most common way a call never reaches a tool, seen repeatedly
    across live runs, is this expression::

        search_tools({query}).then(result => call_tool({ name: result.result, … }))

    `result.result` is the answer - an array of tool objects - and FastMCP
    rejects it against call_tool's own `name: str`, so nothing runs. The answer
    is in rank order and the caller passed all of it, so the top-ranked entry is
    the only defensible reading; taking it is not a guess about intent.

    Returns '' when the value cannot be read as a name, leaving the schema
    error to fire unchanged.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) else ""
    if isinstance(value, list):
        for entry in value:
            name = _name_from_search_answer(entry)
            if name:
                return name
    return ""


class SearchAnswerNameMiddleware(Middleware):
    """Resolve a `call_tool` name that arrived as the search answer.

    Runs before argument validation (like the other argument-shaping
    middleware here), so the call reaches the tool instead of being refused by
    the synthetic tool's own schema.
    """

    def __init__(self, call_tool_name: str = "call_tool") -> None:
        self._call_tool_name = call_tool_name

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        message = getattr(context, "message", None)
        arguments = getattr(message, "arguments", None)
        if getattr(message, "name", "") == self._call_tool_name and isinstance(arguments, dict):
            supplied = arguments.get("name")
            resolved = _name_from_search_answer(supplied)
            if resolved and resolved != supplied:
                logger.info(
                    "call_tool name given as %s; resolved to the top-ranked %r", type(supplied).__name__, resolved
                )
                context.message.arguments = {**arguments, "name": resolved}
        return await call_next(context)


def build_search_transform(mode: str) -> BM25SearchTransform | RegexSearchTransform | None:
    """Return the configured search transform for ``mode`` (``off`` -> None)."""
    if mode == "regex":
        return FallbackRegexSearchTransform(
            max_results=CATALOG_MAX_RESULTS, search_result_serializer=_compact_search_results
        )
    if mode == "bm25":
        return FallbackBM25SearchTransform(
            max_results=CATALOG_MAX_RESULTS, search_result_serializer=_compact_search_results
        )
    return None
