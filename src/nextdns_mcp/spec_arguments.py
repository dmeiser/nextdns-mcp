"""Flat tool arguments -> ``(rendered path, query parameters, request body)``.

This module holds the one responsibility of shaping what a caller sent into
what the endpoint receives: path-template substitution, the query normalizers
(``_QUERY_NORMALIZERS`` - the two analytics window spellings the API reads
wrongly are rewritten rather than rejected) and the path-segment guard
(``_UNSAFE_PATH_VALUE``) that refuses a value before it can address a different
endpoint. Those are the generator's only two departures from the spec, and the
document records them in its ``x-mcp-divergences``. Nothing here re-reads the
spec: the parser's ``parameter_map`` says where each flattened argument came from.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fastmcp.utilities.openapi import HTTPRoute

from .constants import PROFILE_ID_HINT
from .errors import ErrorCode, error_payload
from .utils import is_safe_profile_id, normalize_partials, normalize_time_value

# The argument FastMCP's parser names for a request body that is a bare JSON array
# (``PUT /denylist`` takes an array, not an object, so there are no property names
# to flatten). The spec documents this argument's own description and example, so
# it reaches the caller described rather than as an opaque "body".
WHOLE_BODY_ARG = "body"


# A path parameter the caller did not fill in leaves its placeholder behind.
# The one pattern for a placeholder: substitution and the leftover check are
# the same question ("what occupies {name} in this path now").
_PATH_PLACEHOLDER = re.compile(r"\{(\w+)\}")


# The analytics/log window arguments are rewritten on the way out rather than sent
# exactly as given, which is the one place this generator is not a plain proxy.
#
# It is not a guardrail: it exists because two spellings callers reach for fail
# *silently* upstream. `from=-86400` is a bare negative number, which the API reads
# as an absolute Unix timestamp in 1969 and answers 200 with no rows — across three
# live runs a model retried that call about 40 times, varying the interval, with no
# error to act on. `from=now-1d` (the Grafana spelling) answers "must be number",
# and `partials=false` answers a 400 enum that names no allowed values. Both have an
# accepted spelling the caller meant (`-1d`, `none`), and the rewrite is exact, not a
# guess: see utils.normalize_time_value and normalize_partials for what is and is not
# rewritten. Timestamps, ISO 8601, `now` and `-1d` pass through untouched.
_QUERY_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "from": normalize_time_value,
    "to": normalize_time_value,
    "partials": normalize_partials,
}


# Characters that would turn one path argument into a different endpoint. An id is
# a single path segment; anything here could escape it — ``/`` to another resource,
# ``?``/``#`` to change the query or fragment, ``\`` because some URL parsers read it
# as a separator, whitespace, ``..`` to walk up, or a non-printable character. The
# value is substituted into the request path, and the access-control layer authorizes
# the request by reading the profile id back out of that path, so an unvetted value
# can move the call somewhere the caller was not authorized for. Nothing upstream can
# be assumed to reject it. ``..`` walks up and a lone dot (or a bare run of
# dots) is refused outright — no domain is either.
#
# ``%`` is refused outright, not just when followed by a recognizable escape: it is
# the one character whose *meaning* depends on what decodes the URL. ``%2e%2e`` is
# ``..`` to anything that percent-decodes, and a double-encoded ``%252e%252e`` is
# invisible to a single decode pass — rejecting the character kills both this and
# any future encoding variant in one stroke, and no legal NextDNS id contains it.
# ``{``/``}`` are refused for the same reason in the other direction: they are the
# shape of a path placeholder, and an id carrying one must not be able to name a
# different parameter.
_UNSAFE_PATH_VALUE = re.compile(r"[\s/?#\\{}%]|\.\.|^\.{1,}$|[^\x21-\x7e]")


def split_arguments(route: HTTPRoute, arguments: dict[str, Any]) -> tuple[str, dict[str, Any], Any]:
    """Flat tool arguments into ``(rendered path, query parameters, request body)``.

    The parser's ``parameter_map`` says where each flattened argument came from, so
    nothing here has to re-read the spec to know whether ``id`` is a path segment or
    a body property. An argument the map does not know about is dropped: the
    server's own middleware removes anything outside the published schema before a
    call reaches a tool, and forwarding a stray name into a request body could send
    the API something the spec never described.

    Time-shaped query arguments are rewritten into the form the API reads (see
    ``_QUERY_NORMALIZERS``), because the two shapes callers reach for fail silently
    rather than as an error the caller could correct.
    """
    parameter_map = route.parameter_map or {}
    located = [(name, *_mapped_location(parameter_map, name)) for name in arguments]
    path = _rendered_path(route.path, located, arguments)
    query = _query_arguments(located, arguments)
    body = _split_body_arguments(located, arguments)
    return path, query, body


def _rendered_path(template: str, located: list[tuple[str, str, str]], arguments: dict[str, Any]) -> str:
    """Substitute this route's path arguments into its path template, in one pass.

    A blank path value is left as the placeholder rather than substituted:
    an empty profile id in the middle of a URL answers with a 404 that
    blames the wrong thing, and the caller is further from the mistake.

    The substitution is a single regex pass over the *template*, not a chain of
    ``str.replace`` calls: with sequential replacement, a value containing
    another parameter's placeholder (``profile_id="{entry_id}"``) gets
    re-substituted on a later pass, so the path that leaves carries a second
    argument where the first was authorized - the authorized profile id and the
    path actually sent drift apart. A value inserted by ``sub`` is never
    rescanned, so a placeholder can only ever come from the template itself.
    """
    values = {openapi_name: arguments[name] for name, openapi_name, location in located if location == "path"}

    def substitute(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        return match.group(0) if not _path_value_present(value) else str(value)

    return _PATH_PLACEHOLDER.sub(substitute, template)


def _path_value_present(value: Any) -> bool:
    """True when a path argument carries something to substitute.

    ``None`` and blank strings count as absent, not as the text ``"None"``:
    a client sending an explicit null for a path parameter means "I did not
    fill this in", and rendering that as the literal ``None`` would send a
    request for a profile literally named None.
    """
    return value is not None and str(value).strip() != ""


def _query_arguments(located: list[tuple[str, str, str]], arguments: dict[str, Any]) -> dict[str, Any]:
    """This route's query arguments, each rewritten into the form the API reads.

    An argument sent as an explicit null is dropped rather than sent: httpx
    would serialize it as an empty value (``?to=&cursor=``), which upstream
    answers with a validation error that names the empty string, not the
    client's stray null. A null query argument means "not given" - the same
    thing the argument's absence means.
    """
    query: dict[str, Any] = {}
    for name, openapi_name, location in located:
        if location == "query" and arguments[name] is not None:
            query[openapi_name] = _query_value(openapi_name, arguments[name])
    return query


def _split_body_arguments(located: list[tuple[str, str, str]], arguments: dict[str, Any]) -> Any:
    """The request body: a whole-body argument wins over individual body properties."""
    whole_body: Any = None
    body_properties: dict[str, Any] = {}
    for name, openapi_name, location in located:
        if location != "body":
            continue
        if openapi_name == WHOLE_BODY_ARG:
            whole_body = arguments[name]
        else:
            if arguments[name] is not None:
                body_properties[openapi_name] = arguments[name]
    return _request_body(whole_body, body_properties)


def _mapped_location(parameter_map: dict[str, Any], name: str) -> tuple[str, Any]:
    """The (OpenAPI name, location) a flattened argument was derived from.

    The parser flattened body properties into the tool's top-level schema, so an
    argument's own name is not necessarily the API's name for it - and an
    argument the map does not know about has no location, which drops it.
    """
    mapped = parameter_map.get(name) or {}
    return str(mapped.get("openapi_name") or name), mapped.get("location")


def _query_value(openapi_name: str, value: Any) -> Any:
    """Rewrite one query argument into the form the API reads, if it needs one."""
    normalize = _QUERY_NORMALIZERS.get(openapi_name)
    return normalize(value) if normalize else value


def _request_body(whole_body: Any, body_properties: dict[str, Any]) -> Any:
    """The request body: a whole-body argument wins, else the collected properties.

    An endpoint whose body is one declared argument (``body`` in the flattened
    schema) takes that value as the document; an endpoint whose body is a set of
    properties takes the object built from them. No body arguments means no
    body, not an empty object - some endpoints read an empty document as a
    request to clear what they hold.
    """
    if whole_body is not None:
        return whole_body
    return body_properties or None


def _mapped_path_name(name: str, mapped: dict[str, Any] | None) -> tuple[str, str] | None:
    """(name, openapi name) when this argument's mapped location is a path segment."""
    if not mapped or mapped.get("location") != "path":
        return None
    return name, str(mapped.get("openapi_name") or name)


