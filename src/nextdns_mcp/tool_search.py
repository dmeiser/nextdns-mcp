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

import re
from collections.abc import Sequence

from fastmcp.server.transforms.search import BM25SearchTransform, RegexSearchTransform
from fastmcp.tools.base import Tool

# The NextDNS server exposes a small fixed catalog (the grouped CRUD tools),
# so a search should be able to surface all of it. The FastMCP default of 5
# can hide a relevant grouped tool behind five higher-scoring ones.
CATALOG_MAX_RESULTS = 20

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
        results = await super()._search(tools, query)  # type: ignore[misc]
        # The fallback still gets the name-aware ordering: when nothing matched,
        # the caller's own words are the only signal left about what it wants.
        return _rank_by_name_overlap(results or list(tools)[: self._max_results], query)

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
# assumes an agent CLI, a code-mode sandbox, or any particular call syntax.
_SEARCH_TOOL_DESCRIPTION = """Search this server's tools with the words of the task.

Returns the matching tools with their exact names and full schemas. Tool names
are NOT advertised by this server, so search first instead of guessing: a name
that was not returned here does not exist. One search per task is enough —
reuse its result instead of searching again.
"""

_CALL_TOOL_DESCRIPTION = """Run one tool by the exact name returned by search_tools.

Pass that name and its arguments. Never guess a name: anything search_tools did
not return will fail. If the call returns an error with a `hint`, follow the
hint instead of repeating the call.
"""


class FallbackBM25SearchTransform(_CatalogAwareSearchMixin, BM25SearchTransform):
    """BM25 search with a full-catalog fallback and name-aware ranking."""


class FallbackRegexSearchTransform(_CatalogAwareSearchMixin, RegexSearchTransform):
    """Regex search with a full-catalog fallback and name-aware ranking."""


def build_search_transform(mode: str) -> BM25SearchTransform | RegexSearchTransform | None:
    """Return the configured search transform for ``mode`` (``off`` -> None)."""
    if mode == "regex":
        return FallbackRegexSearchTransform(max_results=CATALOG_MAX_RESULTS)
    if mode == "bm25":
        return FallbackBM25SearchTransform(max_results=CATALOG_MAX_RESULTS)
    return None
