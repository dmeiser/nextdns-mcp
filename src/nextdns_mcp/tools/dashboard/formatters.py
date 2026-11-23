"""The text a dashboard call returns beside its UI.

A Prefab view is for the human; the ``content`` string is what the model reads back,
so it has to carry the numbers the card is showing. This module owns that text and
the item lists it is built from, and nothing else: it never touches the transport
and never builds a component.

SPDX-License-Identifier: MIT
"""

from typing import Any

from ...utils import sanitize_label
from .data import domain_label, item_queries


def _format_top_items(items: list[tuple[str, int]], limit: int = 5) -> str:
    """Format top items as 'name (count)' list.

    Names are sanitized at extraction/shaping time by the row and label
    helpers, so this only formats: a second ``sanitize_label`` pass would
    re-cap the ellipsis the first pass appended.
    """
    formatted = [f"{name} ({q:,})" for name, q in items[:limit]]
    return ", ".join(formatted)


def format_items_summary(title: str, noun: str | None, items: list[tuple[str, int]], total: int) -> str:
    """Format compact factual summary for a list-based visual card.

    Every card but GAFAM names its row count next to the total; GAFAM reports the
    query total alone, so it passes ``noun=None`` and the count is omitted.
    """
    top_str = _format_top_items(items)
    count_phrase = f"{len(items)} {noun}, " if noun is not None else ""
    base = f"[Rendered {title} ({count_phrase}{total:,} queries)"
    return f"{base}: {top_str}]" if top_str else f"{base}]"


def gafam_summary_items(chart_data: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Format (label, queries) pairs for the GAFAM dominance summary."""
    return [(row["label"], row["queries"]) for row in chart_data]


def ip_summary_items(data: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Format (ip, queries) pairs for IP analytics summary."""
    return [(sanitize_label(item.get("ip") or "-"), item_queries(item)) for item in data]


def overview_top_summary(resolved_data: list[dict[str, Any]], blocked_data: list[dict[str, Any]]) -> str:
    """Summarize leading resolved and blocked domains."""
    top_res = _format_top_items([(domain_label(item, "domain"), item_queries(item)) for item in resolved_data])
    top_blk = _format_top_items([(domain_label(item, "domain"), item_queries(item)) for item in blocked_data])
    return f"Top resolved: {top_res or 'none'}; Top blocked: {top_blk or 'none'}"
