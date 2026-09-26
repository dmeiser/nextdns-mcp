"""Unit tests for the E2E schema validator."""

from scripts.validate_schema import (
    _extract_schema_type,
    resolve_schema,
    validate_schema,
    validate_tool_response,
)


def test_resolve_schema_repeated_refs_in_siblings():
    """Repeated $ref pointers in sibling branches must each resolve fully.

    Before the fix, the shared ``_seen`` set was mutated in-place, so the second
    occurrence of a ``$ref`` was incorrectly treated as a cycle and replaced with
    an empty dict.
    """
    spec = {
        "components": {
            "schemas": {
                "Name": {"type": "string"},
            }
        }
    }
    schema = {
        "type": "object",
        "properties": {
            "first_name": {"$ref": "#/components/schemas/Name"},
            "last_name": {"$ref": "#/components/schemas/Name"},
        },
    }

    resolved = resolve_schema(spec, schema)

    assert resolved["properties"]["first_name"] == {"type": "string"}
    assert resolved["properties"]["last_name"] == {"type": "string"}


def test_resolve_schema_cycle_guard_still_works():
    """Self-referential schemas must still be broken to avoid infinite recursion."""
    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Node"}

    resolved = resolve_schema(spec, schema)

    assert resolved["type"] == "object"
    assert resolved["properties"]["child"] == {"type": "object"}


def test_cyclic_ref_invalid_response_fails():
    """An invalid response on a cyclic $ref must fail schema validation instead of passing."""
    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Node"}
    resolved = resolve_schema(spec, schema)

    # Invalid response: child is a primitive string instead of an object
    invalid_response = {"name": "root", "child": "not-a-node"}
    errors = validate_schema(invalid_response, resolved)
    assert len(errors) == 1
    assert "$.child: expected object, got str" in errors[0]

    # Valid response: child is an object
    valid_response = {"name": "root", "child": {"name": "leaf"}}
    assert validate_schema(valid_response, resolved) == []


def test_cyclic_ref_tool_response_validation():
    """validate_tool_response must reject responses where a cyclic ref violates expected type."""
    spec = {
        "paths": {
            "/nodes": {
                "get": {
                    "operationId": "getNodes",
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        },
    }
    # Invalid response should fail validation
    status, errors = validate_tool_response("getNodes", {"child": 12345}, spec)
    assert status == "INVALID"
    assert any("expected object, got int" in e for e in errors)

    # Valid response should pass validation
    status, errors = validate_tool_response("getNodes", {"child": {}}, spec)
    assert status == "VALID"
    assert errors == []


def test_cyclic_ref_array_items_invalid_response_fails():
    """A cyclic $ref in array items must validate the items' expected type."""
    spec = {
        "components": {
            "schemas": {
                "TreeNode": {
                    "type": "object",
                    "properties": {
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TreeNode"},
                        }
                    },
                }
            }
        }
    }
    schema = {"$ref": "#/components/schemas/TreeNode"}
    resolved = resolve_schema(spec, schema)
    assert resolved["properties"]["children"]["items"] == {"type": "object"}

    # Invalid response: item in children is not an object
    errors = validate_schema({"children": ["invalid_string"]}, resolved)
    assert len(errors) == 1
    assert "$.children[0]: expected object, got str" in errors[0]

    # Valid response: item in children is an object
    assert validate_schema({"children": [{"children": []}]}, resolved) == []


def test_cyclic_ref_nested_cycle_invalid_response_fails():
    """A nested cyclic $ref must validate the expected type and fail on invalid data."""
    spec = {
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "next": {"$ref": "#/components/schemas/Item"},
                    },
                },
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Item"}
    resolved = resolve_schema(spec, schema)
    assert resolved["properties"]["next"] == {"type": "object"}

    errors = validate_schema({"next": 42}, resolved)
    assert len(errors) == 1
    assert "$.next: expected object, got int" in errors[0]

    assert validate_schema({"next": {}}, resolved) == []


def test_cyclic_ref_loop_without_type_falls_back_to_empty():
    """A direct cycle with no type definition must safely break without crashing."""
    spec = {
        "components": {
            "schemas": {
                "Loop": {"$ref": "#/components/schemas/Loop"},
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Loop"}
    resolved = resolve_schema(spec, schema)
    assert resolved == {}


def test_resolve_schema_seen_scoped_per_top_level_call():
    """_seen must be scoped per top-level call so subsequent calls are independent."""
    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Node"}

    resolved1 = resolve_schema(spec, schema)
    resolved2 = resolve_schema(spec, schema)

    assert resolved1 == resolved2
    assert resolved1["properties"]["child"] == {"type": "object"}
    assert resolved2["properties"]["child"] == {"type": "object"}


def test_extract_schema_type_coverage():
    """Cover edge cases in _extract_schema_type (allOf, anyOf, oneOf, non-dict)."""
    assert _extract_schema_type(None, "not-a-dict") is None
    assert _extract_schema_type(None, {"properties": {"a": {}}}) == "object"
    assert _extract_schema_type(None, {"items": {"type": "string"}}) == "array"
    assert _extract_schema_type(None, {"allOf": [{"type": "string"}]}) == "string"
    assert _extract_schema_type(None, {"anyOf": [{"type": "integer"}]}) == "integer"
    assert _extract_schema_type(None, {"oneOf": [{"type": "boolean"}]}) == "boolean"
    assert _extract_schema_type(None, {"oneOf": [{"invalid": "no-type"}]}) is None
    assert _extract_schema_type(None, {"$ref": "nonexistent"}) is None
    assert _extract_schema_type(None, {"$ref": "#/some/ref"}) is None
    assert _extract_schema_type({"components": {}}, {"$ref": "#/components/schemas/Missing"}) is None


def test_validate_tool_response_manage_logs_download():
    """manageLogs download CSV envelope should validate successfully."""
    from scripts.validate_schema import validate_tool_response

    spec = {
        "paths": {
            "/profiles/{profile_id}/logs/download": {
                "get": {
                    "operationId": "downloadLogs",
                    "responses": {"200": {"content": {"text/csv": {}}}},
                }
            }
        }
    }
    response_data = {
        "content_type": "text/csv",
        "size": 128,
        "data": "timestamp,domain,client\n1700000000,example.com,client-1",
    }
    status, errors = validate_tool_response("manageLogs", response_data, spec)
    assert status == "VALID"
    assert errors == []
