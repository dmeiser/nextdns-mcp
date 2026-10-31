"""One MCP tool per NextDNS API operation (the ``atomic`` tool surface).

SPDX-License-Identifier: MIT

The grouped surface asks a caller to pick an area tool and then name an
``operation`` inside it; this surface removes that second choice. Every
operation in ``nextdns-openapi.yaml`` becomes a tool named by its own
``operationId`` (``addToDenylist``, ``getAnalyticsDevicesSeries``), described by
the spec's own summary and description, with the endpoint's path, query and
request-body arguments as its parameters.

Two things the grouped surface does not need and this one does:

- Requests are routed from the HTTP method and path rather than from an
  ``operation`` argument, so the call log records the bound endpoint
  (``register_endpoint``) and derives its write flag from the method.
- The spec's parameter documents are the only per-argument guidance a caller
  gets, so the schemas are published with their descriptions, enums and
  defaults intact instead of being summarised by hand.

Every call still goes through ``utils._api_request_payload``, i.e. the same
access-controlled client and the same typed error payloads as the grouped tools.

Two operations are deliberately not registered, and the reason is recorded here
because a missing tool is otherwise invisible to a caller:

- ``streamLogs`` is a Server-Sent Events stream. The MCP tool call is
  request/response, so the transport cannot carry it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.tasks import TaskConfig

from .call_log import register_endpoint
from .errors import ErrorCode, error_payload
from .utils import _api_request_payload

logger = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).with_name("nextdns-openapi.yaml")

_HTTP_METHODS = ("get", "post", "patch", "put", "delete")

# Registered but served by the grouped implementation, because the safe form of
# this call (streaming with size caps and redirect checks) lives there and
# duplicating it here would duplicate its guards too.
_DELEGATED = frozenset({"downloadLogs"})

# Not offered at all: see the module docstring.
_NOT_OFFERED = frozenset({"streamLogs"})

# Name given to the argument carrying a request body that is a JSON array
# (``PUT /denylist`` takes an array, not an object, so it has no property names
# to flatten into the tool's parameters).
ARRAY_BODY_ARG = "entries"

# Bound on a published description. The spec's longest is 545 characters; the
# cap exists so a future spec edit cannot silently make one tool dominate every
# search answer.
DESCRIPTION_MAX_CHARS = 1200

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass(frozen=True)
class ApiOperation:
    """One OpenAPI operation, reduced to what routing and publishing need."""

    operation_id: str
    method: str
    path: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    body_mode: str = "none"  # "none" | "object" | "array"
    body_props: tuple[str, ...] = ()
    required: tuple[str, ...] = ()


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    """Read the shipped OpenAPI spec."""
    with path.open(encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    return spec if isinstance(spec, dict) else {}


def _deref(node: Any, spec: dict[str, Any]) -> Any:
    """Follow a local ``$ref`` (the spec keeps parameters and bodies in components)."""
    current = node
    seen: set[str] = set()
    while isinstance(current, dict) and "$ref" in current:
        ref = str(current["$ref"])
        if not ref.startswith("#/") or ref in seen:
            # Nothing local to resolve an external ref with, and a ref that
            # comes back around is a cycle in the spec, not something to chase.
            return {}
        seen.add(ref)
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        current = target
    return current if isinstance(current, dict) else {}


def _operation_parameters(operation: dict[str, Any], spec: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Resolved parameter documents of one operation (path and query)."""
    resolved = []
    for raw in operation.get("parameters") or []:
        parameter = _deref(raw, spec)
        if parameter.get("name"):
            resolved.append(parameter)
    return tuple(resolved)


