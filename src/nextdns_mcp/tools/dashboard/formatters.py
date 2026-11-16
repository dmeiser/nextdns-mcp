"""The text a dashboard call returns beside its UI.

A Prefab view is for the human; the ``content`` string is what the model reads back,
so it has to carry the numbers the card is showing. This module owns that text and
the item lists it is built from, and nothing else: it never touches the transport
and never builds a component.

SPDX-License-Identifier: MIT
"""

from typing import Any

from .data import _domain_label, _item_queries, _sanitize_label


def _format_top_items(items: list[tuple[str, int]], limit: int = 5) -> str:
    """Format top items as 'name (count)' list."""
    formatted = [f"{_sanitize_label(name)} ({q:,})" for name, q in items[:limit]]
    return ", ".join(formatted)


def _format_items_summary(title: str, noun: str, items: list[tuple[str, int]], total: int) -> str:
    """Format compact factual summary for a list-based visual card."""
    count = len(items)
    top_str = _format_top_items(items)
    if top_str:
        return f"[Rendered {title} ({count} {noun}, {total:,} queries): {top_str}]"
    return f"[Rendered {title} ({count} {noun}, {total:,} queries)]"


def _format_gafam_summary(chart_data: list[dict[str, Any]], total: int) -> str:
    """Format GAFAM dominance summary."""
    items = [(row["label"], row["queries"]) for row in chart_data]
    top_str = _format_top_items(items)
    if top_str:
        return f"[Rendered GAFAM Dominance ({total:,} queries): {top_str}]"
    return f"[Rendered GAFAM Dominance ({total:,} queries)]"


def _ip_summary_items(data: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Format (ip, queries) pairs for IP analytics summary."""
    return [(str(item.get("ip") or "-"), _item_queries(item)) for item in data]


def _overview_top_summary(r_data: list[dict[str, Any]], b_data: list[dict[str, Any]]) -> str:
    """Summarize leading resolved and blocked domains."""
    top_res = _format_top_items([(_domain_label(item, "domain"), _item_queries(item)) for item in r_data])
    top_blk = _format_top_items([(_domain_label(item, "domain"), _item_queries(item)) for item in b_data])
    return f"Top resolved: {top_res or 'none'}; Top blocked: {top_blk or 'none'}"
