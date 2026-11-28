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

Validation itself is delegated to ``jsonschema.Draft202012Validator`` — the
same validator class the server's spec tools compile per route — with the
document registered as the resolution target for local ``$ref`` pointers, so
the check enforces the whole schema (``required``, ``enum``, ``pattern``,
``additionalProperties``, array bounds, composed schemas) instead of a subset.
"""

import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

# Same bootstrap the harness uses: the catalog's source of truth lives in the
# package, so it is importable whether this script is run directly
# (``python scripts/validate_schema.py``) or imported as ``scripts.validate_schema``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from nextdns_mcp.server import (
    generative_ui_content_tool_names,
    generative_ui_data_tool_names,
    published_tool_names,
)

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
def local_tool_names() -> frozenset[str]:
    """Return the set of tools implemented locally on the server."""
    return frozenset(name for name in published_tool_names() if name not in set(spec_tool_names()))


# Of those, the ones that answer with content instead of a JSON document. A chart is an
# interactive Prefab UI view, so there is no payload for a schema to describe; the runner's assertion for it
# is that the call came back with chart content, which is what run_container_e2e checks.
# The generative-UI names are not restated here: they arrive through the server's
# own content/data partition — ``search_prefab_components`` answers with data, not content —
# so a rename there moves this set with it, and the split lives only on the server.
CONTENT_TOOLS = frozenset({"plotAnalytics", "overviewDashboard", *generative_ui_content_tool_names()})

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
    # The generative-UI data tools answer with data no endpoint returns, so their
    # shape is pinned here; the names come from the server's content/data partition,
    # so a rename moves the pin with it and cannot silently orphan it. FastMCP wraps a
    # scalar tool return as {"result": <value>} (x-fastmcp-wrap-result), so the pin
    # describes that wrapped object, not the bare markdown the tool produces.
    **{
        name: {"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}}
        for name in generative_ui_data_tool_names()
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


_SPEC_OPERATION_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _iter_path_items(spec: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The mapping path items of a document; anything else is not a path."""
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return
    for path_item in paths.values():
        if isinstance(path_item, dict):
            yield path_item


