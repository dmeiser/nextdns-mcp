"""Unit tests for the E2E schema validator."""

from pathlib import Path

import pytest

from scripts.run_container_e2e import EXPECTED_TOOLS
from scripts.validate_schema import validate_tool_response

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SPEC_PATH = REPO_ROOT / "src" / "nextdns_mcp" / "nextdns-openapi.yaml"


def _spec_with_get_operation(operation_id: str, schema: dict) -> dict:
    """A one-operation spec whose 200 JSON response is ``schema``."""
    return {
        "paths": {
            "/things": {
                "get": {
                    "operationId": operation_id,
                    "responses": {"200": {"content": {"application/json": {"schema": schema}}}},
                }
            }
        }
    }


def test_repeated_refs_in_siblings_each_resolve():
    """Repeated $ref pointers in sibling branches must each resolve fully."""
    spec = {
        **_spec_with_get_operation(
            "getNames",
            {
                "type": "object",
                "properties": {
                    "first_name": {"$ref": "#/components/schemas/Name"},
                    "last_name": {"$ref": "#/components/schemas/Name"},
                },
            },
        ),
        "components": {"schemas": {"Name": {"type": "string"}}},
    }

    status, errors = validate_tool_response("getNames", {"first_name": 7, "last_name": 8}, spec)
    assert status == "INVALID"
    assert any("$.first_name" in error and "is not of type 'string'" in error for error in errors)
    assert any("$.last_name" in error and "is not of type 'string'" in error for error in errors)

    status, errors = validate_tool_response("getNames", {"first_name": "a", "last_name": "b"}, spec)
    assert status == "VALID"
    assert errors == []


def test_cyclic_ref_invalid_response_fails():
    """An invalid response on a cyclic $ref must fail schema validation instead of passing."""
    spec = {
        **_spec_with_get_operation(
            "getNode",
            {"$ref": "#/components/schemas/Node"},
        ),
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
        },
    }

    # Invalid response: child is a primitive string instead of an object
    status, errors = validate_tool_response("getNode", {"name": "root", "child": "not-a-node"}, spec)
    assert status == "INVALID"
    assert any("$.child" in error and "is not of type 'object'" in error for error in errors)

    # Valid response: child is an object
    status, errors = validate_tool_response("getNode", {"name": "root", "child": {"name": "leaf"}}, spec)
    assert status == "VALID"
    assert errors == []


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
    assert any("$.child" in error and "is not of type 'object'" in error for error in errors)

    # Valid response should pass validation
    status, errors = validate_tool_response("getNodes", {"child": {}}, spec)
    assert status == "VALID"
    assert errors == []


def test_cyclic_ref_array_items_invalid_response_fails():
    """A cyclic $ref in array items must validate the items' expected type."""
    spec = {
        **_spec_with_get_operation(
            "getTree",
            {"$ref": "#/components/schemas/TreeNode"},
        ),
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
        },
    }

    # Invalid response: item in children is not an object
    status, errors = validate_tool_response("getTree", {"children": ["invalid_string"]}, spec)
    assert status == "INVALID"
    assert any("$.children[0]" in error and "is not of type 'object'" in error for error in errors)

    # Valid response: item in children is an object
    status, errors = validate_tool_response("getTree", {"children": [{"children": []}]}, spec)
    assert status == "VALID"
    assert errors == []


def test_cyclic_ref_nested_cycle_invalid_response_fails():
    """A nested cyclic $ref must validate the expected type and fail on invalid data."""
    spec = {
        **_spec_with_get_operation(
            "getItem",
            {"$ref": "#/components/schemas/Item"},
        ),
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "next": {"$ref": "#/components/schemas/Item"},
                    },
                },
            }
        },
    }

    status, errors = validate_tool_response("getItem", {"next": 42}, spec)
    assert status == "INVALID"
    assert any("$.next" in error and "is not of type 'object'" in error for error in errors)

    status, errors = validate_tool_response("getItem", {"next": {}}, spec)
    assert status == "VALID"
    assert errors == []


def test_pure_ref_cycle_validates_without_crashing():
    """A direct $ref cycle with no constraints must not crash or hang validation."""
    spec = {
        **_spec_with_get_operation(
            "getLoop",
            {"$ref": "#/components/schemas/Loop"},
        ),
        "components": {"schemas": {"Loop": {"$ref": "#/components/schemas/Loop"}}},
    }
    status, errors = validate_tool_response("getLoop", {"anything": 1}, spec)
    assert status == "VALID"
    assert errors == []


