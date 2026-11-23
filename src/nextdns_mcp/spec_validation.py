"""The pre-flight schema check: arguments vs the published schema -> typed refusal.

This module holds the one responsibility of validating a call's arguments
against the endpoint's published ``inputSchema`` before any request is
constructed, and rendering every violation as the typed ``invalid_argument``
payload the server documents. The whole argument tree is checked; only the
rendered few are kept, and the counting pass is bounded.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

import jsonschema
from fastmcp.utilities.openapi import HTTPRoute

from .errors import ErrorCode, error_payload
from .spec_arguments import _mapped_location, _query_value

# How many schema violations one payload names before saying "and N more".
_SCHEMA_ERROR_LIMIT = 5


# Cap on one described violation. jsonschema quotes the offending value into
# the message, so a caller who sends a 5 MB string in place of an enum value
# would get that string quoted straight back - and kept (the call log's error
# column, the run transcript). Naming the violation needs its first lines,
# not its payload.
_SCHEMA_ERROR_MSG_CHARS = 300


# Cap on how far past the kept five violations the count runs before the
# iteration stops and reports the tail as a lower bound ("and at least N
# more"). An array body crafted to produce one violation per element cannot
# make this server walk - or remember - a violation per element.
_SCHEMA_ERROR_COUNT_CAP = 1_000


def _schema_problems(validator: jsonschema.Draft202012Validator, arguments: dict[str, Any]) -> tuple[list[Any], int]:
    """Consume ``iter_errors`` incrementally: keep at most LIMIT, count at most LIMIT+CAP.

    ``list(validator.iter_errors(...))`` materialized *every* violation, each
    holding its offending subtree, while the answer renders only the first
    ``_SCHEMA_ERROR_LIMIT`` and uses the rest as a bare count. Streaming the
    generator bounds what is retained to the rendered five, bounds the
    counting pass to ``_SCHEMA_ERROR_COUNT_CAP`` past them, and leaves the
    full argument tree just as fully checked.
    """
    kept: list[Any] = []
    extra = 0
    for error in validator.iter_errors(arguments):
        if len(kept) < _SCHEMA_ERROR_LIMIT:
            kept.append(error)
            continue
        extra += 1
        if extra >= _SCHEMA_ERROR_COUNT_CAP:
            break
    return kept, extra


def _without_nulls(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop explicitly-null arguments: null means "not provided" (see _query_arguments)."""
    return {name: value for name, value in arguments.items() if value is not None}


def _schema_error_where(error: jsonschema.ValidationError) -> str:
    """Where in the arguments a schema violation sits, in the caller's terms."""
    return ".".join(str(part) for part in error.absolute_path) or "<arguments>"


def _describe_schema_errors(kept: list[Any], extra: int) -> str:
    named = [f"{_schema_error_where(error)}: {error.message[:_SCHEMA_ERROR_MSG_CHARS]}" for error in kept]
    if extra >= _SCHEMA_ERROR_COUNT_CAP:
        return "; ".join(named) + f"; and at least {extra} more"
    return "; ".join(named) + (f"; and {extra} more" if extra > 0 else "")


def _normalized_for_validation(route: HTTPRoute, arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments with the query normalizers applied, for schema validation.

    The published schema describes what the API accepts; the normalizers
    (``_QUERY_NORMALIZERS``, recorded in the document's ``x-mcp-divergences``)
    rewrite caller input into exactly that form on the way out. Validating
    the raw arguments would reject the boolean ``partials=false`` the
    normalizer exists to serve - the deviation would become a refusal. So the
    schema check runs against what the server would actually send.
    """
    parameter_map = route.parameter_map or {}
    merged = dict(arguments)
    for name, value in arguments.items():
        openapi_name, location = _mapped_location(parameter_map, name)
        if location == "query":
            merged[name] = _query_value(openapi_name, value)
    return merged


def _schema_validation_payload(
    validator: jsonschema.Draft202012Validator, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """Refuse arguments that do not match the published schema; None when they do.

    The published ``inputSchema`` is the contract this server tells callers
    they are held to (docs/troubleshooting.md says a malformed value is
    "rejected without reaching the API"), so the check runs here, before any
    request is constructed: a wrong-typed body, an unknown enum value or an
    out-of-range query parameter answers with the typed ``invalid_argument``
    naming every violation, and nothing goes upstream.

    The whole argument tree is checked, nested body contents included:
    ``replaceDenylist(body=[{"wrong": "x"}])`` violates the document's own
    item schema (``required: [id]``) and is refused, not forwarded. Opacity
    (issue #273) is about types, not structure - the normalizers never rewrite
    a nested value, so a published nested constraint is exactly the constraint
    the request would satisfy; anything looser would let the server advertise
    a schema it does not enforce. Violations are streamed (see
    :func:`_schema_problems`): every part of the tree is checked while only
    the rendered few are kept.
    """
    problems, extra = _schema_problems(validator, _without_nulls(arguments))
    if not problems:
        return None
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        f"{tool_name} arguments do not match the published schema: {_describe_schema_errors(problems, extra)}",
        hint="the tool's inputSchema is the accepted shape: fix the named values, or omit optional arguments entirely",
    )
