"""Unit tests for the atomic tool surface (one MCP tool per API operation).

The grouped surface asks the caller to pick an area tool and then an
``operation`` inside it; this surface generates one tool per operation of
``nextdns-openapi.yaml``, so the request is decided by the endpoint the tool is
bound to. These tests pin the three things that make that verifiable: the
generated catalog, the endpoint registry the call log reads, and the guidance
the server publishes about a catalog it did not build by hand.

SPDX-License-Identifier: MIT
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import atomic_tools
from nextdns_mcp.call_log import resolve_endpoint
from nextdns_mcp.surface import list_profiles_call
from nextdns_mcp.utils import _note_404_on_a_profile_path

OPERATION_COUNT = 79

# Not offered: a Server-Sent Events stream has nothing to answer a tool call with.
NOT_OFFERED = ("streamLogs",)


def _fake_api(payload: dict[str, Any], calls: list[Any]) -> Callable[..., Any]:
    """Stand in for the HTTP layer, recording how the endpoint was addressed."""

    async def _request(method, url, params=None, json_body=None):
        calls.append({"method": method, "url": url, "params": params, "body": json_body})
        return payload

    return _request


@pytest.fixture
def spec() -> dict:
    return atomic_tools.load_spec()


@pytest.fixture
def operations(spec) -> list[atomic_tools.ApiOperation]:
    return atomic_tools.parse_operations(spec)


def _by_id(operations: list[atomic_tools.ApiOperation]) -> dict[str, atomic_tools.ApiOperation]:
    return {operation.operation_id: operation for operation in operations}


class TestSpecParsing:
    def test_every_spec_operation_is_parsed(self, operations):
        assert len(operations) == OPERATION_COUNT

    def test_operation_ids_are_unique(self, operations):
        assert len({operation.operation_id for operation in operations}) == OPERATION_COUNT

    def test_streaming_endpoint_is_not_offered(self, operations):
        offered = {operation.operation_id for operation in atomic_tools.offerable(operations)}
        assert NOT_OFFERED[0] in _by_id(operations)
        assert NOT_OFFERED[0] not in offered
        assert len(offered) == OPERATION_COUNT - len(NOT_OFFERED)

    def test_tool_names_are_the_operation_ids(self, operations, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        assert set(atomic_tools.atomic_tool_names()) == {
            operation.operation_id for operation in atomic_tools.offerable(operations)
        }

    def test_parameters_carry_path_and_query_with_their_descriptions(self, operations):
        get_logs = _by_id(operations)["getLogs"]
        schema = get_logs.parameters
        assert schema["additionalProperties"] is False
        assert "profile_id" in schema["required"]
        assert schema["properties"]["limit"]["type"] == "integer"
        assert "log entries" in schema["properties"]["limit"]["description"].lower()

    def test_an_array_body_becomes_one_entries_argument(self, operations):
        replace = _by_id(operations)["replaceDenylist"]
        entries = replace.parameters["properties"]["entries"]
        assert entries["type"] == "array"
        assert entries["items"]["properties"]["id"]["type"] == "string"
        assert "entries" in replace.parameters["required"]
        assert replace.body_mode == "array"

    def test_an_object_body_is_flattened_into_the_parameters(self, operations):
        create = _by_id(operations)["createProfile"]
        assert "name" in create.parameters["properties"]
        assert "name" in create.parameters["required"]
        assert create.body_props == ("name",)

    def test_a_referenced_body_is_dereferenced(self, operations):
        add = _by_id(operations)["addToDenylist"]
        # AddContentEntry: the body is a $ref, so the properties only appear
        # once it has been followed.
        assert "id" in add.parameters["properties"]

    def test_description_is_the_specs_own_words_plus_the_endpoint(self, operations):
        add = _by_id(operations)["addRewrite"]
        assert "Add a DNS rewrite" in add.description
        assert "POST /profiles/{profile_id}/rewrites" in add.description
        assert len(add.description) <= atomic_tools.DESCRIPTION_MAX_CHARS

    def test_descriptions_stay_bounded(self, operations):
        assert all(len(operation.description) <= atomic_tools.DESCRIPTION_MAX_CHARS for operation in operations)

    def test_deref_passes_through_a_plain_node(self):
        assert atomic_tools._deref({"type": "string"}, {}) == {"type": "string"}

    def test_deref_of_an_external_ref_is_ignored(self):
        assert atomic_tools._deref({"$ref": "https://example.com/spec"}, {}) == {}

    def test_deref_to_a_non_object_yields_nothing(self):
        assert atomic_tools._deref({"$ref": "#/x"}, {"x": ["not", "an", "object"]}) == {}

    def test_a_ref_cycle_stops_instead_of_spinning(self):
        assert atomic_tools._deref({"$ref": "#/a"}, {"a": {"$ref": "#/a"}}) == {}

    def test_a_parameter_keeps_the_description_written_on_the_parameter(self, spec):
        # OpenAPI states a parameter's meaning beside the parameter, not in its
        # schema; the published argument has to carry it.
        logs_limit = spec["components"]["parameters"]["LogsLimit"]
        assert "limit" in logs_limit["name"]
        merged = atomic_tools._parameter_schema(logs_limit, spec)
        assert merged["type"] == "integer"
        assert "log entries" in merged["description"].lower()

    def test_a_schema_with_nothing_publishable_is_a_string(self):
        assert atomic_tools._property_schema({"title": "unnamed"}) == {"type": "string"}

    def test_an_array_item_without_properties_is_described_by_shape(self):
        assert atomic_tools._item_shape({}) == "an object"
        assert atomic_tools._item_shape({"properties": {"id": {}}}) == "id"

    def test_a_parameter_with_no_name_is_not_published(self):
        """A malformed parameter document must not become an argument called None."""
        operations = atomic_tools.parse_operations(_mini_spec())
        assert list(operations[0].parameters["properties"]) == ["a"]

    def test_a_parameter_with_no_description_publishes_without_one(self):
        operations = atomic_tools.parse_operations(_mini_spec())
        assert operations[0].parameters["properties"]["a"] == {"type": "string"}


def _mini_spec() -> dict[str, Any]:
    """The smallest spec the generator has to survive: one operation, two
    parameters, one of them unnamed and neither described."""
    return {
        "paths": {
            "/x/{a}": {
                "get": {
                    "operationId": "getX",
                    "parameters": [
                        {"in": "path", "name": "a", "schema": {"type": "string"}},
                        {"in": "query", "schema": {"type": "string"}},
                    ],
                }
            }
        }
    }


class TestCoercion:
    """Arguments arrive as JSON; the endpoint's declared type decides what to send."""

    def test_a_value_that_is_already_the_declared_type_is_left_alone(self):
        assert atomic_tools._coerce("limit", 20, {"type": "integer"}) == 20

    def test_a_parameter_the_spec_does_not_type_is_left_alone(self):
        assert atomic_tools._coerce("thing", "20", {}) == "20"

    def test_false_is_a_boolean_too(self):
        assert atomic_tools._coerce("raw", "false", {"type": "boolean"}) is False
        assert atomic_tools._coerce("raw", "0", {"type": "boolean"}) is False

    def test_a_decimal_string_for_a_number_parameter_becomes_a_number(self):
        assert atomic_tools._coerce("w", "1.5", {"type": "number"}) == 1.5


