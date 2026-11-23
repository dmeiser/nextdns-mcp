"""The caller-facing description string for one generated tool.

This module holds the one responsibility of rendering the description a
caller sees for a generated tool: the spec's own summary and description kept
in the order a caller searches with, plus the endpoint line that says which
API call a name executes, truncated to the published cap. The spec is the
only source of the words; nothing here restates or adds to them.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from fastmcp.utilities.openapi import HTTPRoute

from .utils import _truncate_at_boundary, schema_required

# Bound on a published description. The spec's longest is 545 characters; the cap
# exists so a future spec edit cannot silently make one tool dominate every search
# answer the server gives back.
DESCRIPTION_MAX_CHARS = 1200


def _truncate_description(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, preferring a sentence/paragraph boundary."""
    return _truncate_at_boundary(text, limit, sentence=True)


def describe(route: HTTPRoute) -> str:
    """The published description: the spec's words, plus the endpoint it binds.

    The summary is kept ahead of the description because it is the phrase a caller
    searches with ("Get top domains analytics"), and FastMCP's own default picks one
    or the other. The endpoint line tells a caller which API call a name executes
    without having to guess how the tool is implemented.
    """
    summary = (route.summary or "").strip()
    detail = (route.description or "").strip()
    parts = [part for part in (summary, detail) if part]
    parts.append(f"NextDNS API: {route.method} {route.path}")
    text = "\n\n".join(parts)
    return _truncate_description(text, DESCRIPTION_MAX_CHARS)


def _required(route: HTTPRoute) -> list[str]:
    """The arguments the published schema marks required, in the caller's terms."""
    return schema_required(route.flat_param_schema)