def _body_schema(operation: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The ``application/json`` request-body schema, dereferenced."""
    body = _deref(operation.get("requestBody") or {}, spec)
    content = body.get("content") or {}
    return _deref((content.get("application/json") or {}).get("schema") or {}, spec)


def _property_schema(source: dict[str, Any]) -> dict[str, Any]:
    """Publishable JSON schema for one argument, keeping what the spec documents."""
    schema: dict[str, Any] = {}
    for key in ("type", "description", "enum", "default", "minimum", "maximum", "items", "pattern"):
        if key in source:
            schema[key] = source[key]
    if not schema:
        schema["type"] = "string"
    return schema


def _parameter_schema(parameter: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Publishable schema for one path or query parameter.

    OpenAPI documents a parameter's meaning on the parameter, not on its schema
    (``{name, in, description, schema: {type, default, ...}}``), so the two have
    to be merged: publishing the schema alone would publish ``limit`` with no
    word about what it limits.
    """
    schema = dict(_deref(parameter.get("schema") or {}, spec))
    description = str(parameter.get("description") or "").strip()
    if description:
        schema["description"] = description
    return _property_schema(schema)


def _describe(operation_id: str, operation: dict[str, Any], method: str, path: str) -> str:
    """The published description: the spec's own words plus the endpoint it binds."""
    summary = str(operation.get("summary") or "").strip()
    detail = str(operation.get("description") or "").strip()
    parts = [part for part in (summary, detail) if part]
    parts.append(f"NextDNS API: {method} {path}")
    text = "\n\n".join(parts)
    return text[:DESCRIPTION_MAX_CHARS]


def parse_operations(spec: dict[str, Any]) -> list[ApiOperation]:
    """Every spec operation, in stable order, with its MCP parameter schema."""
    operations: list[ApiOperation] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):  # pragma: no cover - malformed spec
            continue
        for verb in _HTTP_METHODS:
            raw = path_item.get(verb)
            if not isinstance(raw, dict):
                continue
            operation_id = str(raw.get("operationId") or "")
            if not operation_id:  # pragma: no cover - the spec has none
                continue
            operations.append(_build_operation(operation_id, verb.upper(), path, raw, spec))
    return sorted(operations, key=lambda item: item.operation_id)


def _build_operation(
    operation_id: str, method: str, path: str, raw: dict[str, Any], spec: dict[str, Any]
) -> ApiOperation:
    """One operation's routing table and published schema."""
    parameters = _operation_parameters(raw, spec)
    path_params = tuple(p["name"] for p in parameters if p.get("in") == "path")
    query_params = tuple(p["name"] for p in parameters if p.get("in") == "query")

    properties: dict[str, Any] = {}
    required: list[str] = list(path_params)
    for parameter in parameters:
        properties[parameter["name"]] = _parameter_schema(parameter, spec)

    body = _body_schema(raw, spec)
    body_mode = "none"
    body_props: tuple[str, ...] = ()
    if body:
        if body.get("type") == "array":
            body_mode = "array"
            item = _deref(body.get("items") or {}, spec)
            properties[ARRAY_BODY_ARG] = {
                "type": "array",
                "description": (
                    "The full replacement collection, sent as the request body. Each item: "
                    + (str(item.get("description")) if item.get("description") else _item_shape(item))
                ),
                "items": item or {"type": "object"},
            }
            required.append(ARRAY_BODY_ARG)
        else:
            body_mode = "object"
            body_props = tuple((body.get("properties") or {}).keys())
            for name, schema in (body.get("properties") or {}).items():
                properties[name] = _property_schema(_deref(schema, spec))
            required.extend(str(name) for name in (body.get("required") or []))

    return ApiOperation(
        operation_id=operation_id,
        method=method,
        path=path,
        description=_describe(operation_id, raw, method, path),
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        path_params=path_params,
        query_params=query_params,
        body_mode=body_mode,
        body_props=body_props,
        required=tuple(required),
    )


def _item_shape(item: dict[str, Any]) -> str:
    """Short text form of an array item schema's properties."""
    names = (item.get("properties") or {}).keys()
    return ", ".join(names) if names else "an object"


def offerable(operations: list[ApiOperation]) -> list[ApiOperation]:
    """Operations this surface can actually serve (see the module docstring)."""
    return [operation for operation in operations if operation.operation_id not in _NOT_OFFERED]


def _coerce(name: str, value: Any, schema: dict[str, Any]) -> Any:
    """Accept the string spellings of typed query arguments.

    MCP arguments arrive as JSON, and a caller that writes ``"30"`` for an
    integer means 30; the upstream API does not.
    """
    declared = schema.get("type")
    if not isinstance(value, str) or declared not in _JSON_TYPES:
        return value
    if declared in ("integer", "number"):
        try:
            return _JSON_TYPES[declared](value)
        except ValueError:
            return value
    if declared == "boolean":
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
    return value