def test_body_schema_ref_resolution_edges():
    """_resolve_body_schema follows ref chains, and breaks cycles and dangling refs."""
    from scripts.validate_schema import _declared_schema_type, _resolve_body_schema

    spec = {
        "components": {
            "schemas": {
                "Name": {"type": "string"},
                "Aliased": {"$ref": "#/components/schemas/Named"},
                "Named": {"type": "object", "properties": {"a": {}}},
                "Loop": {"$ref": "#/components/schemas/Loop"},
            }
        }
    }

    # Non-ref schemas pass through; the declared type reads explicit and implicit shapes.
    assert _resolve_body_schema(spec, {"type": "array"}) == {"type": "array"}
    assert _declared_schema_type({"type": "array"}) == "array"
    assert _declared_schema_type({"properties": {"a": {}}}) == "object"
    assert _declared_schema_type({"items": {"type": "string"}}) == "array"
    assert _declared_schema_type({"anyOf": [{"type": "integer"}]}) is None
    assert _declared_schema_type("not-a-dict") is None

    # A single ref resolves to its target, and a ref chain to the end of the chain.
    assert _resolve_body_schema(spec, {"$ref": "#/components/schemas/Name"}) == {"type": "string"}
    assert _resolve_body_schema(spec, {"$ref": "#/components/schemas/Aliased"}) == {
        "type": "object",
        "properties": {"a": {}},
    }

    # A cyclic chain and a dangling pointer yield "nothing to read" instead of recursing forever.
    assert _resolve_body_schema(spec, {"$ref": "#/components/schemas/Loop"}) == {}
    assert _resolve_body_schema(spec, {"$ref": "#/components/schemas/Missing"}) == {}