class TestPublishedContractIsTheRealOne:
    """The wording a caller reads has to be the contract this API honours.

    The generated tools inherit the spec's parameter descriptions, and the spec
    said the analytics window is "Unix timestamp or ISO 8601". Probed against the
    live API it also accepts a relative offset (`-1d`, `-12h`, `-30d`) and `now`,
    rejects the Grafana spelling `now-1d` with "`from` must be number.", and ends
    at now when `to` is left out — which is what the grouped tool relies on
    (`SERIES_DEFAULT_FROM`). A caller told only about absolute timestamps has no
    way to say "the last day", and one live run answered that step by asking the
    user for timestamps instead of acting.
    """

    def _from_doc(self, operations) -> str:
        return _by_id(operations)["getAnalyticsDomains"].parameters["properties"]["from"]["description"]

    def test_the_relative_offset_is_named_in_the_this_server_sends(self, operations):
        from nextdns_mcp.tools.analytics import SERIES_DEFAULT_FROM

        assert SERIES_DEFAULT_FROM in self._from_doc(operations)

    def test_every_accepted_spelling_is_named(self, operations):
        doc = self._from_doc(operations)
        for spelling in ("epoch seconds", "ISO 8601", "bare date", "now"):
            assert spelling in doc

    def test_the_spelling_upstream_rejects_is_named_too(self, operations):
        doc = self._from_doc(operations)
        assert "now-1d" in doc
        assert "must be number" in doc

    def test_to_can_be_left_out(self, operations):
        doc = _by_id(operations)["getAnalyticsDomains"].parameters["properties"]["to"]["description"]
        assert "omit" in doc