class AtomicApiTool(Tool):
    """A tool that is one HTTP call: arguments in, upstream answer out."""

    task_config: TaskConfig = TaskConfig(mode="forbidden")

    def __init__(self, operation: ApiOperation) -> None:
        super().__init__(
            name=operation.operation_id,
            description=operation.description,
            parameters=operation.parameters,
            tags={"nextdns", "atomic"},
        )
        self._operation = operation

    def __repr__(self) -> str:
        return f"AtomicApiTool(name={self.name!r}, method={self._operation.method}, path={self._operation.path})"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Perform the endpoint's request through the access-controlled client."""
        operation = self._operation
        args = dict(arguments or {})
        if operation.operation_id in _DELEGATED:
            return await self._run_delegated(args)

        missing = [name for name in operation.path_params if not str(args.get(name, "")).strip()]
        if missing:
            return ToolResult(structured_content=self._missing_payload(missing))

        url = operation.path
        for name in operation.path_params:
            url = url.replace("{" + name + "}", str(args.pop(name)))

        params: dict[str, Any] = {}
        for name in operation.query_params:
            if name not in args:
                continue
            schema = (operation.parameters.get("properties") or {}).get(name) or {}
            params[name] = _coerce(name, args.pop(name), schema)

        body: Any = None
        if operation.body_mode == "array":
            body = args.pop(ARRAY_BODY_ARG, [])
        elif operation.body_mode == "object":
            body = {name: args.pop(name) for name in list(args) if name in operation.body_props} or None

        payload = await _api_request_payload(operation.method, url, params=params or None, json_body=body)
        return ToolResult(structured_content=payload)

    async def _run_delegated(self, args: dict[str, Any]) -> ToolResult:
        """Serve ``downloadLogs`` with the grouped tool's guarded downloader."""
        from .tools.logs import _download_logs_to_tempfile

        profile_id = str(args.get("profile_id", ""))
        if not profile_id.strip():
            return ToolResult(structured_content=self._missing_payload(["profile_id"]))
        return ToolResult(structured_content=await _download_logs_to_tempfile(profile_id))

    def _missing_payload(self, missing: list[str]) -> dict[str, Any]:
        """A typed error naming what the endpoint needs and where it comes from."""
        hints = {
            "profile_id": "the profile id is 6 characters; listProfiles() returns the ids this API key can access",
            "entry_id": "the entry id is the `id` field of the entry in this endpoint's list response",
            "id": "the id is the identifier of the entry in this endpoint's list response",
        }
        return error_payload(
            ErrorCode.MISSING_REQUIRED_ARGUMENT,
            f"{self.name} needs {', '.join(missing)} for {self._operation.method} {self._operation.path}",
            # The published schema's own required list: what the caller was told
            # the endpoint needs, in the words it was given.
            hint=hints.get(
                missing[0],
                "required by this endpoint: "
                + ", ".join(str(name) for name in (self._operation.parameters.get("required") or [])),
            ),
        )


def atomic_tool_names() -> list[str]:
    """The names this surface would register, without registering them.

    The server needs them before the tools exist, to describe the catalog size
    in its ``initialize`` instructions.
    """
    return [operation.operation_id for operation in offerable(parse_operations(load_spec()))]


def register_atomic_tools(server: FastMCP, operations: list[ApiOperation] | None = None) -> list[str]:
    """Register one tool per API operation and return their names.

    Each registration also records the endpoint the name is bound to, so the
    server's call log can say which API call a tool call executed (the grouped
    surface derives that from an ``operation`` argument, which these tools do
    not have).
    """
    selected = offerable(parse_operations(load_spec()) if operations is None else operations)
    names: list[str] = []
    for operation in selected:
        register_endpoint(operation.operation_id, operation.method, operation.path)
        server.add_tool(AtomicApiTool(operation))
        names.append(operation.operation_id)
    logger.info("Atomic tool surface: %d tools registered from %s", len(names), SPEC_PATH.name)
    return names
