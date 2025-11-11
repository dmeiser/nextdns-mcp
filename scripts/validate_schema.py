#!/usr/bin/env python3
"""
Validate JSON responses against OpenAPI schema definitions.
Used by E2E tests to catch breaking API changes.
"""

import json
import sys
import yaml
from pathlib import Path
from typing import Dict, Any, Optional


def load_openapi_spec(spec_path: str) -> Dict[str, Any]:
    """Load and parse OpenAPI specification."""
    with open(spec_path, 'r') as f:
        return yaml.safe_load(f)


def get_operation_response_schema(spec: Dict[str, Any], operation_id: str) -> Optional[Dict[str, Any]]:
    """
    Extract the 200 response schema for a given operationId.
    
    Returns the schema object or None if not found.
    """
    # Search through all paths and operations
    paths = spec.get('paths', {})
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method in ['get', 'post', 'put', 'patch', 'delete']:
                if operation.get('operationId') == operation_id:
                    # Found the operation, extract 200 response schema
                    responses = operation.get('responses', {})
                    success_response = responses.get('200', {})
                    content = success_response.get('content', {})
                    json_content = content.get('application/json', {})
                    return json_content.get('schema')
    
    return None


def validate_field_type(value: Any, expected_type: str) -> bool:
    """Validate that a value matches the expected OpenAPI type."""
    type_map = {
        'string': str,
        'integer': int,
        'number': (int, float),
        'boolean': bool,
        'array': list,
        'object': dict,
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
    
    schema_type = schema.get('type')
    
    # Handle object types
    if schema_type == 'object':
        if not isinstance(data, dict):
            errors.append(f"{path}: expected object, got {type(data).__name__}")
            return errors
        
        # Check required properties
        required = schema.get('required', [])
        for req_field in required:
            if req_field not in data:
                errors.append(f"{path}: missing required field '{req_field}'")
        
        # Validate properties
        properties = schema.get('properties', {})
        for field_name, field_schema in properties.items():
            if field_name in data:
                field_path = f"{path}.{field_name}"
                field_errors = validate_schema(data[field_name], field_schema, field_path)
                errors.extend(field_errors)
    
    # Handle array types
    elif schema_type == 'array':
        if not isinstance(data, list):
            errors.append(f"{path}: expected array, got {type(data).__name__}")
            return errors
        
        items_schema = schema.get('items')
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
        print("Usage: validate_schema.py <operation_id> <json_response>", file=sys.stderr)
        print("Example: validate_schema.py getProfile '{\"data\":{\"id\":\"abc123\"}}'", file=sys.stderr)
        sys.exit(1)
    
    operation_id = sys.argv[1]
    json_response = sys.argv[2]
    
    # Find OpenAPI spec
    script_dir = Path(__file__).parent
    spec_path = script_dir.parent / 'src' / 'nextdns_mcp' / 'nextdns-openapi.yaml'
    
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
    
    # Get expected schema
    schema = get_operation_response_schema(spec, operation_id)
    
    if schema is None:
        print(f"WARNING: No schema found for operation '{operation_id}'", file=sys.stderr)
        print("SKIPPED", file=sys.stdout)
        sys.exit(0)
    
    # Validate response
    errors = validate_schema(response_data, schema)
    
    if errors:
        print(f"SCHEMA_ERRORS: {len(errors)} validation error(s)", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)
    else:
        print("VALID", file=sys.stdout)
        sys.exit(0)


if __name__ == '__main__':
    main()
