"""Progressive-disclosure search transforms tuned for small models.

The FastMCP search transforms (``BM25SearchTransform`` / ``RegexSearchTransform``)
power progressive tool disclosure: ``tools/list`` exposes only ``search_tools``
+ ``call_tool``, and the model discovers the real tools by searching.

Both transforms return an EMPTY list when a query shares no tokens with any
tool. Small models reliably query with *guessed tool names* (``updateProfile``,
``replace_parental_services``, ``add dns rewrite record``) rather than task
descriptions, so the search comes back empty and the model then invents a
non-existent tool and fails. This module adds a generic, task-independent
safety net: when a search matches nothing, return the whole (small) catalog.
It is not a task hint — the fallback is identical for every query — and it is
client-agnostic (it lives in the MCP server, not in any harness prompt).

SPDX-License-Identifier: MIT
"""

from collections.abc import Sequence

from fastmcp.server.transforms.search import BM25SearchTransform, RegexSearchTransform
from fastmcp.tools.base import Tool

# The NextDNS server exposes a small fixed catalog (the grouped CRUD tools),
# so a search should be able to surface all of it. The FastMCP default of 5
# can hide a relevant grouped tool behind five higher-scoring ones.
CATALOG_MAX_RESULTS = 20


class _CatalogFallbackMixin:
    """Fall back to the full catalog when a search matches nothing.

    Mixin for the FastMCP search transforms. Keeps the disclosure flow intact
    (tools/list still hides the real tools; the model must still search then
    call_tool) but makes discovery robust to unhelpful queries.
    """

    _max_results: int

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        results = await super()._search(tools, query)  # type: ignore[misc]
        if not results:
            return list(tools)[: self._max_results]
        return results


class FallbackBM25SearchTransform(_CatalogFallbackMixin, BM25SearchTransform):
    """BM25 search with a full-catalog fallback for empty results."""


class FallbackRegexSearchTransform(_CatalogFallbackMixin, RegexSearchTransform):
    """Regex search with a full-catalog fallback for empty results."""


def build_search_transform(mode: str) -> BM25SearchTransform | RegexSearchTransform | None:
    """Return the configured search transform for ``mode`` (``off`` -> None)."""
    if mode == "regex":
        return FallbackRegexSearchTransform(max_results=CATALOG_MAX_RESULTS)
    if mode == "bm25":
        return FallbackBM25SearchTransform(max_results=CATALOG_MAX_RESULTS)
    return None