def test_validate_tool_response_download_logs_receipt():
    """``downloadLogs`` is checked against the receipt it returns, not the CSV it streamed.

    The endpoint answers with a redirect to a signed CSV; what the caller gets is the
    local receipt (path, size, row count, bounded preview). Comparing that to the
    endpoint's document would compare a receipt to a file and report a mismatch on every
    successful download, so the receipt shape is pinned in TOOL_RESPONSE_SCHEMAS and the
    endpoint's schema is not consulted for this tool.
    """
    from scripts.validate_schema import validate_tool_response

    spec = {
        "paths": {
            "/profiles/{profile_id}/logs/download": {
                "get": {
                    "operationId": "downloadLogs",
                    "responses": {"200": {"content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}},
                }
            }
        }
    }
    receipt = {
        "content_type": "text/csv",
        "size": 128,
        "file_path": "/tmp/nextdns_logs_e2e/download.csv",
        "row_count": 1,
        "preview": {"text": "timestamp,domain", "line_count": 1, "bytes": 15, "truncated": False},
    }
    status, errors = validate_tool_response("downloadLogs", receipt, spec)
    assert status == "VALID"
    assert errors == []

    # A receipt missing what the caller needs to use the file is a real failure, and it
    # has to be reported rather than wave
    # through by the endpoint's permissive string schema.
    status, errors = validate_tool_response("downloadLogs", {"size": 128}, spec)
    assert status == "INVALID"
    assert any("file_path" in error for error in errors)

    # The CSV itself is not what the tool returns; a bare string is not a receipt.
    status, _ = validate_tool_response("downloadLogs", "timestamp,domain\n", spec)
    assert status == "INVALID"


def test_integer_typed_property_rejects_boolean_value():
    """End-to-end: a bool value fails an integer-typed property instead of passing."""
    spec = {
        "paths": {
            "/things": {
                "get": {
                    "operationId": "getThing",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"count": {"type": "integer"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }
    status, errors = validate_tool_response("getThing", {"count": True}, spec)
    assert status == "INVALID"
    assert any("$.count" in error and "is not of type 'integer'" in error for error in errors)


def test_get_operation_response_schema_resolves_text_csv():
    """downloadLogs declares only text/csv; the extractor must consider it."""
    from scripts.validate_schema import get_operation_response_schema

    spec = {
        "paths": {
            "/profiles/{profile_id}/logs/download": {
                "get": {
                    "operationId": "downloadLogs",
                    "responses": {"200": {"content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}},
                }
            }
        }
    }
    assert get_operation_response_schema(spec, "downloadLogs") == {"type": "string", "format": "binary"}


def test_response_index_is_built_once_per_spec_object():
    """The path walk runs once per spec object, not once per lookup.

    assert_operation_coverage looks up every published read, and the E2E run
    validates every response it sees; walking all paths per call made the
    coverage check quadratic in the document. The cache is keyed by object
    identity so a swapped-in spec (a fixture, a monkeypatched path) is never
    served the previous spec's index.
    """
    from scripts.validate_schema import _response_index, get_operation_response_schema

    spec = {
        "paths": {
            "/profiles": {
                "get": {
                    "operationId": "listProfiles",
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        }
    }
    assert _response_index(spec) is _response_index(spec)  # memo hit: the walk ran once
    assert get_operation_response_schema(spec, "listProfiles") == {"type": "object"}
    other = {
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "array"}}}}},
                }
            }
        }
    }
    assert get_operation_response_schema(other, "listProfiles") is None
    assert get_operation_response_schema(other, "getX") == {"type": "array"}
    assert get_operation_response_schema(spec, "listProfiles") == {"type": "object"}


def test_catalog_constants_are_imported_not_restated():
    """The withheld set, the spec path, and the local tools are the server's own.

    The script used to keep its own copies of WITHHELD_OPERATIONS and LOCAL_TOOLS;
    drift between them and the module that actually decides what is registered is
    invisible until a container and the validator disagree about the catalog.
    """
    from nextdns_mcp import spec_tools
    from nextdns_mcp.config import is_generative_ui_enabled
    from nextdns_mcp.server import _COMPOSITE_TOOLS, published_tool_names
    from scripts import validate_schema

    assert validate_schema.WITHHELD_OPERATIONS is spec_tools.WITHHELD_OPERATIONS
    assert validate_schema.SPEC_PATH == spec_tools.SPEC_PATH
    published = set(published_tool_names())
    assert validate_schema.local_tool_names() == published - set(spec_tools.spec_tool_names())
    expected_local = {tool.__name__ for tool, _ in _COMPOSITE_TOOLS}
    if is_generative_ui_enabled():
        from nextdns_mcp.server import generative_ui_tool_names

        expected_local.update(generative_ui_tool_names())
    assert expected_local == set(validate_schema.local_tool_names())


def test_every_generative_ui_name_is_classified_exactly_once():
    """Every generative-UI name the server publishes is classified exactly once by the validator.

    A content tool has no payload for a schema to describe; a pinned tool's answer is
    stated in TOOL_RESPONSE_SCHEMAS. A name that falls out of both — a rename that
    reached the server, or a reclassification that moved only one side — would have its
    responses silently unchecked forever, which is the drift hole this closes: the
    catalog names live once, in ``generative_ui_tool_names()``, and the validator's
    two classifications must cover them without overlapping.
    """
    from nextdns_mcp.server import (
        generative_ui_content_tool_names,
        generative_ui_data_tool_names,
        generative_ui_tool_names,
    )
    from scripts import validate_schema

    names = set(generative_ui_tool_names())
    content = names & set(validate_schema.CONTENT_TOOLS)
    pinned = names & set(validate_schema.TOOL_RESPONSE_SCHEMAS)
    assert content | pinned == names
    assert content & pinned == set()
    # The classification the validator reaches is the server's own partition, not a restatement.
    assert content == set(generative_ui_content_tool_names())
    assert pinned == set(generative_ui_data_tool_names())


def test_local_tool_names_reflects_generative_ui_toggle_after_import(monkeypatch):
    """local_tool_names() is evaluated dynamically, not snapshotted at import."""
    from scripts import validate_schema

    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "off")
    assert "generate_prefab_ui" not in validate_schema.local_tool_names()

    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
    assert "generate_prefab_ui" in validate_schema.local_tool_names()


def _spec_with_request_body(operation_id: str, body_schema: dict) -> dict:
    """A one-operation spec whose POST body is ``body_schema``."""
    return {
        "paths": {
            "/things": {
                "post": {
                    "operationId": operation_id,
                    "requestBody": {"content": {"application/json": {"schema": body_schema}}},
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        }
    }


def test_coverage_rejects_a_titled_non_object_request_body():
    """A titled non-object body is a silent wrong-request shape waiting to happen.

    FastMCP names the tool argument for a titled non-object request body after
    the title, while the generated executor recognizes a whole-body argument
    only under the literal name ``body`` — the body would be wrapped under the
    wrong name and the wrong shape sent upstream. The shipped document carries
    no titles; the guard is what keeps one from being added unnoticed.
    """
    from scripts.validate_schema import assert_operation_coverage, titled_non_object_bodies

    spec = _spec_with_request_body("replaceThing", {"type": "array", "title": "Things", "items": {"type": "string"}})
    assert titled_non_object_bodies(spec) == ["replaceThing"]
    with pytest.raises(ValueError, match="titled non-object schemas.*replaceThing"):
        assert_operation_coverage(spec, ["replaceThing"])

    # The title behind a $ref counts the same as an inline one.
    ref_spec = {
        "paths": {
            "/things": {
                "post": {
                    "operationId": "replaceThing",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ThingList"}}}
                    },
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        },
        "components": {"schemas": {"ThingList": {"type": "array", "title": "ThingList", "items": {"type": "string"}}}},
    }
    assert titled_non_object_bodies(ref_spec) == ["replaceThing"]


def test_coverage_accepts_titled_objects_and_untitled_non_object_bodies():
    """The guard fires only for the combination that mis-names the argument."""
    from scripts.validate_schema import assert_operation_coverage, titled_non_object_bodies

    titled_object = _spec_with_request_body(
        "updateThing", {"type": "object", "title": "Thing", "properties": {"a": {"type": "string"}}}
    )
    assert titled_non_object_bodies(titled_object) == []
    assert_operation_coverage(titled_object, ["updateThing"])

    untitled_array = _spec_with_request_body("replaceThing", {"type": "array", "items": {"type": "string"}})
    assert titled_non_object_bodies(untitled_array) == []
    assert_operation_coverage(untitled_array, ["replaceThing"])

    # The shipped document — 12 object bodies, 7 arrays, 9 $ref-to-object — passes.
    from scripts.validate_schema import load_openapi_spec

    spec = load_openapi_spec(str(REAL_SPEC_PATH))
    assert titled_non_object_bodies(spec) == []


def test_a_tool_with_a_pinned_shape_does_not_fall_back_to_its_endpoint():
    """A pinned answer is checked against its own shape, never the endpoint's document.

    Falling back would mean a download receipt validated against a CSV schema, which
    fails every successful download — a validator that reports failures the caller
    cannot act on is worse than one that says nothing.
    """
    spec = {
        "paths": {
            "/profiles/{profile_id}/logs/download": {
                "get": {
                    "operationId": "downloadLogs",
                    "responses": {"200": {"content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}},
                }
            }
        }
    }
    status, errors = validate_tool_response("downloadLogs", {"unexpected": "payload"}, spec)
    assert status == "INVALID"
    # The complaint names the receipt's missing fields, not the CSV's string type.
    assert not any("expected string" in error for error in errors)
    assert any("file_path" in error for error in errors)


def test_doh_lookup_response_validated_against_literal_schema():
    """dohLookup has no OpenAPI operation; it validates against a literal response schema."""
    spec = {"paths": {}}
    ok_payload = {
        "data": {"Status": 0, "Answer": []},
        "_metadata": {
            "profile_id": "abc123",
            "query_domain": "example.com",
            "query_type": "A",
            "doh_endpoint": "https://dns.nextdns.io/abc123/dns-query?name=example.com&type=A",
            "status_description": "NOERROR",
        },
    }
    status, errors = validate_tool_response("dohLookup", ok_payload, spec)
    assert status == "VALID"
    assert errors == []

    status, errors = validate_tool_response("dohLookup", {"data": {"Status": 0}}, spec)
    assert status == "INVALID"
    assert any("'_metadata' is a required property" in error for error in errors)


def test_assert_operation_coverage_accepts_the_shipped_document_and_catalog():
    """The document, the published catalog, and the E2E expectation must agree."""
    from scripts.validate_schema import assert_operation_coverage, load_openapi_spec

    spec = load_openapi_spec(str(REAL_SPEC_PATH))
    assert_operation_coverage(spec, EXPECTED_TOOLS)


def test_operation_coverage_rejects_a_catalog_the_document_does_not_describe():
    """A tool that names no operation is drift, and so is a grouped name from the old surface."""
    from scripts.validate_schema import assert_operation_coverage, load_openapi_spec

    spec = load_openapi_spec(str(REAL_SPEC_PATH))
    with pytest.raises(ValueError, match="published tools that name no operation.*brandNewTool"):
        assert_operation_coverage(spec, [*EXPECTED_TOOLS, "brandNewTool"])
    with pytest.raises(ValueError, match="published tools that name no operation.*manageLists"):
        assert_operation_coverage(spec, [*EXPECTED_TOOLS, "manageLists"])


def test_operation_coverage_rejects_an_operation_the_server_does_not_publish():
    """A withheld operation needs a reason; a missing one because the image is stale does not."""
    from scripts.validate_schema import assert_operation_coverage, load_openapi_spec

    spec = load_openapi_spec(str(REAL_SPEC_PATH))
    stale = [tool for tool in EXPECTED_TOOLS if tool != "getLogs"]
    with pytest.raises(ValueError, match="spec operations published as no tool.*getLogs"):
        assert_operation_coverage(spec, stale)


def test_operation_coverage_rejects_a_read_with_no_response_schema():
    """A published read with no schema in the document would validate as SKIPPED forever."""
    from scripts.validate_schema import assert_operation_coverage

    spec = {"paths": {"/profiles": {"get": {"operationId": "listProfiles", "responses": {"200": {}}}}}}
    with pytest.raises(ValueError, match="published reads that resolve to no response schema"):
        assert_operation_coverage(spec, ["listProfiles"])


def test_operation_coverage_rejects_a_local_answer_that_is_neither_pinned_nor_content(monkeypatch):
    """A tool the server performs itself has to have its answer stated somewhere.

    Nothing in the document describes it, so if it is not pinned as a JSON payload and
    not declared content, its responses are silently unchecked forever.
    """
    from scripts import validate_schema

    spec = validate_schema.load_openapi_spec(str(REAL_SPEC_PATH))
    monkeypatch.setattr(validate_schema, "CONTENT_TOOLS", frozenset())
    with pytest.raises(ValueError, match=r"neither pinned nor content:"):
        validate_schema.assert_operation_coverage(spec, EXPECTED_TOOLS)


def test_a_type_union_accepts_any_member():
    """OpenAPI 3.1 writes "a string, or nothing" as ``type: [string, null]``.

    A validator that cannot read that reports a failure for a value the document says is
    fine, so a union passes if any member does.
    """
    spec = {
        "paths": {
            "/receipts": {
                "get": {
                    "operationId": "getReceipt",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"content_type": {"type": ["string", "null"]}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }

    status, errors = validate_tool_response("getReceipt", {"content_type": "text/csv"}, spec)
    assert status == "VALID"
    assert errors == []

    status, errors = validate_tool_response("getReceipt", {"content_type": None}, spec)
    assert status == "VALID"
    assert errors == []

    status, errors = validate_tool_response("getReceipt", {"content_type": 7}, spec)
    assert status == "INVALID"
    assert any("$.content_type" in error for error in errors)


def test_enum_membership_is_enforced():
    """A value outside a declared ``enum`` must fail validation.

    The document declares allowed values with ``enum``; the E2E check is the
    advisory signal for breaking API changes, so a response carrying a value
    the document does not name is exactly what it must catch.
    """
    spec = _spec_with_get_operation(
        "getThingState",
        {"type": "object", "properties": {"state": {"type": "string", "enum": ["active"]}}},
    )

    status, errors = validate_tool_response("getThingState", {"state": "unknown"}, spec)
    assert status == "INVALID"
    assert any("'unknown' is not one of ['active']" in error for error in errors)

    status, errors = validate_tool_response("getThingState", {"state": "active"}, spec)
    assert status == "VALID"
    assert errors == []


def test_enum_behind_a_component_ref_is_enforced():
    """``enum`` constraints must be checked through resolved ``$ref`` pointers."""
    spec = {
        **_spec_with_get_operation(
            "getThingState",
            {"$ref": "#/components/schemas/ThingState"},
        ),
        "components": {
            "schemas": {
                "ThingState": {"type": "object", "properties": {"state": {"type": "string", "enum": ["active"]}}}
            }
        },
    }

    status, errors = validate_tool_response("getThingState", {"state": "unknown"}, spec)
    assert status == "INVALID"
    assert any("'unknown' is not one of ['active']" in error for error in errors)

    status, errors = validate_tool_response("getThingState", {"state": "active"}, spec)
    assert status == "VALID"
    assert errors == []


def test_the_logs_status_enum_matches_the_live_api():
    """`relayed` is a real NextDNS status: live-verified 2026-11-05 against the
    production API (status=relayed returned HTTP 200 with rows whose status is
    'relayed', while allowlisted/denylisted/cached/refused/forwarded were all
    rejected with the enum error). The vendored document must not refuse a value
    the API accepts."""
    from scripts.validate_schema import load_openapi_spec

    spec = load_openapi_spec(str(REAL_SPEC_PATH))
    status_param = spec["components"]["parameters"]["Status"]
    assert "relayed" in status_param["schema"]["enum"]
    logs_get = spec["paths"]["/profiles/{profile_id}/logs"]["get"]
    inline = next(p for p in logs_get["parameters"] if isinstance(p, dict) and p.get("name") == "status")
    assert "relayed" in inline["schema"]["enum"]


def test_search_prefab_components_not_content_tool():
    from scripts import validate_schema

    assert "search_prefab_components" not in validate_schema.CONTENT_TOOLS
    assert "search_prefab_components" in validate_schema.TOOL_RESPONSE_SCHEMAS
    assert validate_schema.TOOL_RESPONSE_SCHEMAS["search_prefab_components"] == {"type": "string"}
