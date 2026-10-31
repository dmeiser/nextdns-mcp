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

Only the transport is ours. Every call goes through ``utils._api_request_payload``,
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

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.openapi import HTTPRoute, parse_openapi_to_http_routes

from .call_log import register_endpoint
from .errors import ErrorCode, error_payload
from .utils import _api_request_payload, normalize_partials, normalize_time_value

logger = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).with_name("nextdns-openapi.yaml")

# See the module docstring.
WITHHELD_OPERATIONS = frozenset({"streamLogs"})
DELEGATED_OPERATIONS = frozenset({"downloadLogs"})

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
_UNFILLED_PATH_ARG = re.compile(r"\{(\w+)\}")

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
# be assumed to reject it. A domain is a legal entry id, so a lone dot is not refused
# — only ``..``, which no domain contains.
_UNSAFE_PATH_VALUE = re.compile(r"[\s/?#\\]|\.\.|[^\x21-\x7e]")


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    """Read the shipped OpenAPI document."""
    with path.open(encoding="utf-8") as handle:
        spec: dict[str, Any] = yaml.safe_load(handle)
    return spec


def spec_routes(spec: dict[str, Any] | None = None) -> list[HTTPRoute]:
    """Every offerable operation, in stable order.

    Ordered by ``operationId`` rather than by the spec's path order so the tool
    list a client sees does not shift when an unrelated path is added to the spec.
    """
    parsed = parse_openapi_to_http_routes(load_spec() if spec is None else spec)
    offerable = [route for route in parsed if route.operation_id and route.operation_id not in WITHHELD_OPERATIONS]
    return sorted(offerable, key=lambda route: str(route.operation_id))


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
    return "\n\n".join(parts)[:DESCRIPTION_MAX_CHARS]


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
    path = route.path
    query: dict[str, Any] = {}
    body_properties: dict[str, Any] = {}
    whole_body: Any = None

    for name, value in arguments.items():
        openapi_name, location = _mapped_location(parameter_map, name)
        if location == "path":
            # A blank path value is left as the placeholder rather than substituted:
            # an empty profile id in the middle of a URL answers with a 404 that
            # blames the wrong thing, and the caller is further from the mistake.
            if str(value).strip():
                path = path.replace("{" + openapi_name + "}", str(value))
        elif location == "query":
            query[openapi_name] = _query_value(openapi_name, value)
        elif location == "body" and openapi_name == WHOLE_BODY_ARG:
            whole_body = value
        elif location == "body":
            body_properties[openapi_name] = value

    return path, query, _request_body(whole_body, body_properties)


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
    """One NextDNS API endpoint, published from the spec and executed through the ACL client."""

    def __init__(self, route: HTTPRoute) -> None:
        super().__init__(
            name=str(route.operation_id),
            description=describe(route),
            parameters=route.flat_param_schema,
            tags={"nextdns", "spec"},
        )
        self._route = route

    def __repr__(self) -> str:
        return f"SpecApiTool(name={self.name!r}, method={self._route.method}, path={self._route.path})"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Call the endpoint and report the result as a typed payload."""
        if self.name in DELEGATED_OPERATIONS:
            return await self._run_delegated(arguments or {})

        unsafe = _unsafe_path_arguments(self._route, arguments or {})
        if unsafe:
            return ToolResult(structured_content=self._unsafe_path_payload(unsafe))

        path, query, body = split_arguments(self._route, arguments or {})
        unfilled = _UNFILLED_PATH_ARG.findall(path)
        if unfilled:
            return ToolResult(structured_content=self._missing_payload(unfilled))

        payload = await _api_request_payload(self._route.method, path, params=query or None, json_body=body)
        return ToolResult(structured_content=payload)

    async def _run_delegated(self, arguments: dict[str, Any]) -> ToolResult:
        """Serve ``downloadLogs`` with the guarded downloader instead of the raw redirect."""
        from .tools.logs import _download_logs_to_tempfile

        profile_id = str(arguments.get("profile_id", "")).strip()
        if not profile_id:
            return ToolResult(structured_content=self._missing_payload(["profile_id"]))
        return ToolResult(structured_content=await _download_logs_to_tempfile(profile_id))

    def _unsafe_path_payload(self, names: list[str]) -> dict[str, Any]:
        """A typed error refusing to put an id-shaped argument anywhere but a path segment."""
        code = ErrorCode.INVALID_PROFILE_ID if "profile_id" in names else ErrorCode.INVALID_ENTRY_ID
        hint = (
            "the profile id is 6 or 10 characters; listProfiles() returns the ids this API key can access"
            if "profile_id" in names
            else "the id is the `id` field of the entry in this endpoint's list response"
        )
        return error_payload(
            code,
            f"{self.name} cannot send {', '.join(names)} as a path segment: it must not contain a slash, "
            "a query or fragment separator, whitespace, or '..'",
            hint=hint,
        )

    def _missing_payload(self, missing: list[str]) -> dict[str, Any]:
        """A typed error naming what the endpoint still needs and where it comes from."""
        hints = {
            "profile_id": "the profile id is 6 characters; listProfiles() returns the ids this API key can access",
            "entry_id": "the entry id is the `id` field of the entry in this endpoint's list response",
            "id": "the id is the identifier of the entry in this endpoint's list response",
        }
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT,
            f"{self.name} needs {', '.join(missing)} for {self._route.method} {self._route.path}",
            hint=hints.get(missing[0], "required by this endpoint: " + ", ".join(_required(self._route))),
        )


def _unsafe_path_arguments(route: HTTPRoute, arguments: dict[str, Any]) -> list[str]:
    """Path arguments whose value could address something other than this tool's endpoint.

    A blank value is not listed here: it is left as the placeholder by
    :func:`split_arguments` and reported by :meth:`SpecApiTool.run` as a missing
    argument, which is the more useful thing to say about an empty ``profile_id``.
    """
    parameter_map = route.parameter_map or {}
    rejected: list[str] = []
    for name, value in arguments.items():
        mapped = parameter_map.get(name) or {}
        if mapped.get("location") != "path":
            continue
        text = str(value)
        if _UNSAFE_PATH_VALUE.search(text):
            rejected.append(str(mapped.get("openapi_name") or name))
    return rejected


def _required(route: HTTPRoute) -> list[str]:
    """The arguments the published schema marks required, in the caller's terms."""
    return [str(name) for name in ((route.flat_param_schema or {}).get("required") or [])]


def spec_tool_names() -> list[str]:
    """The names this server publishes, without registering them.

    The ``initialize`` instructions describe the catalog before the tools exist.
    """
    return [str(route.operation_id) for route in spec_routes()]


def register_spec_tools(server: FastMCP) -> list[str]:
    """Register one tool per offerable operation and return their names.

    Each registration records the endpoint the name is bound to, so the server's
    call log can say which API call a tool call executed. That record is what an
    end-to-end run checks a step against: the tool name alone says what the caller
    asked for, not what the server sent upstream.
    """
    names: list[str] = []
    for route in spec_routes():
        operation_id = str(route.operation_id)
        register_endpoint(operation_id, route.method, route.path)
        server.add_tool(SpecApiTool(route))
        names.append(operation_id)
    logger.info("Spec tool surface: %d tools registered from %s", len(names), SPEC_PATH.name)
    return names
