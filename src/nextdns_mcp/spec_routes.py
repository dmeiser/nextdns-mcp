"""The offerable-route catalog: the document parsed once per process, shared by every consumer.

This module holds the one responsibility of turning ``nextdns-openapi.yaml``
into the sorted list of operations the server offers: the parse, the
``streamLogs`` withholding, and the process-wide cache behind it. The server
surface, the call-log endpoint map, the health probe, the harness and the
coverage check all ask the same question about the same file, and they all
get their answer from this one parse.

``streamLogs`` is the one operation the document describes that is not
offered: it is a Server-Sent Events stream, and an MCP tool call is
request/response, so the transport cannot carry it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastmcp.utilities.openapi import HTTPRoute, parse_openapi_to_http_routes

SPEC_PATH = Path(__file__).with_name("nextdns-openapi.yaml")


# See the module docstring.
WITHHELD_OPERATIONS = frozenset({"streamLogs"})


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    """Read the shipped OpenAPI document."""
    with path.open(encoding="utf-8") as handle:
        spec: dict[str, Any] = yaml.safe_load(handle)
    return spec


_OFFERABLE_ROUTES: tuple[HTTPRoute, ...] | None = None


def _is_offerable(route: HTTPRoute) -> bool:
    return bool(route.operation_id) and route.operation_id not in WITHHELD_OPERATIONS


def spec_routes(spec: dict[str, Any] | None = None) -> list[HTTPRoute]:
    """Every offerable operation, in stable order.

    Ordered by ``operationId`` rather than by the spec's path order so the tool
    list a client sees does not shift when an unrelated path is added to the spec.

    The shipped document parses once per process and the result is reused: the
    server surface, the call-log endpoint map, the health probe, the harness
    and the coverage check all ask the same question about the same file, and
    re-reading and re-parsing it for each was the cold-start cost. A caller
    that hands in a spec (tests do, with modified documents) always gets a
    fresh parse - the cache is for the shipped one only. The cache itself is
    stored as a tuple and never handed out: every call gets its own list, so
    a caller that mutates its result cannot corrupt what later calls see.
    """
    global _OFFERABLE_ROUTES
    if spec is None and _OFFERABLE_ROUTES is not None:
        return list(_OFFERABLE_ROUTES)
    parsed = parse_openapi_to_http_routes(load_spec() if spec is None else spec)
    offerable = tuple(sorted(filter(_is_offerable, parsed), key=lambda route: str(route.operation_id)))
    if spec is None:
        _OFFERABLE_ROUTES = offerable
    return list(offerable)


def endpoints_for_routes(routes: list[HTTPRoute] | None = None) -> dict[str, tuple[str, str]]:
    """``operationId -> (method, path template)``, for the call log.

    Derived from the same parsed routes the tools are registered from, so the
    log's endpoint column and the published surface come from one parse of one
    document.
    """
    return {
        str(route.operation_id): (str(route.method), str(route.path))
        for route in (spec_routes() if routes is None else routes)
    }


def spec_tool_names() -> list[str]:
    """The names this server publishes, without registering them.

    The ``initialize`` instructions describe the catalog before the tools exist.
    """
    return [str(route.operation_id) for route in spec_routes()]