class TestRegistration:
    async def test_the_atomic_surface_publishes_one_tool_per_operation(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        async with Client(atomic_tools_server()) as client:
            names = [tool.name for tool in await client.list_tools()]
        assert len(names) == OPERATION_COUNT - len(NOT_OFFERED)
        assert "addToDenylist" in names
        assert "manageLists" not in names

    async def test_the_grouped_surface_is_the_default(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            names = [tool.name for tool in await client.list_tools()]
        assert "manageLists" in names
        assert "addToDenylist" not in names

    def test_registering_records_each_endpoint_for_the_call_log(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        server = FastMCP("probe")
        names = atomic_tools.register_atomic_tools(server)
        assert resolve_endpoint("addToDenylist") == ("POST", "/profiles/{profile_id}/denylist")
        assert resolve_endpoint("getAnalyticsDevicesSeries") == (
            "GET",
            "/profiles/{profile_id}/analytics/devices;series",
        )
        assert resolve_endpoint("manageLists") is None
        assert "addToDenylist" in names

    def test_the_instructions_describe_the_surface_that_is_published(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        from nextdns_mcp.server import build_mcp_server

        instructions = build_mcp_server().instructions
        assert "one tool per endpoint" in instructions
        # The grouped surface's contract: there is no operation to choose here.
        assert "an `operation` (get, list, add" not in instructions

    def test_the_grouped_instructions_are_unchanged(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        from nextdns_mcp.server import build_mcp_server

        assert "an `operation` (get, list, add" in build_mcp_server().instructions

    async def test_the_usage_guide_is_not_published_on_the_atomic_surface(self, monkeypatch):
        """The guide walks the grouped tools; on this surface they do not exist."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        async with Client(atomic_tools_server()) as client:
            assert await client.list_prompts() == []


def atomic_tools_server():
    """Build the server with the atomic surface (env already set by the caller)."""
    from nextdns_mcp.server import build_mcp_server

    return build_mcp_server()


class TestToolCall:
    @pytest.fixture
    def calls(self):
        return []

    def _tool(self, monkeypatch, calls, operation_id: str, payload: dict | None = None):
        monkeypatch.setattr(
            atomic_tools,
            "_api_request_payload",
            _fake_api(payload or {"data": []}, calls),
        )
        operation = _by_id(atomic_tools.parse_operations(atomic_tools.load_spec()))[operation_id]
        return atomic_tools.AtomicApiTool(operation)

    async def test_a_read_sends_the_path_and_query_it_declares(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getAnalyticsDomains")
        result = await tool.run({"profile_id": "abc123", "from": "1700000000", "limit": "20"})
        assert calls == [
            {
                "method": "GET",
                "url": "/profiles/abc123/analytics/domains",
                # "20" is accepted for an integer parameter: the caller means 20.
                "params": {"from": "1700000000", "limit": 20},
                "body": None,
            }
        ]
        assert result.structured_content == {"data": []}

    async def test_boolean_query_parameters_accept_their_string_spellings(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getLogs")
        await tool.run({"profile_id": "abc123", "raw": "true", "status": "blocked"})
        assert calls[0]["params"] == {"raw": True, "status": "blocked"}

    async def test_a_boolean_sent_as_a_word_that_is_not_false_is_sent_as_written(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getLogs")
        await tool.run({"profile_id": "abc123", "raw": "off"})
        assert calls[0]["params"] == {"raw": "off"}

    async def test_a_number_that_is_not_a_number_is_sent_as_written(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getLogs")
        await tool.run({"profile_id": "abc123", "limit": "top ten"})
        assert calls[0]["params"] == {"limit": "top ten"}

    async def test_a_missing_path_argument_names_the_endpoint_and_the_id(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        result = await tool.run({"profile_id": " "})
        payload = result.structured_content
        assert payload["code"] == "missing_required_argument"
        assert "profile_id" in payload["error"]
        assert "GET /profiles/{profile_id}/denylist" in payload["error"]
        assert "listProfiles()" in payload["hint"]
        assert calls == []

    async def test_a_missing_entry_argument_says_where_entry_ids_come_from(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "removeFromDenylist")
        payload = (await tool.run({"profile_id": "abc123"})).structured_content
        assert "entry id" in payload["hint"]

    async def test_an_unknown_missing_argument_lists_what_the_endpoint_needs(self, monkeypatch, calls):
        operation = atomic_tools.ApiOperation(
            operation_id="weirdEndpoint",
            method="GET",
            path="/profiles/{profile_id}/weird/{thing_id}",
            description="d",
            parameters={"properties": {}, "required": ["thing_id"]},
            path_params=("thing_id",),
        )
        payload = (await atomic_tools.AtomicApiTool(operation).run({})).structured_content
        assert payload["error"].startswith("weirdEndpoint needs thing_id")
        assert payload["hint"] == "required by this endpoint: thing_id"

    async def test_an_object_body_is_rebuilt_from_the_flattened_arguments(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "updatePrivacySettings")
        await tool.run({"profile_id": "abc123", "disguisedTrackers": True, "allowAffiliate": False})
        assert calls[0]["url"] == "/profiles/abc123/privacy"
        assert calls[0]["body"] == {"disguisedTrackers": True, "allowAffiliate": False}

    async def test_an_object_body_with_nothing_sent_is_no_body(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "updateProfile")
        await tool.run({"profile_id": "abc123"})
        assert calls[0]["body"] is None

    async def test_an_array_body_is_sent_as_the_array(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "replaceSecurityTLDs")
        await tool.run({"profile_id": "abc123", "entries": [{"id": "zip", "active": True}]})
        assert calls[0]["method"] == "PUT"
        assert calls[0]["body"] == [{"id": "zip", "active": True}]

    async def test_an_array_body_default_is_an_empty_collection(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "replaceSecurityTLDs")
        await tool.run({"profile_id": "abc123"})
        assert calls[0]["body"] == []

    async def test_the_download_endpoint_uses_the_guarded_downloader(self, monkeypatch, calls):
        from nextdns_mcp.tools import logs as logs_module

        async def fake_download(profile_id):
            calls.append({"profile_id": profile_id})
            return {"path": "/tmp/x.csv", "bytes": 10}

        monkeypatch.setattr(logs_module, "_download_logs_to_tempfile", fake_download)
        tool = self._tool(monkeypatch, calls, "downloadLogs")
        result = await tool.run({"profile_id": "abc123"})
        assert result.structured_content == {"path": "/tmp/x.csv", "bytes": 10}
        assert calls == [{"profile_id": "abc123"}]

    async def test_the_download_endpoint_still_needs_a_profile(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "downloadLogs")
        payload = (await tool.run({"profile_id": ""})).structured_content
        assert payload["code"] == "missing_required_argument"
        assert calls == []

    def test_repr_names_the_endpoint(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        assert repr(tool) == "AtomicApiTool(name='getDenylist', method=GET, path=/profiles/{profile_id}/denylist)"


class TestCallLogEndpoint:
    """A generated tool's record has to say which API call it executed."""

    def test_the_record_carries_the_endpoint_and_a_method_derived_write(self, tmp_path):
        from nextdns_mcp.call_log import CallLogMiddleware

        path = tmp_path / "calls.jsonl"
        middleware = CallLogMiddleware(path=str(path))
        atomic_tools.register_endpoint("addRewrite", "POST", "/profiles/{profile_id}/rewrites")
        atomic_tools.register_endpoint("listRewrites", "GET", "/profiles/{profile_id}/rewrites")
        middleware._record("addRewrite", {"profile_id": "abc123"}, ok=True, error="")
        middleware._record("listRewrites", {"profile_id": "abc123"}, ok=True, error="")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["endpoint"] == "POST /profiles/{profile_id}/rewrites"
        assert rows[0]["write"] is True
        assert rows[1]["endpoint"] == "GET /profiles/{profile_id}/rewrites"
        assert rows[1]["write"] is False

    def test_a_grouped_record_keeps_the_operation_rule(self, tmp_path):
        from nextdns_mcp.call_log import CallLogMiddleware

        path = tmp_path / "calls.jsonl"
        CallLogMiddleware(path=str(path))._record("manageLists", {"operation": "add"}, ok=True, error="")
        row = json.loads(path.read_text(encoding="utf-8"))
        assert "endpoint" not in row
        assert row["write"] is True

    def test_a_server_without_a_call_log_records_nothing(self, monkeypatch, tmp_path):
        from nextdns_mcp.call_log import CALL_LOG_ENV, CallLogMiddleware

        monkeypatch.delenv(CALL_LOG_ENV, raising=False)
        path = tmp_path / "calls.jsonl"
        CallLogMiddleware(path="")._record("addRewrite", {"profile_id": "abc123"}, ok=True, error="")
        assert not path.exists()


class TestGuidanceFollowsTheSurface:
    """Error hints name a call the caller could actually make."""

    def test_the_profile_pointer_names_the_surface_own_tool(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        assert list_profiles_call() == "listProfiles()"
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "grouped")
        assert list_profiles_call() == 'manageProfiles(operation="list")'

    def test_an_unrecognised_surface_falls_back_to_grouped(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "whichever")
        assert list_profiles_call() == 'manageProfiles(operation="list")'

    def test_the_404_hint_points_at_the_tool_that_exists(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        payload = _note_404_on_a_profile_path({"upstream_code": "notFound"}, "/profiles/abc123/denylist")
        assert "listProfiles() returns the profile ids" in payload["hint"]

    def test_the_grouped_hint_is_unchanged(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_TOOL_SURFACE", raising=False)
        payload = _note_404_on_a_profile_path({"upstream_code": "notFound"}, "/profiles/abc123/denylist")
        assert 'manageProfiles(operation="list") returns the profile ids' in payload["hint"]


class TestToolThroughTheClient:
    """The generated tools are ordinary FastMCP tools: disclosure and call_tool work."""

    async def test_call_tool_runs_a_generated_tool(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")

        async def fake_payload(method, url, params=None, json_body=None):
            return {"data": [{"id": "zip"}]}

        monkeypatch.setattr(atomic_tools, "_api_request_payload", fake_payload)
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed] == ["search_tools", "call_tool"]
            found = await client.call_tool("search_tools", {"query": "add a domain to the denylist"})
            hits = [hit["name"] for hit in (found.structured_content or {})["result"]]
            assert "addToDenylist" in hits
            called = await client.call_tool(
                "call_tool", {"name": "addToDenylist", "arguments": {"profile_id": "abc123", "id": "example.com"}}
            )
            assert called.structured_content == {"data": [{"id": "zip"}]}
