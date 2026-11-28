"""Unit tests for the spec-generated tool surface (one MCP tool per API operation).

The OpenAPI document is the contract, so these tests are mostly about what a
caller is told and what the server then sends: the generated catalog, the
argument splitting, the endpoint registry the call log reads, and the typed
error classification. The refusal/guard half lives in
``test_spec_tools_guards.py``.

SPDX-License-Identifier: MIT
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import spec_tools, transport

SPEC_OPERATIONS = 79
WITHHELD = ("streamLogs",)
COMPOSITES = (
    "plotAnalytics",
    "dohLookup",
    "overviewDashboard",
)


def _fake_api(payload: dict[str, Any], calls: list[Any]):
    """Stand in for the HTTP layer, recording how the endpoint was addressed."""

    async def _request(method, url, params=None, json_body=None, **extra):
        calls.append({"method": method, "url": url, "params": params, "body": json_body, **extra})
        return payload

    return _request


@pytest.fixture
def routes() -> dict[str, Any]:
    """Every offerable route, by operationId."""
    return {route.operation_id: route for route in spec_tools.spec_routes()}


class TestGeneratedCatalog:
    def test_every_spec_operation_is_offered_except_the_stream(self, routes):
        parsed = spec_tools.spec_routes(spec_tools.load_spec())
        assert len(parsed) == SPEC_OPERATIONS - len(WITHHELD)
        assert not {route.operation_id for route in parsed} & set(WITHHELD)

    def test_names_are_the_operation_ids(self, routes):
        assert "addToDenylist" in routes
        assert "getAnalyticsStatusSeries" in routes
        assert spec_tools.spec_tool_names() == sorted(routes)

    def test_the_catalog_order_is_by_name_not_by_spec_order(self):
        names = spec_tools.spec_tool_names()
        assert names == sorted(names)

    def test_mutating_a_returned_list_cannot_corrupt_the_cache(self):
        """The process-wide cache is never handed out: a caller mutating the list
        ``spec_routes()`` returned must not change what any later call sees."""
        original = [route.operation_id for route in spec_tools.spec_routes()]
        routes = spec_tools.spec_routes()
        routes.pop()
        routes.clear()
        after = [route.operation_id for route in spec_tools.spec_routes()]
        assert after == original

    def test_an_explicit_spec_still_gets_a_fresh_parse(self):
        """The cache is for the shipped document only; handing in a spec parses it."""
        parsed = spec_tools.spec_routes(spec_tools.load_spec())
        assert [route.operation_id for route in parsed] == [route.operation_id for route in spec_tools.spec_routes()]
        parsed.pop()
        assert len(spec_tools.spec_routes(spec_tools.load_spec())) == len(spec_tools.spec_routes())

    def test_parameters_keep_their_descriptions_and_types(self, routes):
        properties = routes["getAnalyticsDomains"].flat_param_schema["properties"]
        assert properties["profile_id"]["type"] == "string"
        assert "Profile ID" in properties["profile_id"]["description"]
        assert properties["limit"]["type"] == "integer"
        assert properties["root"]["type"] == "boolean"

    def test_an_array_body_is_one_argument_the_spec_describes(self, routes):
        body = routes["replaceDenylist"].flat_param_schema["properties"]["body"]
        assert body["type"] == "array"
        # The spec writes the description on the body schema itself, so the
        # argument is not an opaque "body" to the caller.
        assert "denylist" in body["description"]
        assert body["items"]["properties"]["id"]["type"] == "string"

    def test_an_object_body_is_flattened_into_the_parameters(self, routes):
        properties = routes["updatePrivacySettings"].flat_param_schema["properties"]
        assert "disguisedTrackers" in properties
        assert routes["updatePrivacySettings"].parameter_map["disguisedTrackers"]["location"] == "body"

    def test_the_description_is_the_specs_own_words_plus_the_endpoint(self, routes):
        description = spec_tools.describe(routes["addToDenylist"])
        assert description.startswith("Add domain to denylist")
        assert "Adds a domain to the profile's denylist" in description
        assert description.endswith("NextDNS API: POST /profiles/{profile_id}/denylist")

    def test_a_description_without_a_summary_still_names_the_endpoint(self):
        class _Route:
            summary = None
            description = "Only a description."
            method = "GET"
            path = "/profiles"

        assert spec_tools.describe(_Route()) == "Only a description.\n\nNextDNS API: GET /profiles"

    def test_descriptions_stay_bounded(self, routes):
        assert all(len(spec_tools.describe(route)) <= spec_tools.DESCRIPTION_MAX_CHARS for route in routes.values())

    def test_the_published_schema_offers_no_copyable_profile_id(self, routes):
        """An example profile id in a schema is a value a caller will send.

        A NextDNS profile id is six hexadecimal characters, which is the shape of a
        real one, so an example like ``abc123`` reads as a live profile and the call
        404s blaming a profile that never existed — the exact loop the 404 hint
        exists for. The pattern states the shape; the profile listing gives values.
        """
        for route in routes.values():
            published = (route.flat_param_schema or {}).get("properties", {}).get("profile_id")
            if published is None:
                continue
            assert "example" not in published, f"{route.operation_id} publishes an example id"
            assert "profile-listing" in published["description"]

    def test_the_window_contract_a_caller_can_act_on_is_in_the_parameter(self, routes):
        """Every spelling the live API accepts, and the one it rejects, in the docs.

        Probed against the API: epoch seconds, ISO 8601, a bare date, ``now`` and a
        relative offset work; ``now-1d`` is rejected with "must be number"; omitting
        ``to`` ends at now. A relative window has no absolute spelling, so a caller
        asked for "the last day" needs ``-1d`` named in the text to act at all.
        """
        window = routes["getAnalyticsDomains"].flat_param_schema["properties"]["from"]["description"]
        for spelling in ("epoch seconds", "ISO 8601", "bare date", "now", "-1d"):
            assert spelling in window
        assert "now-1d" in window
        assert "must be number" in window
        assert "omit" in routes["getAnalyticsDomains"].flat_param_schema["properties"]["to"]["description"]


class TestArgumentSplitting:
    def test_path_arguments_fill_the_template_and_query_arguments_follow(self, routes):
        path, query, body = spec_tools.split_arguments(
            routes["getAnalyticsDomains"], {"profile_id": "abc123", "from": "-1d", "limit": 20}
        )
        assert path == "/profiles/abc123/analytics/domains"
        assert query == {"from": "-1d", "limit": 20}
        assert body is None

    def test_an_object_body_is_rebuilt_from_the_flattened_arguments(self, routes):
        path, query, body = spec_tools.split_arguments(
            routes["updatePrivacySettings"],
            {"profile_id": "abc123", "disguisedTrackers": True, "allowAffiliate": False},
        )
        assert path == "/profiles/abc123/privacy"
        assert query == {}
        assert body == {"disguisedTrackers": True, "allowAffiliate": False}

    def test_an_object_body_with_nothing_sent_is_no_body(self, routes):
        _, _, body = spec_tools.split_arguments(routes["updateProfile"], {"profile_id": "abc123"})
        assert body is None

    def test_an_array_body_is_the_array_itself(self, routes):
        _, _, body = spec_tools.split_arguments(
            routes["replaceSecurityTLDs"], {"profile_id": "abc123", "body": [{"id": "zip", "active": True}]}
        )
        assert body == [{"id": "zip", "active": True}]

    def test_an_argument_the_endpoint_does_not_declare_is_dropped(self, routes):
        """The middleware removes these before a call reaches a tool; the tool must not invent a body field."""
        _, _, body = spec_tools.split_arguments(routes["getDenylist"], {"profile_id": "abc123", "surprise": "x"})
        assert body is None


class TestToolCall:
    @pytest.fixture
    def calls(self):
        return []

    def _tool(self, monkeypatch, calls, operation_id: str, payload: dict | None = None):
        monkeypatch.setattr(spec_tools, "api_request_payload", _fake_api(payload or {"data": []}, calls))
        return spec_tools.SpecApiTool({route.operation_id: route for route in spec_tools.spec_routes()}[operation_id])

    async def test_a_read_sends_the_endpoint_it_declares(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getAnalyticsDomains")
        result = await tool.run({"profile_id": "abc123", "from": "1700000000"})
        assert calls == [
            {
                "method": "GET",
                "url": "/profiles/abc123/analytics/domains",
                "params": {"from": "1700000000"},
                "body": None,
            }
        ]
        assert result.structured_content == {"data": []}

    async def test_a_post_sends_the_body_it_declares(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "addToDenylist")
        await tool.run({"profile_id": "abc123", "id": "example.com"})
        assert calls[0]["method"] == "POST"
        assert calls[0]["body"] == {"id": "example.com"}

    async def test_an_unfilled_path_argument_names_the_endpoint_and_the_id(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        result = await tool.run({"profile_id": ""})
        payload = result.structured_content
        assert result.is_error, "a missing required argument is an error, not a result"
        assert payload["code"] == "missing_required_argument"
        assert "profile_id" in payload["error"]
        assert "GET /profiles/{profile_id}/denylist" in payload["error"]
        assert "listProfiles()" in payload["hint"]
        assert calls == []

    @pytest.mark.parametrize(
        "entry_id",
        ["../x", "a/../../b", "id?admin=1", "id#fragment", "id name", "id\x01name", "foo\\bar"],
    )
    async def test_an_entry_id_that_could_address_another_endpoint_is_refused(self, monkeypatch, calls, entry_id):
        """An id is one path segment, and the guard refuses anything else.

        The value is substituted into the request path and the access-control layer
        authorizes the request by reading the profile id back out of that path, so an
        unvetted value could move the call to an endpoint the caller was not
        authorized for. Nothing upstream can be assumed to reject it.
        """
        tool = self._tool(monkeypatch, calls, "deleteRewrite")
        result = await tool.run({"profile_id": "abc123", "entry_id": entry_id})
        payload = result.structured_content
        assert result.is_error, "an unsafe path value is an error, not a result"
        assert payload["code"] == "invalid_entry_id"
        assert "must not contain a slash" in payload["error"]
        assert calls == []

    @pytest.mark.parametrize("profile_id", ["abc/def", "..", "ab?c", "ab c"])
    async def test_a_profile_id_that_could_address_another_endpoint_is_refused(self, monkeypatch, calls, profile_id):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        result = await tool.run({"profile_id": profile_id})
        payload = result.structured_content
        assert result.is_error, "an unsafe path value is an error, not a result"
        assert payload["code"] == "invalid_profile_id"
        assert "listProfiles()" in payload["hint"]
        assert calls == []

    async def test_a_query_value_is_never_checked_as_a_path_value(self, monkeypatch, calls):
        """The guard is about the request path, not about every argument.

        A log search term legitimately contains spaces and symbols; refusing those
        would break the endpoint rather than protect it.
        """
        tool = self._tool(monkeypatch, calls, "getLogs")
        await tool.run({"profile_id": "abc123", "search": "a b?c#d", "limit": 100})
        assert calls[0]["params"] == {"search": "a b?c#d", "limit": 100}

    async def test_a_missing_entry_argument_says_where_entry_ids_come_from(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "removeFromDenylist")
        result = await tool.run({"profile_id": "abc123"})
        payload = result.structured_content
        assert result.is_error
        assert "entry id" in payload["hint"]

    async def test_an_unnamed_missing_argument_lists_what_the_endpoint_needs(self, monkeypatch, calls):
        route = SimpleNamespace(
            operation_id="weirdEndpoint",
            method="GET",
            path="/weird/{thing_id}",
            summary="Weird",
            description="",
            flat_param_schema={"properties": {}, "required": ["thing_id"]},
            parameter_map={},
        )

        result = await spec_tools.SpecApiTool(route).run({})
        payload = result.structured_content
        assert result.is_error
        assert payload["error"].startswith("weirdEndpoint needs thing_id")
        assert payload["hint"] == "required by this endpoint: thing_id"

    async def test_the_download_endpoint_uses_the_guarded_downloader(self, monkeypatch, calls):
        from nextdns_mcp.tools import logs as logs_module

        async def fake_download(profile_id):
            calls.append({"profile_id": profile_id})
            return {"path": "/tmp/x.csv", "bytes": 10}

        monkeypatch.setattr(logs_module, "_download_logs_to_tempfile", fake_download)
        tool = self._tool(monkeypatch, calls, "downloadLogs")
        result = await tool.run({"profile_id": "abc123"})
        assert result.structured_content == {"path": "/tmp/x.csv", "bytes": 10}
        assert result.is_error is False, "a successful download payload is a result, not an error"
        assert calls == [{"profile_id": "abc123"}]

    async def test_the_download_endpoint_still_needs_a_profile(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "downloadLogs")
        result = await tool.run({"profile_id": ""})
        payload = result.structured_content
        assert result.is_error
        assert payload["code"] == "missing_required_argument"
        assert calls == []

    def test_repr_names_the_endpoint(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        assert repr(tool) == "SpecApiTool(name='getDenylist', method=GET, path=/profiles/{profile_id}/denylist)"


class TestTypedErrorClassification:
    """``is_error_payload`` is the one copy of the typed-error predicate (issue #551).

    ``spec_tools`` used to keep its own looser predicate (``"error" in payload or
    "code" in payload``), which flagged malformed or partial 2xx bodies - a bare
    ``{"error": ...}`` or ``{"code": ...}`` - as tool errors. The module now defers
    to the transport's predicate: only a payload carrying ``code`` AND ``error``
    AND no ``data`` key is a typed failure. These pin the boundary through the
    tool surface, not just the predicate in isolation, and that no second copy
    of the predicate exists to drift from it.
    """

    @pytest.fixture
    def calls(self):
        return []

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"error": "an answer about a domain named error"}, id="error-key-without-code"),
            pytest.param({"code": "a free-text status a 2xx body happened to carry"}, id="code-key-without-error"),
            pytest.param({"code": "partial", "error": "truncated", "data": []}, id="code-and-error-but-also-data"),
        ],
    )
    async def test_a_malformed_or_partial_payload_is_a_result_not_an_error(self, monkeypatch, calls, payload: Any):
        monkeypatch.setattr(spec_tools, "api_request_payload", _fake_api(payload, calls))
        tool = spec_tools.SpecApiTool(
            {route.operation_id: route for route in spec_tools.spec_routes()}["getAnalyticsDomains"]
        )
        result = await tool.run({"profile_id": "abc123"})
        assert result.is_error is False
        assert result.structured_content == payload

    async def test_a_typed_failure_is_still_flagged_through_the_tool(self, monkeypatch, calls):
        """The consolidation must not weaken the flag for real error payloads."""
        monkeypatch.setattr(
            spec_tools,
            "api_request_payload",
            _fake_api({"code": "http_error", "error": "upstream unavailable", "status_code": 503}, calls),
        )
        tool = spec_tools.SpecApiTool(
            {route.operation_id: route for route in spec_tools.spec_routes()}["getAnalyticsDomains"]
        )
        result = await tool.run({"profile_id": "abc123"})
        assert result.is_error is True
        assert result.structured_content["code"] == "http_error"

    def test_spec_tools_defers_to_the_transport_predicate(self):
        """The module keeps no copy of its own: classification routes through transport."""
        assert not hasattr(spec_tools, "_payload_is_error")
        assert spec_tools.is_error_payload is transport.is_error_payload


class TestRegistration:
    def test_registering_derives_the_call_log_endpoints_from_the_routes(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        routes = spec_tools.spec_routes()
        names = spec_tools.register_spec_tools(FastMCP("probe"), routes)
        endpoints = spec_tools.endpoints_for_routes(routes)
        assert endpoints["addToDenylist"] == ("POST", "/profiles/{profile_id}/denylist")
        assert endpoints["getAnalyticsDevicesSeries"] == (
            "GET",
            "/profiles/{profile_id}/analytics/devices;series",
        )
        assert names == spec_tools.spec_tool_names()

    async def test_the_record_carries_the_endpoint_and_a_method_derived_write(self, tmp_path):
        from nextdns_mcp.call_log import CallLogMiddleware

        path = tmp_path / "calls.jsonl"
        middleware = CallLogMiddleware(
            path=str(path),
            endpoints={
                "addRewrite": ("POST", "/profiles/{profile_id}/rewrites"),
                "listRewrites": ("GET", "/profiles/{profile_id}/rewrites"),
            },
        )
        await middleware._record("addRewrite", {"profile_id": "abc123"}, ok=True, error="")
        await middleware._record("listRewrites", {"profile_id": "abc123"}, ok=True, error="")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["endpoint"] == "POST /profiles/{profile_id}/rewrites"
        assert rows[0]["write"] is True
        assert rows[1]["endpoint"] == "GET /profiles/{profile_id}/rewrites"
        assert rows[1]["write"] is False

    async def test_the_surface_publishes_one_tool_per_operation(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            names = [tool.name for tool in await client.list_tools()]
        assert len(names) == SPEC_OPERATIONS - len(WITHHELD) + len(COMPOSITES)
        assert "addToDenylist" in names
        assert "manageLists" not in names

    async def test_the_composite_operations_are_published_here(self, monkeypatch):
        """DoH resolution and charting have no endpoint, so the spec cannot name them.

        They are registered by hand beside the generated tools; without that, a
        caller of this surface has no way to find out the server can do either.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(COMPOSITES) <= set(tools)
        # The whole docstring, not FastMCP's first-section excerpt: what a caller
        # needs here is the accepted interval range and the worked example.
        assert "interval`` must be between 60 and 86400 seconds" in tools["plotAnalytics"].description

    async def test_a_composite_is_found_by_search(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            found = await client.call_tool("search_tools", {"query": "DNS-over-HTTPS lookup"}, raise_on_error=False)
            hits = [hit["name"] for hit in (found.structured_content or {})["result"]]
            charted = await client.call_tool(
                "search_tools", {"query": "render a chart of analytics over time"}, raise_on_error=False
            )
            chart_hits = [hit["name"] for hit in (charted.structured_content or {})["result"]]
        assert hits[0] == "dohLookup"
        assert chart_hits[0] in ("plotAnalytics", "overviewDashboard")

    async def test_loose_argument_types_are_coerced_before_the_call(self, monkeypatch):
        """A client that sends "20" for an integer parameter means 20.

        The coercion is the server middleware's, not the generated tool's, so it
        has to be proven through a real call rather than against the tool alone.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(spec_tools, "api_request_payload", _fake_api({"data": []}, calls))
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            await client.call_tool(
                "getLogs", {"profile_id": "abc123", "limit": "20", "raw": "true"}, raise_on_error=False
            )
        assert calls[0]["params"] == {"limit": 20, "raw": True}

    async def test_disclosure_and_call_tool_work_on_generated_tools(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

        async def fake_payload(method, url, params=None, json_body=None):
            return {"data": [{"id": "zip"}]}

        monkeypatch.setattr(spec_tools, "api_request_payload", fake_payload)
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed] == ["search_tools", "call_tool"]
            found = await client.call_tool(
                "search_tools", {"query": "add a domain to the denylist"}, raise_on_error=False
            )
            hits = [hit["name"] for hit in (found.structured_content or {})["result"]]
            assert "addToDenylist" in hits
            called = await client.call_tool(
                "call_tool", {"name": "addToDenylist", "arguments": {"profile_id": "abc123", "id": "example.com"}}
            )
            assert called.structured_content == {"data": [{"id": "zip"}]}
