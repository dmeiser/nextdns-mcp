"""Search-answer construction: compaction, truncation and the byte budget.

Split out of ``tool_search`` so the serving side (the transform and its
fallback) and the answer side (what a search answer carries) stay separately
readable. Everything here is a pure function of the tools it is handed: the
per-tool compaction, the per-result description cut, and the skip-and-continue
byte scan that bounds the answer. The transform owns the memoization (the
cached compaction); this module owns the bytes.

SPDX-License-Identifier: MIT
"""

import contextvars
import json
from collections.abc import Sequence
from typing import Any

from fastmcp.tools.base import Tool

from .utils import _truncate_at_boundary

# Per-result budget for the description in a search answer. Sized so the parts
# that decide the call - the list types, the operations, the metrics - fit, and
# the parts that do not (long prose, worked examples) do not ride along on every
# discovery.
SEARCH_DESCRIPTION_CHARS = 1400

# Budget for the whole answer. Four fully-described tools still measured ~10 KB,
# and the run that carried 10-18 KB per discovery stalled 27 times in 29 steps.
SEARCH_ANSWER_MAX_BYTES = 8000

# Budget for the full-catalog fallback answer (a query that matched nothing).
# The fallback IS the catalog: the caller found nothing and needs the whole
# surface, so it must never silently drop an entry the way the ranked-answer
# budget can. Measured against the real 78-tool catalog with descriptions at
# the per-result cut, the whole-catalog answer is ~100 KB, so the fallback
# gets its own ceiling well above that rather than sharing the ranked 8 KB.
# (FastMCP also sends the answer twice, text + structured, so the wire size is
# ~2x the content size on every path - see compact_entry.)
SEARCH_FALLBACK_MAX_BYTES = 131_072

# Whether the search currently being rendered took the full-catalog fallback,
# which is what chooses between the two budgets above. Set by the transform's
# _search, consumed by its serializer. It lives beside the budgets it selects;
# it is a context var, not an instance attribute: concurrent searches in one
# process must not see each other's mode, and asyncio copies the context per
# task.
fallback_answer: contextvars.ContextVar[bool] = contextvars.ContextVar("nextdns_fallback_answer", default=False)

# The set-token for the flag above, so the serializer can RESET it to whatever
# the context held before the search instead of stamping a hardcoded False over
# it. The reset is what keeps a mode from outliving the answer it belongs to: a
# search that never reached its render, and any mode an enclosing context had
# already published, would otherwise decide the budget of the next answer in
# the same task.
_fallback_answer_token: contextvars.ContextVar[contextvars.Token[bool] | None] = contextvars.ContextVar(
    "nextdns_fallback_answer_token", default=None
)


def begin_fallback_answer(fell_back: bool) -> None:
    """Publish the mode of the search now being answered.

    Any mode an earlier search in this task left unpublished is restored
    first, so the flag always describes the newest search.
    """
    end_fallback_answer()
    _fallback_answer_token.set(fallback_answer.set(fell_back))


def end_fallback_answer() -> None:
    """Restore the mode the context held before the search being answered."""
    token = _fallback_answer_token.get()
    if token is not None:
        fallback_answer.reset(token)
        _fallback_answer_token.set(None)


def _truncate_description(dumped: dict[str, Any]) -> None:
    """Cut one dumped tool definition's description to the per-result budget.

    The cut lands on a line boundary at or after the halfway point of the
    budget; a description with no qualifying line boundary that late (a hard-
    wrapped docstring can wrap mid-sentence) is cut at the budget itself, so
    the cut CAN be mid-sentence. The input schema, which decides whether a
    call is valid, is never touched.
    """
    description = str(dumped.get("description") or "")
    if len(description) <= SEARCH_DESCRIPTION_CHARS:
        return
    dumped["description"] = _truncate_at_boundary(description, SEARCH_DESCRIPTION_CHARS) + "\n[description truncated]"


def compact_entry(tool: Tool) -> tuple[dict[str, Any], int]:
    """FastMCP's dump of one tool, description cut, with its whole-answer byte cost.

    The cost is measured on UTF-8 BYTES, not characters: the budget bounds the
    answer's CONTENT size, and a non-ASCII description costs more bytes than
    the ``len(json.dumps(...))`` character count reported. (The content budget
    is not the wire size: FastMCP serializes the answer as both text and
    structured content, so roughly 2x the content crosses the wire.)
    """
    dumped = tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
    _truncate_description(dumped)
    return dumped, len(json.dumps(dumped, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def budget_results(entries: Sequence[tuple[dict[str, Any], int]], budget: int | None = None) -> list[dict[str, Any]]:
    """Assemble a search answer under ``budget`` bytes of answer content.

    The accounting is skip-and-continue, not break: a result that does not fit
    is skipped and the scan goes on, so one oversized early result cannot
    suppress every smaller one after it (breaking at the first overflow was
    what it did, and the smaller later candidates were the ones the caller
    still needed). The FIRST result is always kept, even when it alone
    overflows the budget - an empty answer is worse than a big one. Ranked
    answers rely on that skip; the full-catalog fallback uses a budget that
    holds every entry (SEARCH_FALLBACK_MAX_BYTES), so nothing is skipped there.
    Framing is counted too: every result after the first rides in the JSON
    array behind a comma, so it carries that ``len(",")`` byte of overhead.
    ``budget`` defaults to the ranked-answer ceiling, resolved as a module
    global at call time so tests can monkeypatch it.
    """
    if budget is None:
        budget = SEARCH_ANSWER_MAX_BYTES
    return _fit_under_budget(entries, budget)


def _fit_under_budget(entries: Sequence[tuple[dict[str, Any], int]], budget: int) -> list[dict[str, Any]]:
    """The skip-and-continue scan itself (see budget_results for the contract)."""
    results: list[dict[str, Any]] = []
    spent = 0
    for dumped, cost in entries:
        framing = len(",") if results else 0
        if results and spent + framing + cost > budget:
            continue
        spent += framing + cost
        results.append(dumped)
    return results


def _compact_search_results(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    """FastMCP's search-result shape, with each description cut to a budget.

    A tool's own description is its full contract, and under disclosure the
    search answer is the only place a caller reads it. But the answer is also
    the biggest thing the model has to hold per discovery: with the full text
    and four results it measured 10-18 KB for a one-line task, and the run that
    did that stalled 27 times across 29 steps. So the search payload carries a
    bounded excerpt - cut at a line boundary when one exists early enough,
    otherwise hard-cut at the budget, which can land mid-sentence - and the
    input schema, which is the part that decides whether the call is valid, is
    never cut. The transform's cached serializer is the mounted one; this module-level
    form is the uncached reference version.
    """
    return budget_results([compact_entry(tool) for tool in tools])
