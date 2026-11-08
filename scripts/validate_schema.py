#!/usr/bin/env python3
"""
Validate JSON responses against OpenAPI schema definitions.
Used by the E2E run as an advisory signal for breaking API changes.

A published tool is one OpenAPI operation, named by its ``operationId``, so the
response a tool returns is the response that operation's schema describes and
validation needs no mapping: the tool name is the lookup key. The one exception
is a tool the server performs itself rather than proxying (``plotAnalytics``,
``dohLookup``, and the download redirect in ``downloadLogs``); those answer with a
payload no endpoint returns, so their shape is pinned in TOOL_RESPONSE_SCHEMAS.

Because most vendored schemas declare no ``required`` fields, a match is weak
evidence rather than a conformance proof; assert_operation_coverage() is what
keeps the catalog honest about what the spec says exists.

Known limitation: a cyclic $ref whose target has no unambiguous expected type
(e.g. a mixed anyOf/oneOf union) falls back to permissive validation of that
subtree rather than guessing a type.
"""

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

# Same bootstrap the harness uses: the catalog's source of truth lives in the
# package, so it is importable whether this script is run directly
# (``python scripts/validate_schema.py``) or imported as ``scripts.validate_schema``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from nextdns_mcp.server import published_tool_names

# Operations the server does not publish, read from the server itself: the
# reason is recorded in nextdns_mcp.spec_tools (``streamLogs`` is a Server-Sent
# Events stream, and an MCP tool call is request/response, so the transport
# cannot carry it). A second copy here could only drift from the one that
# decides what is actually registered.
from nextdns_mcp.spec_tools import SPEC_PATH, WITHHELD_OPERATIONS, load_spec, spec_tool_names

# Tools the server performs itself rather than proxying: no endpoint returns
# their answer, so no operation schema describes them and they are excused from
# the naming check below. Derived from the server's own catalog — anything it
# publishes that the document's operations do not name is local by definition —
# so this cannot drift from the surface it excuses.
LOCAL_TOOLS = frozenset(name for name in published_tool_names() if name not in set(spec_tool_names()))

# Of those, the ones that answer with content instead of a JSON document. A chart is an
# interactive Prefab UI view, so there is no payload for a schema to describe; the runner's assertion for it
# is that the call came back with chart content, which is what run_container_e2e checks.
CONTENT_TOOLS = frozenset({"plotAnalytics"})

# Response shapes for tools whose answer is not the document their endpoint returns.
# dohLookup queries dns.nextdns.io directly (outside the NextDNS API spec), so its
# response shape is pinned here instead: the raw DNS JSON response under ``data`` and
# lookup context under ``_metadata``.
TOOL_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "dohLookup": {
        "type": "object",
        "required": ["data", "_metadata"],
        "properties": {
            "data": {"type": "object"},
            "_metadata": {
                "type": "object",
                "required": ["profile_id", "query_domain", "query_type", "doh_endpoint"],
                "properties": {
                    "profile_id": {"type": "string"},
                    "query_domain": {"type": "string"},
                    "query_type": {"type": "string"},
                    "doh_endpoint": {"type": "string"},
                    "status_description": {"type": "string"},
                },
            },
        },
    },
    # The download endpoint answers with a redirect to a signed CSV, which the
    # server follows and streams to disk; what the caller gets is the local
    # receipt, never the CSV itself. The spec describes the CSV, so the shape of
    # what this tool actually returns has to be stated here.
    "downloadLogs": {
        "type": "object",
        "required": ["file_path", "size", "row_count", "preview"],
        "properties": {
            "file_path": {"type": "string"},
            "content_type": {"type": ["string", "null"]},
            "size": {"type": "integer"},
            "row_count": {"type": "integer"},
            "preview": {
                "type": "object",
                "required": ["text", "line_count", "bytes", "truncated"],
                "properties": {
                    "text": {"type": "string"},
                    "line_count": {"type": "integer"},
                    "bytes": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                },
            },
        },
    },
}


def load_openapi_spec(spec_path: str) -> dict[str, Any]:
    """Load and parse the OpenAPI document.

    Delegates to the package's single loader (``spec_tools.load_spec``): the
    shipped document had a second ``yaml.safe_load`` per script, and copies of
    a loader are copies that drift.
    """
    return load_spec(Path(spec_path))


def _success_response_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    """The 200 (or 201) response schema one operation declares, JSON or CSV.

    Both ``application/json`` and ``text/csv`` content are considered: some
    operations (e.g. downloadLogs) declare only a CSV body, and their schema
    is just as much part of the API contract as a JSON one.
    """
    responses = operation.get("responses", {})
    # Some operations return 200, others 201 (created)
    for success_code in ("200", "201"):
        content = responses.get(success_code, {}).get("content", {})
        for media_type in ("application/json", "text/csv"):
            schema = content.get(media_type, {}).get("schema")
            if schema:
                return schema
    return None