def _path_argument_names(route: HTTPRoute, arguments: dict[str, Any]) -> list[tuple[str, str]]:
    """(name, openapi name) of the sent arguments this route places in the path."""
    parameter_map = route.parameter_map or {}
    pairs = (_mapped_path_name(name, parameter_map.get(name)) for name in arguments)
    return list(filter(None, pairs))


def _unsafe_path_arguments(route: HTTPRoute, arguments: dict[str, Any]) -> list[str]:
    """Path arguments whose value could address something other than this tool's endpoint.

    A blank or null value is not listed here: it is not unsafe, it is missing.
    It is left as the placeholder by :func:`split_arguments` and reported by
    :meth:`SpecApiTool.run` as a missing argument, which is the more useful
    thing to say about an empty ``profile_id`` - and it keeps ``None`` from
    being stringified into the text ``"None"``, which contains none of the
    refused characters and would sail through this guard into the path.
    """
    return [
        openapi_name
        for name, openapi_name in _path_argument_names(route, arguments)
        if _path_value_present(arguments[name]) and _UNSAFE_PATH_VALUE.search(str(arguments[name]))
    ]


def _malformed_profile_id_payload(route: HTTPRoute, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """A typed refusal when a sent ``profile_id`` cannot be a NextDNS profile id.

    A printable-but-impossible id (13 characters, uppercase, punctuation) used
    to be substituted into the path and refused by the access-control layer as
    an unclassifiable URL - a 403 about the *request*, when the honest answer
    is that the id is not shaped like one. Catching it here returns the typed
    ``invalid_profile_id`` and the shape hint before a request is built.
    """
    for name, openapi_name in _path_argument_names(route, arguments):
        if openapi_name != "profile_id":
            continue
        value = arguments[name]
        if _path_value_present(value) and not is_safe_profile_id(value):
            return error_payload(
                ErrorCode.INVALID_PROFILE_ID,
                f"{route.operation_id} was given a profile id that is not shaped like one: {value!r}",
                hint=PROFILE_ID_HINT,
            )
    return None
