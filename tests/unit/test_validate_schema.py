"""Unit tests for the E2E schema validator."""

from scripts.validate_schema import resolve_schema


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
    assert resolved["properties"]["child"] == {}