# operation_id -> 2xx response schema, walked once per spec object. The old
# per-call search over every path made assert_operation_coverage() quadratic in
# the document (~70 full walks for the published reads), and every E2E response
# validation paid a walk too. One entry keyed by object identity: callers pass
# whatever dict they hold — the shipped spec, or a test fixture — and a swapped
# spec must not be served a stale index.
_response_index_memo: tuple[dict[str, Any], dict[str, dict[str, Any]]] | None = None


def _response_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every operationId's 2xx response schema in one pass (memoized per spec)."""
    global _response_index_memo
    if _response_index_memo is not None and _response_index_memo[0] is spec:
        return _response_index_memo[1]
    index: dict[str, dict[str, Any]] = {}
    for path_item in spec.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            operation_id = operation.get("operationId")
            if not operation_id or operation_id in index:
                continue
            schema = _success_response_schema(operation)
            if schema:
                index[operation_id] = schema
    _response_index_memo = (spec, index)
    return index


def get_operation_response_schema(spec: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    """
    Extract the 200 response schema for a given operationId.

    Returns the schema object or None if not found.
    """
    return _response_index(spec).get(operation_id)


def spec_operations(spec: dict[str, Any]) -> dict[str, str]:
    """Every ``operationId`` the spec declares, mapped to its HTTP method.

    The catalog is generated from this document, so this is the statement of what
    the server is supposed to publish — and the thing a stale container image gets
    wrong.
    """
    operations: dict[str, str] = {}
    for path_item in spec.get("paths", {}).values():
        for method, operation in path_item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                operation_id = operation.get("operationId")
                if operation_id:
                    operations[operation_id] = method
    return operations


def _request_body_json_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    """The operation's request-body JSON schema, if it declares one."""
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    schema = body.get("content", {}).get("application/json", {}).get("schema")
    return schema if isinstance(schema, dict) else None


def titled_non_object_bodies(spec: dict[str, Any]) -> list[str]:
    """operationIds whose request body is a non-object schema carrying a ``title``.

    FastMCP names a titled non-object request body's tool argument after the
    title, while the spec-generated executor recognizes a whole-body argument
    only under the literal name ``body`` — a titled array body would therefore
    arrive wrapped as ``{"<title>": [...]}`` and send the wrong shape upstream,
    the same silent mis-request this branch's other guards exist to prevent.
    The shipped document carries no titles today; asserting it here is what
    keeps it from gaining one that breaks the executor.
    """
    offenders: list[str] = []
    for path_item in spec.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            body_schema = _request_body_json_schema(operation)
            if body_schema is None:
                continue
            # $ref'd bodies resolve to their target; only a non-object effective
            # type mis-names the argument (an undecidable type is not "object"
            # either, and FastMCP's namer reads the title from the raw schema).
            if _extract_schema_type(spec, body_schema) == "object":
                continue
            resolved = resolve_schema(spec, body_schema)
            if isinstance(resolved, dict) and resolved.get("title") and operation.get("operationId"):
                offenders.append(str(operation["operationId"]))
    return sorted(set(offenders))


def assert_operation_coverage(spec: dict[str, Any], expected_tools: Iterable[str]) -> None:
    """Hard-fail when the served catalog drifts from the spec.

    A published tool *is* an operation, named by its ``operationId``, so this is a
    direct comparison rather than a lookup through a mapping table:

    - every published tool names an operation the spec declares, unless it is one of
      the operations the server performs itself (``LOCAL_TOOLS``), whose answer no
      endpoint returns — that answer is either pinned here or is content rather than a
      document (``CONTENT_TOOLS``);
    - every operation the spec declares is published, unless it is withheld with a
      reason (``WITHHELD_OPERATIONS``) — the case a stale image produces;
    - every published read resolves to a response schema, because a read whose
      schema cannot be found is validated as SKIPPED, which the E2E workflow counts
      as neither pass nor fail.

    Raises:
        ValueError: listing the offending tools or operation ids.
    """
    operations = spec_operations(spec)
    published = set(expected_tools)

    unknown = sorted(published - set(operations) - LOCAL_TOOLS)
    if unknown:
        raise ValueError(f"published tools that name no operation in the spec: {unknown}")

    unpublished = sorted(set(operations) - published - WITHHELD_OPERATIONS)
    if unpublished:
        raise ValueError(f"spec operations published as no tool: {unpublished}")

    unpinned = sorted(
        tool for tool in published & LOCAL_TOOLS if tool not in TOOL_RESPONSE_SCHEMAS and tool not in CONTENT_TOOLS
    )
    if unpinned:
        raise ValueError(f"local tools whose answer is neither pinned nor content: {unpinned}")

    unvalidated = sorted(
        operation_id
        for operation_id, method in operations.items()
        if method == "get" and operation_id in published and get_operation_response_schema(spec, operation_id) is None
    )
    if unvalidated:
        raise ValueError(f"published reads that resolve to no response schema: {unvalidated}")

    titled = titled_non_object_bodies(spec)
    if titled:
        raise ValueError(
            "request bodies that are titled non-object schemas — FastMCP would name the tool "
            f"argument after the title while the executor wraps whole bodies as 'body': {titled}"
        )


def validate_field_type(value: Any, expected_type: str) -> bool:
    """Validate that a value matches the expected OpenAPI type."""
    # bool is a subclass of int in Python, so isinstance(True, int) is True;
    # an exact-type check keeps a boolean from satisfying an integer schema.
    if expected_type == "integer":
        return type(value) is int

    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }

    if isinstance(expected_type, list):
        # OpenAPI 3.1 writes "a string, or nothing" as ``type: [string, null]``. A value
        # satisfies that if it satisfies any member; every member is a type name this
        # validator knows or skips, so the union needs no special case beyond the loop.
        return any(validate_field_type(value, member) for member in expected_type)

    if expected_type not in type_map:
        return True  # Unknown type, skip validation

    expected_python_type = type_map[expected_type]
    return isinstance(value, expected_python_type)


