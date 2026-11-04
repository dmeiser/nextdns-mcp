"""Every NextDNS API operation as a tool, generated from the shipped OpenAPI document.

SPDX-License-Identifier: MIT

The spec is the tool contract. ``nextdns-openapi.yaml`` names each operation
(``operationId``), describes it, and documents every argument it takes, so this
module does not restate any of that: it hands the document to FastMCP's OpenAPI
parser, which turns each operation into a tool whose name is the ``operationId``,
whose description is the spec's own summary and description, and whose parameters
are the endpoint's path, query and request-body fields with their descriptions,
enums, defaults and examples intact. Tuning what a caller sees means editing the
YAML, which is the only place that information lives.

Only the transport is ours. Every call goes through ``transport._api_request_payload``,
which is the access-controlled client: profile read/write allowlists, read-only
mode and the protected-profile denylist are enforced there, and failures come back
as the typed error payloads the server documents (``{"error", "code", "hint", ...}``)
rather than as an HTTP status with the upstream body discarded. That is the reason
this module exists at all: FastMCP's own OpenAPI transport sends through its own
client and raises on a non-2xx status, which would silently bypass the ACL layer
and drop the body that says *why* a call failed.

The generator adds nothing to what the spec says a call means, with two exceptions,
both recorded where they are defined: a path argument is refused before it can turn
into a different endpoint (``_UNSAFE_PATH_VALUE``), and the analytics window
arguments are rewritten out of the two spellings the API reads wrongly rather than
rejects (``_QUERY_NORMALIZERS``).

Two operations are treated specially, and both reasons are recorded here because a
surprising tool is otherwise invisible to a caller:

- ``streamLogs`` is withheld. It is a Server-Sent Events stream, and an MCP tool
  call is request/response, so the transport cannot carry it.
- ``downloadLogs`` is delegated. The endpoint answers with a redirect to a signed
  URL; the safe form of following it (size cap, redirect checks, no API key on the
  follow-up request) lives in ``tools.logs`` and is reused rather than duplicated.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.openapi import HTTPRoute, parse_openapi_to_http_routes
from mcp.types import ToolAnnotations

from .constants import PROFILE_ID_HINT
from .errors import ErrorCode, error_payload
from .tools import logs as _logs
from .tools.plots import _now, _resolve_time_point, _series_budget_error
from .transport import _api_request_payload
from .utils import is_safe_profile_id, normalize_partials, normalize_time_value, resolve_profile_id, schema_required

logger = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).with_name("nextdns-openapi.yaml")

# See the module docstring.
WITHHELD_OPERATIONS = frozenset({"streamLogs"})

# The endpoint's adaptive series-interval default, priced for calls that state
# no interval. Probed live 2026-11: windows over 7 days answer at 86400s with
# no self-cap (a 4000-day range returns 4000 points), 7-day windows at 10800s.
_UNINTERVALLED_FINEST_DEFAULT = 10800
_UNINTERVALLED_FINEST_WINDOW = 7 * 86400
_UNINTERVALLED_COARSEST_DEFAULT = 86400

# Ceiling on a generated ``;series`` tool result, measured as JSON bytes. The
# request-side point budgets bound one series; the endpoint's upstream top-N
# bounds its rows; this is the whole-response backstop, generous enough that
# a budgeted answer (10 rows x 2000 points ~ 200 KB) always fits.
MAX_SERIES_RESPONSE_BYTES = 512_000


def _delegate_download_logs(profile_id: str) -> Awaitable[dict[str, Any]]:
    """Forward to the guarded downloader, resolved on its module at call time.

    The indirection is deliberate: capturing ``_download_logs_to_tempfile`` at
    import time would freeze the original function into the registry, and the
    tests that replace it on ``tools.logs`` would then be patching a name
    nobody calls.
    """
    return _logs._download_logs_to_tempfile(profile_id)


# The operations this server performs itself, and the handler that performs
# each one. Mapping operation id -> handler is the whole special case: adding
# a delegated operation means one dict entry here and a guarded implementation
# in ``tools``, and nothing in ``SpecApiTool`` changes. The name set is derived
# from the mapping so the two can never disagree.
DELEGATED_HANDLERS: dict[str, Callable[[str], Awaitable[dict[str, Any]]]] = {
    "downloadLogs": _delegate_download_logs,
}
DELEGATED_OPERATIONS = frozenset(DELEGATED_HANDLERS)

# Bound on a published description. The spec's longest is 545 characters; the cap
# exists so a future spec edit cannot silently make one tool dominate every search
# answer the server gives back.
DESCRIPTION_MAX_CHARS = 1200

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


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    """Read the shipped OpenAPI document."""
    with path.open(encoding="utf-8") as handle:
        spec: dict[str, Any] = yaml.safe_load(handle)
    return spec


_OFFERABLE_ROUTES: list[HTTPRoute] | None = None


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
    fresh parse - the cache is for the shipped one only. The cached list is
    shared; callers must treat it as read-only.
    """
    global _OFFERABLE_ROUTES
    if spec is None and _OFFERABLE_ROUTES is not None:
        return _OFFERABLE_ROUTES
    parsed = parse_openapi_to_http_routes(load_spec() if spec is None else spec)
    offerable = sorted(filter(_is_offerable, parsed), key=lambda route: str(route.operation_id))
    if spec is None:
        _OFFERABLE_ROUTES = offerable
    return offerable


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


