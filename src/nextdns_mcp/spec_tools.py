"""Every NextDNS API operation as a tool, generated from the shipped OpenAPI document.

SPDX-License-Identifier: MIT

The spec is the tool contract: ``nextdns-openapi.yaml`` names, describes and documents
every operation and argument with its type, enum, default and example, so this module
does not restate any of that - FastMCP's OpenAPI parser turns each operation into a tool
named and described from the spec itself. Tuning what a caller sees means editing the YAML.

This module is the composition: ``SpecApiTool`` is the only thing that knows the pieces,
so the order their refusals fire in is readable in one place - path safety and a malformed
id, the ``createProfile`` guard, delegation, the argument split, the schema check, the
series budget, the empty-write refusal, dispatch. The pieces are ``spec_routes``,
``spec_descriptions``, ``spec_arguments``, ``spec_validation`` and ``spec_series``.

Only the transport is ours: calls go through ``transport._api_request_payload``, the
access-controlled client enforcing the profile allowlists, read-only mode and the
protected-profile denylist, and answering failures with the server's typed payload rather
than an HTTP status that discards the body. FastMCP's own transport bypasses that ACL
layer and raises on non-2xx - the reason this module exists at all.

The one operation performed here is ``downloadLogs``: following its signed-URL redirect
safely lives in ``tools.logs``, so the tool hands off. ``_api_request_payload`` and
``resolve_profile_id`` resolve from these globals, which the suite patches.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import jsonschema
from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.openapi import HTTPRoute
from mcp.types import ToolAnnotations

from .constants import PROFILE_ID_HINT
from .errors import ErrorCode, error_payload
from .spec_arguments import _PATH_PLACEHOLDER, _malformed_profile_id_payload, _unsafe_path_arguments, split_arguments
from .spec_descriptions import DESCRIPTION_MAX_CHARS, _required, describe
from .spec_routes import SPEC_PATH, WITHHELD_OPERATIONS, endpoints_for_routes, load_spec, spec_routes, spec_tool_names
from .spec_series import (
    _capped_transport_kwargs,
    _ceiling_measured_on_the_wire,
    _series_budget_for_generated_call,
    _series_refusal,
)
from .spec_validation import _normalized_for_validation, _schema_validation_payload
from .tools import logs as _logs
from .transport import MAX_SERIES_RESPONSE_BYTES, _api_request_payload, is_error_payload
from .utils import resolve_profile_id

# The generated surface's public names, unchanged by the split: the server,
# the OpenAPI layer and the suite import these from here.
__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "MAX_SERIES_RESPONSE_BYTES",
    "SPEC_PATH",
    "WITHHELD_OPERATIONS",
    "SpecApiTool",
    "describe",
    "endpoints_for_routes",
    "is_error_payload",
    "load_spec",
    "register_spec_tools",
    "spec_routes",
    "spec_tool_names",
    "split_arguments",
]

logger = logging.getLogger(__name__)


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
        # readOnlyHint is derived from the HTTP method, with one override:
        # downloadLogs answers a GET but WRITES a CSV to disk, so marking it
        # read-only would tell callers a call has no side effects it has.
        read_only = method in {"GET", "HEAD"} and route.operation_id != "downloadLogs"
        # destructiveHint is derived from the HTTP method, with one override:
        # an operation the spec declares `x-mcp-destructive: true` destroys
        # state no matter its method, so a future POST-shaped purge cannot
        # publish as non-destructive (see x-mcp-divergences in the spec).
        destructive = method in {"DELETE", "PUT", "PATCH"} or bool(
            (getattr(route, "extensions", None) or {}).get("x-mcp-destructive")
        )
        super().__init__(
            name=str(route.operation_id),
            description=describe(route),
            parameters=route.flat_param_schema,
            tags={"nextdns", "spec"},
            annotations=ToolAnnotations(
                read_only_hint=read_only,
                destructive_hint=destructive,
                open_world_hint=True,
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
            # Path-safety and malformed-id refusals are errors, not results.
            return ToolResult(structured_content=refused, is_error=True)
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

        # One snapshot per invocation: the writable set and the protected
        # denylist must come from the same read of the environment, and the
        # snapshot loader already calls get_protected_profiles().
        access = _config.load_profile_access_control()
        writable = access.writable
        if writable is not None and writable and writable.issubset(access.protected):
            return ToolResult(
                structured_content=error_payload(ErrorCode.INVALID_ARGUMENT, "no writable profile available"),
                is_error=True,
            )
        return None

    async def _dispatch_rendered(self, path: str, query: dict[str, Any], body: Any, args: dict[str, Any]) -> ToolResult:
        unfilled = _PATH_PLACEHOLDER.findall(path)
        if unfilled:
            return ToolResult(structured_content=self._missing_payload(unfilled), is_error=True)
        invalid = _schema_validation_payload(self._validator, self.name, _normalized_for_validation(self._route, args))
        if invalid is not None:
            return ToolResult(structured_content=invalid, is_error=True)
        budget = _series_budget_for_generated_call(self._route, query)
        if budget is not None:
            return ToolResult(structured_content=budget, is_error=True)
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
        transport_kwargs = _capped_transport_kwargs(self._route, request)
        payload = await request(
            self._route.method,
            path,
            params=query or None,
            json_body=body,
            **transport_kwargs,
        )
        return self._wrap_call_result(payload, _ceiling_measured_on_the_wire(request, transport_kwargs))

    def _wrap_call_result(self, payload: Any, ceiling_measured_on_the_wire: bool = False) -> ToolResult:
        """Turn a transport payload into a typed ToolResult.

        ``ceiling_measured_on_the_wire`` says the transport already bounded the
        ``;series`` body byte-for-byte as it arrived, which is what lets the
        serialized backstop stay out of a successful call's way.
        """
        refused = _series_refusal(self._route, payload, ceiling_measured_on_the_wire)
        if refused is not None:
            return ToolResult(structured_content=refused, is_error=True)
        return ToolResult(structured_content=payload, is_error=is_error_payload(payload))

    async def _run_delegated(self, arguments: dict[str, Any]) -> ToolResult:
        """Serve a delegated operation with its own guarded handler.

        The profile id gets the same shape check the generated path gets: a
        malformed id must fail as ``invalid_profile_id`` here rather than being
        smuggled into a path the downloader builds from it. Unsafe path
        characters are already refused by the guard ``run`` applies before
        dispatch.
        """
        profile_id = arguments.get("profile_id")
        resolved, error = resolve_profile_id("" if profile_id is None else str(profile_id).strip(), allow_default=False)
        if error is not None and error.get("code") == ErrorCode.MISSING_PROFILE_ID:
            return ToolResult(structured_content=self._missing_payload(["profile_id"]), is_error=True)
        if error is not None:
            return ToolResult(structured_content=error, is_error=True)
        payload = await DELEGATED_HANDLERS[self.name](str(resolved))
        # The delegated handler (downloadLogs) answers errors as typed payloads;
        # they must surface as errors, while a successful download payload must
        # not. Detection is the same rule _wrap_call_result uses.
        return ToolResult(structured_content=payload, is_error=is_error_payload(payload))

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