def _resolve_json_pointer(spec: dict[str, Any], pointer: str) -> Any:
    """Resolve a local JSON pointer (e.g. '#/components/schemas/Foo') in the spec."""
    if not pointer.startswith("#/"):
        return None
    parts = pointer[2:].split("/")
    target: Any = spec
    for part in parts:
        if isinstance(target, dict):
            target = target.get(part)
        else:
            return None
    return target


def _extract_schema_type(
    spec: dict[str, Any] | None,
    schema: Any,
    _visited: set[str] | None = None,
) -> str | None:
    """Extract the expected OpenAPI type string from a schema or ref."""
    if not isinstance(schema, dict):
        return None

    if _visited is None:
        _visited = set()

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in _visited:
            return None
        target = _resolve_json_pointer(spec, ref) if spec else None
        if target is None:
            return None
        return _extract_schema_type(spec, target, _visited | {ref})

    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type

    if "properties" in schema:
        return "object"

    if "items" in schema:
        return "array"

    return None


def resolve_schema(spec: dict[str, Any] | None, schema: Any, _seen: set[str] | None = None) -> Any:
    """Recursively resolve ``$ref`` pointers and nested schema references.

    Returns a schema dict with all local references expanded. Cyclic references
    are broken by returning a schema with the expected type for the repeated
    reference (or an empty dict if the type cannot be determined), preventing
    infinite recursion while still enforcing type validation.
    """
    if _seen is None:
        _seen = set()
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        ref = schema["$ref"]
        target = _resolve_json_pointer(spec, ref) if spec else None
        if target is None:
            return {}
        if ref in _seen:
            # Cycle guard: avoid infinite recursion on self-referential schemas.
            # Validate the unresolved ref's expected type instead of returning
            # an empty schema that passes everything.
            expected_type = _extract_schema_type(spec, target, _seen | {ref})
            if expected_type:
                return {"type": expected_type}
            return {}
        return resolve_schema(spec, target, _seen | {ref})
    resolved: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            resolved[key] = {k: resolve_schema(spec, v, _seen) for k, v in value.items()}
        elif key == "items":
            resolved[key] = resolve_schema(spec, value, _seen)
        elif key in ("allOf", "anyOf", "oneOf") and isinstance(value, list):
            resolved[key] = [resolve_schema(spec, item, _seen) for item in value]
        else:
            resolved[key] = value
    return resolved


