#!/usr/bin/env python3
"""
Validate JSON responses against OpenAPI schema definitions.
Used by E2E tests to catch breaking API changes.

The grouped CRUD tools collapse many OpenAPI operations into a single MCP tool,
so validation is performed against the union of response schemas for the
underlying operations. A response is considered valid if it matches any of the
expected schemas for that grouped tool.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


# Mapping from grouped MCP tool names to the OpenAPI operationIds they dispatch to.
# A response is valid if it conforms to at least one of the listed schemas.
GROUPED_TOOL_OPERATIONS: dict[str, list[str]] = {
    "manageProfiles": [
        "listProfiles",
        "createProfile",
        "getProfile",
        "updateProfile",
        "deleteProfile",
    ],
    "manageSettings": [
        "getSettings",
        "updateSettings",
        "getLogsSettings",
        "updateLogsSettings",
        "getBlockPageSettings",
        "updateBlockPageSettings",
        "getPerformanceSettings",
        "updatePerformanceSettings",
        "getSecuritySettings",
        "updateSecuritySettings",
        "getPrivacySettings",
        "updatePrivacySettings",
        "getParentalControlSettings",
        "updateParentalControlSettings",
    ],
    "manageLists": [
        "getDenylist",
        "addToDenylist",
        "replaceDenylist",
        "updateDenylistEntry",
        "removeFromDenylist",
        "getAllowlist",
        "addToAllowlist",
        "replaceAllowlist",
        "updateAllowlistEntry",
        "removeFromAllowlist",
        "getParentalControlServices",
        "replaceParentalControlServices",
        "addToParentalControlServices",
        "updateParentalControlServiceEntry",
        "removeFromParentalControlServices",
        "getParentalControlCategories",
        "replaceParentalControlCategories",
        "addToParentalControlCategories",
        "updateParentalControlCategoryEntry",
        "removeFromParentalControlCategories",
        "getSecurityTLDs",
        "addSecurityTLD",
        "replaceSecurityTLDs",
        "removeSecurityTLD",
        "getPrivacyBlocklists",
        "addPrivacyBlocklist",
        "replacePrivacyBlocklists",
        "removePrivacyBlocklist",
        "getPrivacyNatives",
        "addPrivacyNative",
        "replacePrivacyNatives",
        "removePrivacyNative",
    ],
    "manageRewrites": [
        "listRewrites",
        "addRewrite",
        "deleteRewrite",
    ],
    "manageLogs": [
        "getLogs",
        "clearLogs",
        "downloadLogs",
    ],
    "queryAnalytics": [
        "getAnalyticsStatus",
        "getAnalyticsStatusSeries",
        "getAnalyticsDomains",
        "getAnalyticsQueryTypes",
        "getAnalyticsQueryTypesSeries",
        "getAnalyticsReasons",
        "getAnalyticsReasonsSeries",
        "getAnalyticsIPs",
        "getAnalyticsIPsSeries",
        "getAnalyticsDNSSEC",
        "getAnalyticsDNSSECSeries",
        "getAnalyticsEncryption",
        "getAnalyticsEncryptionSeries",
        "getAnalyticsIPVersions",
        "getAnalyticsIPVersionsSeries",
        "getAnalyticsProtocols",
        "getAnalyticsProtocolsSeries",
        "getAnalyticsDestinations",
        "getAnalyticsDestinationsSeries",
        "getAnalyticsDevices",
        "getAnalyticsDevicesSeries",
    ],
    "plotAnalytics": [
        "getAnalyticsStatusSeries",
        "getAnalyticsDevicesSeries",
        "getAnalyticsProtocolsSeries",
        "getAnalyticsQueryTypesSeries",
        "getAnalyticsIPVersionsSeries",
        "getAnalyticsDNSSECSeries",
        "getAnalyticsEncryptionSeries",
        "getAnalyticsReasonsSeries",
        "getAnalyticsIPsSeries",
    ],
    "dohLookup": [],
}


def load_openapi_spec(spec_path: str) -> Dict[str, Any]:
    """Load and parse OpenAPI specification."""
    with open(spec_path, "r") as f:
        return yaml.safe_load(f)


def get_operation_response_schema(spec: Dict[str, Any], operation_id: str) -> Optional[Dict[str, Any]]:
    """
    Extract the 200 response schema for a given operationId.

    Returns the schema object or None if not found.
    """
    # Search through all paths and operations
    paths = spec.get("paths", {})
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method in ["get", "post", "put", "patch", "delete"]:
                if operation.get("operationId") == operation_id:
                    # Found the operation, extract 200 response schema
                    responses = operation.get("responses", {})
                    # Some operations return 200, others 201 (created)
                    for success_code in ("200", "201"):
                        success_response = responses.get(success_code, {})
                        content = success_response.get("content", {})
                        json_content = content.get("application/json", {})
                        schema = json_content.get("schema")
                        if schema:
                            return schema

    return None


def validate_field_type(value: Any, expected_type: str) -> bool:
    """Validate that a value matches the expected OpenAPI type."""
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    if expected_type not in type_map:
        return True  # Unknown type, skip validation

    expected_python_type = type_map[expected_type]
    return isinstance(value, expected_python_type)


def validate_schema(data: Any, schema: Dict[str, Any], path: str = "$") -> list[str]:
    """
    Recursively validate data against OpenAPI schema.

    Returns a list of validation error messages.
    """
    errors = []

    if schema is None:
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


def main():
    """Main entry point for schema validation."""
    if len(sys.argv) < 3:
        print("Usage: validate_schema.py <tool_name> <json_response>", file=sys.stderr)
        print('Example: validate_schema.py manageProfiles \'{"data":{"id":"abc123"}}\'', file=sys.stderr)
        sys.exit(1)

    tool_name = sys.argv[1]
    json_response = sys.argv[2]

    # Find OpenAPI spec
    script_dir = Path(__file__).parent
    spec_path = script_dir.parent / "src" / "nextdns_mcp" / "nextdns-openapi.yaml"

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

    # Synthetic 204-style success responses generated by the grouped tools do not
    # correspond to any OpenAPI response body and are therefore not validated.
    if response_data == {"success": True}:
        print("SKIPPED", file=sys.stdout)
        sys.exit(0)

    operation_ids = [tool_name]
    if tool_name in GROUPED_TOOL_OPERATIONS:
        operation_ids = GROUPED_TOOL_OPERATIONS[tool_name]

    schemas: list[tuple[str, Dict[str, Any]]] = []
    for op_id in operation_ids:
        schema = get_operation_response_schema(spec, op_id)
        if schema is not None:
            schemas.append((op_id, schema))

    if not schemas:
        print(f"WARNING: No schemas found for tool '{tool_name}'", file=sys.stderr)
        print("SKIPPED", file=sys.stdout)
        sys.exit(0)

    # Validate against each candidate schema. The response is valid if it matches
    # at least one of the underlying operations for the grouped tool.
    all_errors: list[str] = []
    for op_id, schema in schemas:
        errors = validate_schema(response_data, schema)
        if not errors:
            print("VALID", file=sys.stdout)
            sys.exit(0)
        all_errors.append(f"{op_id}:")
        for error in errors:
            all_errors.append(f"  - {error}")

    print(f"SCHEMA_ERRORS: no matching schema for {tool_name}", file=sys.stderr)
    for line in all_errors:
        print(line, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