def _truncate_description(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, preferring a sentence/paragraph boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(". "))
    if boundary < limit // 2:
        return cut.rstrip()
    step = 1 if cut[boundary] == "\n" else 2
    return cut[: boundary + step].rstrip()


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


class SpecApiTool(Tool):
    """One NextDNS API endpoint, published from the spec and executed through the ACL client.

    ``transport`` overrides the request function (the default, resolved as a
    module global at call time, is ``_api_request_payload``); it exists so a
    caller can inject a transport per tool instead of patching module state,
    while tests that do patch the module keep working.
    """

    def __init__(
        self,
        route: HTTPRoute,
        transport: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        method = str(route.method).upper()
        super().__init__(
            name=str(route.operation_id),
            description=describe(route),
            parameters=route.flat_param_schema,
            tags={"nextdns", "spec"},
            annotations=ToolAnnotations(
                read_only_hint=method in {"GET", "HEAD"},
                destructive_hint=method in {"DELETE", "PUT", "PATCH"},
            ),
        )
        self._route = route
        self._transport = transport
        # The published schema is the contract this tool accepts, so it is also
        # what the call is checked against before anything goes out; compiling
        # the validator once per route makes per-call validation cheap.
        self._validator = jsonschema.Draft202012Validator(route.flat_param_schema or {})

    def __repr__(self) -> str:
        return f"SpecApiTool(name={self.name!r}, method={self._route.method}, path={self._route.path})"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Call the endpoint and report the result as a typed payload.

        Refusal comes before dispatch in two halves: what may not be *shaped*
        into this endpoint at all (path-unsafe values, an impossible profile
        id) is refused for every operation, delegated ones included - the
        delegation used to run ahead of these checks, which let a crafted
        ``profile_id`` steer the guarded downloader's URL. What this endpoint
        still needs or rejects (an unfilled placeholder, arguments that
        violate the published schema, a series request beyond the point
        budget) is checked on the generated path, after the call is split.
        """
        args = arguments or {}
        refused = self._request_refusal(args)
        if refused is not None:
            return ToolResult(structured_content=refused)
        create_refusal = self._create_profile_guard()
        if create_refusal is not None:
            return create_refusal
        if self.name in DELEGATED_OPERATIONS:
            return await self._run_delegated(args)
        path, query, body = split_arguments(self._route, args)
        return await self._dispatch_rendered(path, query, body, args)

    def _request_refusal(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """The payload when this call's path arguments must not be sent at all."""
        unsafe = _unsafe_path_arguments(self._route, args)
        if unsafe:
            return self._unsafe_path_payload(unsafe)
        return _malformed_profile_id_payload(self._route, args)

    def _create_profile_guard(self) -> ToolResult | None:
        """Refuse createProfile when every writable profile is also protected."""
        if self.name != "createProfile":
            return None
        from . import config as _config

        writable = _config.get_writable_profiles_set()
        protected = _config.get_protected_profiles()
        if writable is not None and writable and writable.issubset(protected):
            return ToolResult(
                structured_content=error_payload(ErrorCode.INVALID_ARGUMENT, "no writable profile available"),
                is_error=True,
            )
        return None

    async def _dispatch_rendered(self, path: str, query: dict[str, Any], body: Any, args: dict[str, Any]) -> ToolResult:
        unfilled = _PATH_PLACEHOLDER.findall(path)
        if unfilled:
            return ToolResult(structured_content=self._missing_payload(unfilled))
        invalid = _schema_validation_payload(self._validator, self.name, _normalized_for_validation(self._route, args))
        if invalid is not None:
            return ToolResult(structured_content=invalid)
        budget = _series_budget_for_generated_call(self._route, query)
        if budget is not None:
            return ToolResult(structured_content=budget)
        empty = self._empty_write_refusal(self._route.method, body)
        if empty is not None:
            return empty
        return await self._call_endpoint(path, query, body)

    def _empty_write_refusal(self, method: Any, body: Any) -> ToolResult | None:
        """A PATCH/PUT with nothing to apply is an error, not a silent success."""
        if str(method).upper() not in {"PATCH", "PUT"}:
            return None
        if body is not None and body != {}:
            return None
        return ToolResult(
            structured_content=error_payload(ErrorCode.INVALID_ARGUMENT, "no applicable fields to update"),
            is_error=True,
        )

    async def _call_endpoint(self, path: str, query: dict[str, Any], body: Any) -> ToolResult:
        """Send the request this route describes and wrap its payload.

        A ``;series`` route runs under ``MAX_SERIES_RESPONSE_BYTES`` *inside
        the transport*: the response streams and the refusal fires while the
        body is still arriving, so an over-wide answer never fills transport
        memory and is never JSON-decoded. An injected transport predating the
        cap keeps its legacy four-argument signature (see
        :func:`_transport_accepts_cap`) and is called the old way; either way
        ``_series_refusal`` renders the ceiling as the typed series refusal.
        """
        request = self._transport or _api_request_payload
        payload = await request(
            self._route.method,
            path,
            params=query or None,
            json_body=body,
            **_capped_transport_kwargs(self._route, request),
        )
        return self._wrap_call_result(payload)

    def _wrap_call_result(self, payload: Any) -> ToolResult:
        """Turn a transport payload into a typed ToolResult."""
        refused = _series_refusal(self._route, payload)
        if refused is not None:
            return ToolResult(structured_content=refused, is_error=True)
        is_err = isinstance(payload, dict) and ("error" in payload or "code" in payload) and "data" not in payload
        return ToolResult(structured_content=payload, is_error=is_err)

    async def _run_delegated(self, arguments: dict[str, Any]) -> ToolResult:
        """Serve a delegated operation with its own guarded handler.

        The profile id gets the same shape check the generated path gets (and
        the check the pre-migration ``manageLogs`` tool enforced): a malformed
        id must fail as ``invalid_profile_id`` here rather than being smuggled
        into a path the downloader builds from it. Unsafe path characters are
        already refused by the guard ``run`` applies before dispatch.
        """
        profile_id = arguments.get("profile_id")
        resolved, error = resolve_profile_id("" if profile_id is None else str(profile_id).strip(), allow_default=False)
        if error is not None and error.get("code") == ErrorCode.MISSING_PROFILE_ID:
            return ToolResult(structured_content=self._missing_payload(["profile_id"]))
        if error is not None:
            return ToolResult(structured_content=error)
        return ToolResult(structured_content=await DELEGATED_HANDLERS[self.name](str(resolved)))

    def _unsafe_path_payload(self, names: list[str]) -> dict[str, Any]:
        """A typed error refusing to put an id-shaped argument anywhere but a path segment."""
        code = ErrorCode.INVALID_PROFILE_ID if "profile_id" in names else ErrorCode.INVALID_ENTRY_ID
        hint = (
            PROFILE_ID_HINT
            if "profile_id" in names
            else "the id is the `id` field of the entry in this endpoint's list response"
        )
        return error_payload(
            code,
            f"{self.name} cannot send {', '.join(names)} as a path segment: it must not contain a slash, "
            "a query or fragment separator, whitespace, '..' or '%', or a brace",
            hint=hint,
        )

    def _missing_payload(self, missing: list[str]) -> dict[str, Any]:
        """A typed error naming what the endpoint still needs and where it comes from."""
        hints = {
            "profile_id": PROFILE_ID_HINT,
            "entry_id": "the entry id is the `id` field of the entry in this endpoint's list response",
            "id": "the id is the identifier of the entry in this endpoint's list response",
        }
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT,
            f"{self.name} needs {', '.join(missing)} for {self._route.method} {self._route.path}",
            hint=hints.get(missing[0], "required by this endpoint: " + ", ".join(_required(self._route))),
        )


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