def validate_schema(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """
    Recursively validate data against an OpenAPI schema.

    ``$ref`` pointers must already be resolved with ``resolve_schema``.
    Returns a list of validation error messages.
    """
    errors = []

    if schema is None:
        return errors

    if not isinstance(schema, dict):
        return errors

    # Handle composed schemas
    if "allOf" in schema:
        for i, sub_schema in enumerate(schema["allOf"]):
            sub_errors = validate_schema(data, sub_schema, f"{path}.allOf[{i}]")
            errors.extend(sub_errors)
        if errors:
            return errors

    if "anyOf" in schema:
        for i, sub_schema in enumerate(schema["anyOf"]):
            sub_errors = validate_schema(data, sub_schema, f"{path}.anyOf[{i}]")
            if not sub_errors:
                return []
        errors.append(f"{path}: expected anyOf schema to match")
        return errors

    if "oneOf" in schema:
        matches = 0
        for sub_schema in schema["oneOf"]:
            sub_errors = validate_schema(data, sub_schema, path)
            if not sub_errors:
                matches += 1
        if matches != 1:
            errors.append(f"{path}: expected exactly one oneOf schema to match, got {matches}")
        return errors

    schema_type = schema.get("type")

    # Handle object types
    if schema_type == "object":
        if not isinstance(data, dict):
            errors.append(f"{path}: expected object, got {type(data).__name__}")
            return errors

        # Check required properties
        required = schema.get("required", [])
        for req_field in required:
            if req_field not in data:
                errors.append(f"{path}: missing required field '{req_field}'")

        # Validate properties
        properties = schema.get("properties", {})
        for field_name, field_schema in properties.items():
            if field_name in data:
                field_path = f"{path}.{field_name}"
                field_errors = validate_schema(data[field_name], field_schema, field_path)
                errors.extend(field_errors)

    # Handle array types
    elif schema_type == "array":
        if not isinstance(data, list):
            errors.append(f"{path}: expected array, got {type(data).__name__}")
            return errors

        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                item_path = f"{path}[{i}]"
                item_errors = validate_schema(item, items_schema, item_path)
                errors.extend(item_errors)

    # Handle primitive types
    elif schema_type:
        if not validate_field_type(data, schema_type):
            errors.append(f"{path}: expected {schema_type}, got {type(data).__name__}")

    return errors


def validate_tool_response(
    tool_name: str,
    response_data: Any,
    spec: dict[str, Any],
) -> tuple[str, list[str]]:
    """Validate a tool response against the schema of the operation it runs.

    Returns:
        tuple of (status, error_messages) where status is:
        - "SKIPPED" if synthetic response or no schemas
        - "VALID" if matches at least one candidate schema
        - "INVALID" if does not match any candidate schema
    """
    # A 204-style success has no upstream body to validate: the transport says the
    # endpoint answered with nothing, and there is nothing to compare a schema to.
    if response_data == {"success": True}:
        return "SKIPPED", []

    # A proxied tool answers with its endpoint's body, so the operation is the tool
    # name. A tool with a pinned shape answers with something the endpoint does not
    # return — a DoH response, or the receipt for a file the endpoint streams elsewhere
    # — so only the pinned schema applies; checking it against the endpoint's document
    # would compare a receipt to a CSV.
    schemas: list[tuple[str, dict[str, Any]]] = []
    literal_schema = TOOL_RESPONSE_SCHEMAS.get(tool_name)
    if literal_schema is not None:
        schemas.append((tool_name, literal_schema))
    elif tool_name not in LOCAL_TOOLS:
        operation_schema = get_operation_response_schema(spec, tool_name)
        if operation_schema is not None:
            schemas.append((tool_name, operation_schema))

    if not schemas:
        return "SKIPPED", [f"No schemas found for tool '{tool_name}'"]

    # One tool, one operation, one schema; the loop is here so an answer with more than
    # one candidate is reported by which one it failed.
    all_errors: list[str] = []
    for op_id, schema in schemas:
        resolved_schema = resolve_schema(spec, schema)
        errors = validate_schema(response_data, resolved_schema)
        if not errors:
            return "VALID", []
        all_errors.append(f"{op_id}:")
        for error in errors:
            all_errors.append(f"  - {error}")

    return "INVALID", all_errors


def main():
    """Main entry point for schema validation."""
    if len(sys.argv) < 3:
        print("Usage: validate_schema.py <tool_name> <json_response>", file=sys.stderr)
        print('Example: validate_schema.py listProfiles \'{"data":[{"id":"abc123"}]}\'', file=sys.stderr)
        sys.exit(1)

    tool_name = sys.argv[1]
    json_response = sys.argv[2]

    # Find OpenAPI spec (the same path the server reads; imported from the package)
    spec_path = SPEC_PATH

    if not spec_path.exists():
        print(f"ERROR: OpenAPI spec not found at {spec_path}", file=sys.stderr)
        sys.exit(1)

    # Load spec and response
    try:
        spec = load_openapi_spec(str(spec_path))
        response_data = json.loads(json_response)
    except yaml.YAMLError as e:
        print(f"ERROR: Failed to parse OpenAPI spec: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse JSON response: {e}", file=sys.stderr)
        sys.exit(1)

    status, errors = validate_tool_response(tool_name, response_data, spec)
    if status == "SKIPPED":
        if errors:
            print(f"WARNING: {errors[0]}", file=sys.stderr)
        print("SKIPPED", file=sys.stdout)
        sys.exit(0)
    elif status == "VALID":
        print("VALID", file=sys.stdout)
        sys.exit(0)
    else:
        print(f"SCHEMA_ERRORS: no matching schema for {tool_name}", file=sys.stderr)
        for line in errors:
            print(line, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