def _iter_spec_operations(spec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(method, operation)`` for every HTTP operation in a document.

    The schema index, the published-tool catalog and the container runner's
    write-coverage gate used to each walk ``paths`` with their own copy of the
    method filter. Entries that are not HTTP operations — including a
    non-mapping path item, which the container runner already tolerated — are
    not operations and contribute nothing.
    """
    for path_item in _iter_path_items(spec):
        for method, operation in path_item.items():
            if method in _SPEC_OPERATION_METHODS and isinstance(operation, dict):
                yield method, operation


# operation_id -> 2xx response schema, walked once per spec object. The old
# per-call search over every path made assert_operation_coverage() quadratic in
# the document (~70 full walks for the published reads), and every E2E response
# validation paid a walk too. One entry keyed by object identity: callers pass
# whatever dict they hold — the shipped spec, or a test fixture — and a swapped
# spec must not be served a stale index.
_response_index_memo: tuple[dict[str, Any], dict[str, dict[str, Any]]] | None = None


def _index_operation(index: dict[str, dict[str, Any]], operation: dict[str, Any]) -> None:
    operation_id = operation.get("operationId")
    if not operation_id or operation_id in index:
        return
    schema = _success_response_schema(operation)
    if schema:
        index[operation_id] = schema


def _response_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every operationId's 2xx response schema in one pass (memoized per spec)."""
    global _response_index_memo
    if _response_index_memo is not None and _response_index_memo[0] is spec:
        return _response_index_memo[1]
    index: dict[str, dict[str, Any]] = {}
    for _method, operation in _iter_spec_operations(spec):
        _index_operation(index, operation)
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
    for method, operation in _iter_spec_operations(spec):
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


def _titled_non_object_body(spec: dict[str, Any], operation: dict[str, Any]) -> str:
    body_schema = _request_body_json_schema(operation)
    if body_schema is None:
        return ""
    # $ref'd bodies resolve to their target; only a non-object effective
    # type mis-names the argument (an undecidable type is not "object"
    # either, and FastMCP's namer reads the title from the raw schema).
    resolved = _resolve_body_schema(spec, body_schema)
    if _declared_schema_type(resolved) == "object":
        return ""
    if resolved.get("title") and operation.get("operationId"):
        return str(operation["operationId"])
    return ""


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
    for _method, operation in _iter_spec_operations(spec):
        offender = _titled_non_object_body(spec, operation)
        if offender:
            offenders.append(offender)
    return sorted(set(offenders))


def _check_published_names(operations: dict[str, str], published: set[str], local_tools: set[str]) -> None:
    unknown = sorted(published - set(operations) - local_tools)
    if unknown:
        raise ValueError(f"published tools that name no operation in the spec: {unknown}")


def _check_published_operations(operations: dict[str, str], published: set[str]) -> None:
    unpublished = sorted(set(operations) - published - WITHHELD_OPERATIONS)
    if unpublished:
        raise ValueError(f"spec operations published as no tool: {unpublished}")


def _check_pinned_local_tools(published: set[str], local_tools: set[str]) -> None:
    unpinned = sorted(
        tool for tool in published & local_tools if tool not in TOOL_RESPONSE_SCHEMAS and tool not in CONTENT_TOOLS
    )
    if unpinned:
        raise ValueError(f"local tools whose answer is neither pinned nor content: {unpinned}")


def _unvalidated_read(spec: dict[str, Any], operation_id: str, method: str, published: set[str]) -> bool:
    return method == "get" and operation_id in published and get_operation_response_schema(spec, operation_id) is None


def _check_validated_reads(spec: dict[str, Any], operations: dict[str, str], published: set[str]) -> None:
    unvalidated = sorted(
        operation_id
        for operation_id, method in operations.items()
        if _unvalidated_read(spec, operation_id, method, published)
    )
    if unvalidated:
        raise ValueError(f"published reads that resolve to no response schema: {unvalidated}")


def _check_untitled_bodies(spec: dict[str, Any]) -> None:
    titled = titled_non_object_bodies(spec)
    if titled:
        raise ValueError(
            "request bodies that are titled non-object schemas — FastMCP would name the tool "
            f"argument after the title while the executor wraps whole bodies as 'body': {titled}"
        )


def assert_operation_coverage(spec: dict[str, Any], expected_tools: Iterable[str]) -> None:
    """Hard-fail when the served catalog drifts from the spec.

    A published tool *is* an operation, named by its ``operationId``, so this is a
    direct comparison rather than a lookup through a mapping table:

    - every published tool names an operation the spec declares, unless it is one of
      the operations the server performs itself (``local_tool_names()``), whose answer no
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
    local_tools = local_tool_names()

    _check_published_names(operations, published, local_tools)
    _check_published_operations(operations, published)
    _check_pinned_local_tools(published, local_tools)
    _check_validated_reads(spec, operations, published)
    _check_untitled_bodies(spec)


# Canonical address of the OpenAPI document inside the validator registry. A
# response schema is a subschema of the document: its ``$ref`` pointers are
# document-local, so the document is registered as a resource and each local
# pointer is restated to address it (``#/components/schemas/X`` ->
# ``nextdns-openapi#/components/schemas/X``). Restating a pointer is addressing
# plumbing, not validation logic — resolution itself is the library's job, and
# recursive schemas that descend into the instance validate without any guard.
# A reference that loops back to itself *without* descending (a pure
# ``{"$ref": ...}`` cycle) would recurse forever; that case is named up front
# by _non_consuming_ref_cycle() and reported as a violation.
_DOC_URI = "nextdns-openapi"


def _with_absolute_refs(node: Any) -> Any:
    """Restate document-local ``$ref`` pointers to address the registered document."""

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: f"{_DOC_URI}{item}"
                if key == "$ref" and isinstance(item, str) and item.startswith("#")
                else rewrite(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    return rewrite(node)


_spec_registry_memo: tuple[dict[str, Any], Registry[Any]] | None = None


def _registry_for_spec(spec: dict[str, Any]) -> Registry[Any]:
    """The validator registry holding the spec document as its only resource.

    One registry per spec object: callers pass whatever dict they hold — the
    shipped spec, or a test fixture — and a swapped spec must not be served the
    previous document's resolution context.
    """
    global _spec_registry_memo
    if _spec_registry_memo is not None and _spec_registry_memo[0] is spec:
        return _spec_registry_memo[1]
    resource: Resource[Any] = Resource(contents=spec, specification=DRAFT202012)
    registry: Registry[Any] = Registry().with_resource(_DOC_URI, resource)
    _spec_registry_memo = (spec, registry)
    return registry


def _run_validator(spec: dict[str, Any], schema: dict[str, Any], data: Any) -> list[str]:
    validator = jsonschema.Draft202012Validator(_with_absolute_refs(schema), registry=_registry_for_spec(spec))
    try:
        return sorted(f"{error.json_path}: {error.message}" for error in validator.iter_errors(data))
    except Unresolvable as error:
        return [f"$: unresolvable schema reference: {error}"]
    except RecursionError:
        # Backstop for a non-consuming reference cycle the walk above did not
        # name: reported as a violation, never as a permissive pass. A cycle
        # that descends through ``properties``/``items`` cannot reach this,
        # because every step consumes instance depth and payloads are finite.
        return ["$: schema reference recurses back to itself without descending into the instance"]


def _validation_errors(spec: dict[str, Any], schema: dict[str, Any], data: Any) -> list[str]:
    """Validate ``data`` against ``schema``, resolving ``$ref`` pointers in ``spec``.

    Delegates to ``jsonschema.Draft202012Validator`` — the same validator class
    the server's spec tools compile per route — so the E2E check enforces the
    whole document (``required``, ``enum``, ``pattern``, ``additionalProperties``,
    array bounds, composed schemas) instead of the subset a local
    re-implementation happened to cover. Returns one ``path: message`` string
    per violation; a reference the document cannot resolve, and a reference that
    recurses without ever descending into the instance, are each reported as a
    violation rather than crashing the run or passing silently.
    """
    if not isinstance(schema, dict):
        return []
    cycle = _non_consuming_ref_cycle(spec, schema)
    if cycle is not None:
        return [f"$: schema reference {cycle!r} recurses back to itself without descending into the instance"]
    return _run_validator(spec, schema, data)


def _resolve_schema_ref(spec: dict[str, Any], ref: str) -> Any:
    """Resolve one document-local ``$ref`` against the spec document."""
    try:
        resolver = _registry_for_spec(spec).resolver(base_uri=_DOC_URI)
        return resolver.lookup(ref).contents
    except Unresolvable:
        return None


# Keywords whose subschemas apply to the *same* instance location. A reference
# cycle through only these re-enters the same (instance, schema) pair forever;
# a cycle that passes through ``properties``/``items`` descends into the
# instance and terminates on any finite payload.
_SAME_INSTANCE_KEYWORDS = ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas")


def _visited(chain: tuple[Any, ...], node: Any) -> bool:
    return any(entry is node for entry in chain)


def _ref_cycle(spec: dict[str, Any], schema: dict[str, Any], chain: tuple[Any, ...]) -> str | None:
    """The ``$ref`` this schema points at when it closes a same-instance loop."""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return None
    target = _resolve_schema_ref(spec, ref)
    if isinstance(target, dict) and _visited(chain, target):
        return ref
    return _non_consuming_ref_cycle(spec, target, chain)


def _same_instance_group(keyword: str, subschemas: dict[str, Any]) -> list[Any]:
    return list(subschemas.values()) if keyword == "dependentSchemas" else [subschemas]


def _same_instance_members(schema: dict[str, Any]) -> Iterator[Any]:
    """The subschemas that apply to the same instance location as this one."""
    for keyword in _SAME_INSTANCE_KEYWORDS:
        subschemas = schema.get(keyword)
        if isinstance(subschemas, dict):
            yield from _same_instance_group(keyword, subschemas)
        elif isinstance(subschemas, list):
            yield from subschemas


def _member_cycle(spec: dict[str, Any], schema: dict[str, Any], chain: tuple[Any, ...]) -> str | None:
    for member in _same_instance_members(schema):
        found = _non_consuming_ref_cycle(spec, member, chain)
        if found is not None:
            return found
    return None


def _non_consuming_ref_cycle(spec: dict[str, Any], schema: Any, chain: tuple[Any, ...] = ()) -> str | None:
    """Return the ``$ref`` that re-enters an already-visited schema without descending.

    ``jsonschema`` follows references lazily and recurses on the same instance
    when a schema refers back to itself through same-instance keywords only — the
    plain ``{"$ref": ...}`` loop, or a component whose content is just an
    ``allOf`` of itself. Such a schema has no decidable outcome; the reference is
    named as a violation rather than validated permissively (the removed
    resolver's fallback) or recursed into. Every step extends ``chain`` with a
    schema identity, so the walk itself terminates: a repeated identity is the
    stop condition, not a recursion risk.
    """
    if not isinstance(schema, dict) or _visited(chain, schema):
        return None
    chain = chain + (schema,)
    ref_cycle = _ref_cycle(spec, schema, chain)
    if ref_cycle is not None:
        return ref_cycle
    return _member_cycle(spec, schema, chain)


def _body_ref_target(spec: dict[str, Any], schema: dict[str, Any], seen: set[str]) -> Any:
    ref = schema["$ref"]
    if not isinstance(ref, str) or ref in seen:
        return None
    seen.add(ref)
    target = _resolve_schema_ref(spec, ref)
    return target if isinstance(target, dict) else None


def _resolve_body_schema(spec: dict[str, Any], schema: Any) -> Any:
    """Follow a top-level ``$ref`` chain to the schema document it names.

    The title guard reads the schema document itself rather than a payload, so
    it needs the named schema, not a validation result; resolution goes through
    the same registry as validation rather than a second pointer-walker.
    """
    seen: set[str] = set()
    while isinstance(schema, dict) and "$ref" in schema:
        target = _body_ref_target(spec, schema, seen)
        if target is None:
            return {}
        schema = target
    return schema


def _declared_schema_type(schema: Any) -> str | None:
    """The JSON type a schema document declares, for guards that read specs.

    An explicit ``type`` wins; a schema with ``properties`` or ``items``
    declares its shape implicitly, the same inference the document relies on.
    """
    if not isinstance(schema, dict):
        return None
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def _candidate_schemas(tool_name: str, spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The schema(s) an answer for this tool can be checked against."""
    # A proxied tool answers with its endpoint's body, so the operation is the tool
    # name. A tool with a pinned shape answers with something the endpoint does not
    # return — a DoH response, or the receipt for a file the endpoint streams elsewhere
    # — so only the pinned schema applies; checking it against the endpoint's document
    # would compare a receipt to a CSV.
    literal_schema = TOOL_RESPONSE_SCHEMAS.get(tool_name)
    if literal_schema is not None:
        return [(tool_name, literal_schema)]
    if tool_name in local_tool_names():
        return []
    operation_schema = get_operation_response_schema(spec, tool_name)
    if operation_schema is not None:
        return [(tool_name, operation_schema)]
    return []


def _validation_report(
    spec: dict[str, Any], schemas: list[tuple[str, dict[str, Any]]], response_data: Any
) -> tuple[str, list[str]]:
    # One tool, one operation, one schema; the loop is here so an answer with more than
    # one candidate is reported by which one it failed.
    all_errors: list[str] = []
    for op_id, schema in schemas:
        errors = _validation_errors(spec, schema, response_data)
        if not errors:
            return "VALID", []
        all_errors.append(f"{op_id}:")
        for error in errors:
            all_errors.append(f"  - {error}")
    return "INVALID", all_errors


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
    schemas = _candidate_schemas(tool_name, spec)
    if not schemas:
        return "SKIPPED", [f"No schemas found for tool '{tool_name}'"]
    return _validation_report(spec, schemas, response_data)


def _print_usage_and_exit() -> None:
    print("Usage: validate_schema.py <tool_name> <json_response>", file=sys.stderr)
    print('Example: validate_schema.py listProfiles \'{"data":[{"id":"abc123"}]}\'', file=sys.stderr)
    sys.exit(1)


def _load_spec_and_response(spec_path: Path, json_response: str) -> tuple[dict[str, Any], Any]:
    try:
        spec = load_openapi_spec(str(spec_path))
        response_data = json.loads(json_response)
    except yaml.YAMLError as e:
        print(f"ERROR: Failed to parse OpenAPI spec: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse JSON response: {e}", file=sys.stderr)
        sys.exit(1)
    return spec, response_data


def _report_verdict(tool_name: str, status: str, errors: list[str]) -> None:
    if status == "SKIPPED":
        if errors:
            print(f"WARNING: {errors[0]}", file=sys.stderr)
        print("SKIPPED", file=sys.stdout)
        sys.exit(0)
    if status == "VALID":
        print("VALID", file=sys.stdout)
        sys.exit(0)
    print(f"SCHEMA_ERRORS: no matching schema for {tool_name}", file=sys.stderr)
    for line in errors:
        print(line, file=sys.stderr)
    sys.exit(1)


def main():
    """Main entry point for schema validation."""
    if len(sys.argv) < 3:
        _print_usage_and_exit()

    tool_name = sys.argv[1]
    json_response = sys.argv[2]

    # Find OpenAPI spec (the same path the server reads; imported from the package)
    spec_path = SPEC_PATH

    if not spec_path.exists():
        print(f"ERROR: OpenAPI spec not found at {spec_path}", file=sys.stderr)
        sys.exit(1)

    # Load spec and response
    spec, response_data = _load_spec_and_response(spec_path, json_response)

    status, errors = validate_tool_response(tool_name, response_data, spec)
    _report_verdict(tool_name, status, errors)


if __name__ == "__main__":
    main()