def _interval_seconds(value: Any) -> int | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _series_budget_for_generated_call(route: HTTPRoute, query: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse a generated ``;series`` call whose range, interval or row count outpoint the budget.

    Two ceilings are enforced before the request is constructed, on every
    call that states a ``from``:

    * Per series: the same 2000-point budget ``plotAnalytics`` enforces
      (issue #267 applies to both doors). A call that names no interval is
      priced at the endpoint's adaptive default rather than passed through:
      probed live 2026-11, windows over 7 days answer at 86400 with **no
      self-cap** - a 4000-day range returns 4000 points - so the old
      "default is already bounded" pass-through was wrong. The estimate is
      10800s for windows up to 7 days (probed: 7d -> 10800) and 86400s above
      it (probed: 30d, 365d, 4000d all answer at 86400), which bounds every
      no-interval call the same way a stated interval is bounded.
    * Across the whole response: rows a ``;series`` endpoint returns are
      capped upstream (a default top-N this server does not publish), so the
      row count is not fully ours to price up front; the fetched payload
      gets a byte ceiling instead (:meth:`_series_response_ceiling_error`).

    A stated ``to`` of 0 is the epoch, not an omission: ``or`` would silently
    swap in ``now`` and re-price the window.
    """
    if ";series" not in route.path or query.get("from") is None:
        return None
    return _price_series_range(query["from"], query.get("to"), _interval_seconds(query.get("interval")))


def _price_series_range(from_time: Any, to_time_raw: Any, interval: int | None) -> dict[str, Any] | None:
    """Price one series call's range, estimating the interval default when unnamed."""
    if interval is None:
        interval = _default_interval_estimate(from_time, to_time_raw)
        if interval is None:
            return None  # unresolvable range: the endpoint gives its own 400
    to_time = "now" if to_time_raw is None else to_time_raw
    return _series_budget_error(from_time, to_time, interval)


def _default_interval_estimate(from_time: Any, to_time_raw: Any) -> int | None:
    """Price the endpoint's adaptive interval default for a no-interval call.

    Probed live 2026-11: windows over 7 days answer at 86400s with no
    self-cap (a 4000-day range returns 4000 points), 7-day windows at
    10800s. Neither range resolving (garbage input) means no price here -
    the endpoint gives its own 400.
    """
    now = _now()
    to_time = "now" if to_time_raw is None else to_time_raw
    start = _resolve_time_point(from_time, now)
    end = _resolve_time_point(to_time, now)
    if start is None or end is None:
        return None
    return (
        _UNINTERVALLED_FINEST_DEFAULT
        if end - start <= _UNINTERVALLED_FINEST_WINDOW
        else _UNINTERVALLED_COARSEST_DEFAULT
    )


def _capped_transport_kwargs(route: HTTPRoute, request: Callable[..., Any]) -> dict[str, Any]:
    """The transport kwargs a call needs: the cap only for routes that want it and transports that take it."""
    if ";series" in route.path and _transport_accepts_cap(request):
        return {"max_response_bytes": MAX_SERIES_RESPONSE_BYTES}
    return {}


def _transport_accepts_cap(request: Callable[..., Any]) -> bool:
    """Whether the resolved transport accepts a ``max_response_bytes`` keyword.

    ``transport`` exists so a caller can inject a per-tool request function,
    and a callable written before the cap existed has exactly the legacy
    ``(method, url, params, json_body)`` signature. Passing the cap kwarg to
    such a callable would raise ``TypeError`` before its first line runs, so
    the capability is probed from the signature instead: a legacy transport
    keeps the old call shape, and its oversized ``;series`` answers are still
    refused by the serialized backstop in :func:`_series_refusal`. A callable
    whose signature cannot be introspected is treated as legacy for the same
    reason - the unpriced path still enforces the ceiling, the priced one
    must not guess.
    """
    try:
        parameters = inspect.signature(request).parameters
    except (TypeError, ValueError):
        return False
    named = parameters.get("max_response_bytes")
    if named is not None and named.kind in (named.KEYWORD_ONLY, named.POSITIONAL_OR_KEYWORD):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _series_refusal(route: HTTPRoute, payload: Any) -> dict[str, Any] | None:
    """The typed ceiling refusal for a ``;series`` result, from either layer.

    The transport enforces the cap while streaming and answers with the typed
    ``response_too_large`` payload; that gets the series wording with no
    measured size claimed (nothing was measured - the refusal came from the
    wire). The serialized check is the backstop for an injected transport
    that honours no cap. The ceiling is a ``;series`` policy only: aggregate
    endpoints (a wide denylist, a big profile listing) legitimately answer
    with larger documents and are not refused.
    """
    if ";series" not in route.path or not isinstance(payload, dict):
        return None
    if payload.get("code") == ErrorCode.RESPONSE_TOO_LARGE:
        return _series_ceiling_payload(str(route.operation_id), None)
    return _series_response_ceiling_error(route, payload)


def _series_ceiling_payload(operation_id: str, response_bytes: int | None) -> dict[str, Any]:
    """The typed refusal for a ``;series`` answer over the byte ceiling.

    ``response_bytes`` is the measured size when something measured it (the
    serialized backstop); None when the transport refused mid-stream and the
    only known fact is that the cap was crossed.
    """
    answered = (
        f"{response_bytes} bytes" if response_bytes is not None else f"more than {MAX_SERIES_RESPONSE_BYTES} bytes"
    )
    extra: dict[str, Any] = {"max_response_bytes": MAX_SERIES_RESPONSE_BYTES}
    if response_bytes is not None:
        extra["response_bytes"] = response_bytes
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        f"{operation_id} answered {answered}, more than the "
        f"{MAX_SERIES_RESPONSE_BYTES}-byte series response ceiling; the range/interval produced "
        "more data than this server returns in one tool result",
        hint="use a larger interval or a shorter range, or read an aggregate endpoint instead of ;series",
        **extra,
    )


def _series_response_ceiling_error(route: HTTPRoute, payload: Any) -> dict[str, Any] | None:
    """Typed refusal when a fetched ``;series`` payload is too big to hand back.

    The transport enforces ``MAX_SERIES_RESPONSE_BYTES`` while streaming;
    this is the backstop for a transport that does not honour the cap (an
    injected test double, a future custom transport): a payload whose JSON
    is over the ceiling is refused as a typed error rather than dumped into
    the caller's context. Only reached for ``;series`` routes, gated by
    :func:`_series_refusal`.
    """
    try:
        size = len(json.dumps(payload, default=str))
    except (TypeError, ValueError):  # pragma: no cover - transport payloads are JSON-shaped
        size = len(str(payload))
    if size <= MAX_SERIES_RESPONSE_BYTES:
        return None
    return _series_ceiling_payload(str(route.operation_id), size)


def _required(route: HTTPRoute) -> list[str]:
    """The arguments the published schema marks required, in the caller's terms."""
    return schema_required(route.flat_param_schema)


def spec_tool_names() -> list[str]:
    """The names this server publishes, without registering them.

    The ``initialize`` instructions describe the catalog before the tools exist.
    """
    return [str(route.operation_id) for route in spec_routes()]


def register_spec_tools(server: FastMCP, routes: list[HTTPRoute] | None = None) -> list[str]:
    """Register one tool per offerable operation and return their names.

    ``routes`` is the already-parsed list the server passes in; the tool
    surface, the call-log endpoint map and the published-name list are then
    three views of a single parse of the shipped document, not three parses of
    it.
    """
    names: list[str] = []
    for route in spec_routes() if routes is None else routes:
        operation_id = str(route.operation_id)
        server.add_tool(SpecApiTool(route))
        names.append(operation_id)
    logger.info("Spec tool surface: %d tools registered from %s", len(names), SPEC_PATH.name)
    return names
