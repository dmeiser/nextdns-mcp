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
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.openapi import HTTPRoute, parse_openapi_to_http_routes

from .call_log import register_endpoint
from .errors import ErrorCode, error_payload
from .utils import _api_request_payload

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
    """
    parameter_map = route.parameter_map or {}
    path = route.path
    query: dict[str, Any] = {}
    body_properties: dict[str, Any] = {}
    whole_body: Any = None

    for name, value in arguments.items():
        mapped = parameter_map.get(name) or {}
        openapi_name = str(mapped.get("openapi_name") or name)
        location = mapped.get("location")
        if location == "path":
            # A blank path value is left as the placeholder rather than substituted:
            # an empty profile id in the middle of a URL answers with a 404 that
            # blames the wrong thing, and the caller is further from the mistake.
            if str(value).strip():
                path = path.replace("{" + openapi_name + "}", str(value))
        elif location == "query":
            query[openapi_name] = value
        elif location == "body" and openapi_name == WHOLE_BODY_ARG:
            whole_body = value
        elif location == "body":
            body_properties[openapi_name] = value

    return path, query, whole_body if whole_body is not None else (body_properties or None)


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
