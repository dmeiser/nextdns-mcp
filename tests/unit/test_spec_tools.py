"""Unit tests for the spec-generated tool surface (one MCP tool per API operation).

The OpenAPI document is the contract, so these tests are mostly about what a
caller is told and what the server then sends: the generated catalog, the
endpoint registry the call log reads, and the transport that keeps the access
control layer and the typed error payloads in the path.

SPDX-License-Identifier: MIT
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import spec_tools

# Operations in nextdns-openapi.yaml, the one that is not offered, and the two the
# server performs itself.
SPEC_OPERATIONS = 79
WITHHELD = ("streamLogs",)
COMPOSITES = ("plotAnalytics", "dohLookup")


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
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api(payload or {"data": []}, calls))
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
        payload = (await tool.run({"profile_id": ""})).structured_content
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
        payload = (await tool.run({"profile_id": "abc123", "entry_id": entry_id})).structured_content
        assert payload["code"] == "invalid_entry_id"
        assert "must not contain a slash" in payload["error"]
        assert calls == []

    @pytest.mark.parametrize("profile_id", ["abc/def", "..", "ab?c", "ab c"])
    async def test_a_profile_id_that_could_address_another_endpoint_is_refused(self, monkeypatch, calls, profile_id):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        payload = (await tool.run({"profile_id": profile_id})).structured_content
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
        payload = (await tool.run({"profile_id": "abc123"})).structured_content
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

        payload = (await spec_tools.SpecApiTool(route).run({})).structured_content
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
        assert calls == [{"profile_id": "abc123"}]

    async def test_the_download_endpoint_still_needs_a_profile(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "downloadLogs")
        payload = (await tool.run({"profile_id": ""})).structured_content
        assert payload["code"] == "missing_required_argument"
        assert calls == []

    def test_repr_names_the_endpoint(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "getDenylist")
        assert repr(tool) == "SpecApiTool(name='getDenylist', method=GET, path=/profiles/{profile_id}/denylist)"


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
            charted = await client.call_tool("search_tools", {"query": "render a chart of analytics over time"}, raise_on_error=False)
            chart_hits = [hit["name"] for hit in (charted.structured_content or {})["result"]]
        assert hits[0] == "dohLookup"
        assert chart_hits[0] == "plotAnalytics"

    async def test_loose_argument_types_are_coerced_before_the_call(self, monkeypatch):
        """A client that sends "20" for an integer parameter means 20.

        The coercion is the server middleware's, not the generated tool's, so it
        has to be proven through a real call rather than against the tool alone.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            await client.call_tool("getLogs", {"profile_id": "abc123", "limit": "20", "raw": "true"}, raise_on_error=False)
        assert calls[0]["params"] == {"limit": 20, "raw": True}

    async def test_disclosure_and_call_tool_work_on_generated_tools(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

        async def fake_payload(method, url, params=None, json_body=None):
            return {"data": [{"id": "zip"}]}

        monkeypatch.setattr(spec_tools, "_api_request_payload", fake_payload)
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed] == ["search_tools", "call_tool"]
            found = await client.call_tool("search_tools", {"query": "add a domain to the denylist"}, raise_on_error=False)
            hits = [hit["name"] for hit in (found.structured_content or {})["result"]]
            assert "addToDenylist" in hits
            called = await client.call_tool(
                "call_tool", {"name": "addToDenylist", "arguments": {"profile_id": "abc123", "id": "example.com"}}
            )
            assert called.structured_content == {"data": [{"id": "zip"}]}


class TestRefusalsBeforeTransport:
    """Nothing malformed reaches the API: every guard answers before dispatch.

    The published ``inputSchema`` is what docs/troubleshooting.md says the
    server enforces ("rejected without reaching the API"), path arguments are
    checked for anything that could address a different endpoint, and a series
    range beyond the documented point budget is refused where it is built.
    Each of these must answer as a typed payload with no upstream request at
    all — an argument refused locally is cheaper and clearer than the 400 the
    API would answer, and a path value refused here is the ACL layer never
    having to interpret a crafted URL.
    """

    @pytest.fixture
    def calls(self):
        return []

    def _tool(self, monkeypatch, calls, operation_id: str):
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        return spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}[operation_id])

    async def test_a_value_beyond_the_published_bound_is_refused_locally(self, monkeypatch, calls):
        """``LogsLimit`` caps at 1000; a larger limit never reaches the API."""
        tool = self._tool(monkeypatch, calls, "getLogs")
        payload = (await tool.run({"profile_id": "abc123", "limit": 99999})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "limit" in payload["error"]
        assert calls == [], "an argument the published schema rejects must not be sent upstream"

    async def test_a_nested_body_violation_is_refused_not_sent(self, monkeypatch, calls):
        """The whole argument tree is validated: a body item without its required id.

        ``replaceDenylist(body=[{"wrong": "x"}])`` is the exact call the
        re-review reached the transport with; the published schema demands
        ``id`` on every item, so the refusal - with the dotted path naming the
        offending entry - is what docs/troubleshooting.md promises.
        """
        tool = self._tool(monkeypatch, calls, "replaceDenylist")
        payload = (await tool.run({"profile_id": "abc123", "body": [{"wrong": "x"}]})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "body.0" in payload["error"], payload["error"]
        assert calls == []

    async def test_a_body_of_the_wrong_kind_is_refused_not_sent(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls, "replaceDenylist")
        payload = (await tool.run({"profile_id": "abc123", "body": "not-an-array"})).structured_content
        assert payload["code"] == "invalid_argument"
        assert calls == []

    async def test_a_schema_valid_nested_body_still_reaches_transport(self, monkeypatch, calls):
        """Full-tree validation gates on the published schema only - valid bodies pass."""
        tool = self._tool(monkeypatch, calls, "replaceDenylist")
        await tool.run({"profile_id": "abc123", "body": [{"id": "evil.example"}]})
        assert calls

    async def test_a_deep_setting_value_outside_the_published_enum_is_refused(self, monkeypatch, calls):
        """Nested enums bind too: Settings.logs.retention is an enum of seconds."""
        tool = self._tool(monkeypatch, calls, "updateSettings")
        payload = (await tool.run({"profile_id": "abc123", "logs": {"retention": 30}})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "logs.retention" in payload["error"], payload["error"]
        assert calls == []

    async def test_a_percent_in_a_path_value_is_refused_not_sent(self, monkeypatch, calls):
        """``%`` is refused outright: its meaning depends on what decodes it.
        ``%2e%2e`` is ``..`` to anything that percent-decodes, and a
        double-encoded variant is invisible to a single decode pass; refusing
        the character closes both, and no legal NextDNS id contains it.
        """
        tool = self._tool(monkeypatch, calls, "getDenylist")
        payload = (await tool.run({"profile_id": "ab%cd"})).structured_content
        assert payload["code"] == "invalid_profile_id"
        assert calls == []

    @pytest.mark.parametrize("value", ["{profile_id}", "a}b", "{"])
    async def test_a_brace_in_a_path_value_is_refused(self, monkeypatch, calls, value):
        """A brace is the shape of a path placeholder; an id may not carry one."""
        tool = self._tool(monkeypatch, calls, "getDenylist")
        payload = (await tool.run({"profile_id": value})).structured_content
        assert payload["code"] == "invalid_profile_id"
        assert calls == []

    async def test_an_impossible_profile_shape_is_refused_before_the_acl(self, monkeypatch, calls):
        """A printable-but-impossible id is an invalid id, not an unsafe URL.

        13 lowercase alphanumerics contain none of the refused characters, so
        without a shape guard this id used to be substituted into the path and
        refused by the access-control layer as an unclassifiable URL - a 403
        about the request when the honest answer names the id.
        """
        tool = self._tool(monkeypatch, calls, "getDenylist")
        payload = (await tool.run({"profile_id": "abcdefg123456"})).structured_content
        assert payload["code"] == "invalid_profile_id"
        assert "listProfiles()" in payload["hint"]
        assert calls == []

    async def test_a_null_path_value_is_missing_not_the_text_none(self, monkeypatch, calls):
        """An explicit null for a path parameter means "not filled in"."""
        tool = self._tool(monkeypatch, calls, "getDenylist")
        payload = (await tool.run({"profile_id": None})).structured_content
        assert payload["code"] == "missing_required_argument"
        assert "None" not in payload["error"]
        assert calls == []

    async def test_a_null_query_value_is_dropped_not_sent(self, monkeypatch, calls):
        """``?to=`` is a validation error naming an empty string; null means absent."""
        tool = self._tool(monkeypatch, calls, "getLogs")
        await tool.run({"profile_id": "abc123", "to": None})
        assert calls[0]["params"] is None or "to" not in calls[0]["params"]

    def test_path_rendering_is_one_pass_so_a_value_cannot_re_substitute(self):
        """A value is inserted, never rescanned.

        With sequential ``str.replace`` passes, a second argument holding the
        first argument's placeholder gets substituted on the later pass, so
        the path that leaves differs from the one the ACL authorized. One
        ``re.sub`` pass over the template makes that structurally impossible.
        """
        located = [
            ("profile_id", "profile_id", "path"),
            ("entry_id", "entry_id", "path"),
        ]
        rendered = spec_tools._rendered_path(
            "/profiles/{profile_id}/denylist/{entry_id}",
            located,
            {"profile_id": "abc123", "entry_id": "{profile_id}"},
        )
        # The inserted value is never rescanned: the second placeholder stays
        # exactly what the caller sent, instead of becoming the authorized
        # profile id a sequential-replace renderer would have substituted.
        assert rendered == "/profiles/abc123/denylist/{profile_id}"

    def test_many_schema_violations_are_named_then_counted(self):
        """Up to five violations are named; the rest are counted, not hidden."""
        from types import SimpleNamespace

        problems = [SimpleNamespace(absolute_path=[f"f{i}"], message=f"bad {i}") for i in range(7)]
        described = spec_tools._describe_schema_errors(problems[: spec_tools._SCHEMA_ERROR_LIMIT], 2)
        assert "f0: bad 0" in described
        assert "f4: bad 4" in described
        assert "f5" not in described.split("; and")[0]
        assert "and 2 more" in described

    def test_a_root_schema_violation_is_located_at_the_arguments(self):
        error = SimpleNamespace(absolute_path=[])
        assert spec_tools._schema_error_where(error) == "<arguments>"


class TestDelegationGuardsRunFirst:
    """``downloadLogs`` is delegated, but delegation runs after the shape guards.

    The guarded downloader builds ``/profiles/{profile_id}/logs/download`` from
    the id it is handed, so the checks that keep a generated path honest have
    to fire before the handoff — otherwise delegation is a door around them.
    """

    @pytest.fixture
    def calls(self):
        return []

    def _downloader(self, monkeypatch, calls):
        from nextdns_mcp.tools import logs as logs_module

        async def fake_download(profile_id):
            calls.append({"profile_id": profile_id})
            return {"ok": True}

        monkeypatch.setattr(logs_module, "_download_logs_to_tempfile", fake_download)
        return spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["downloadLogs"])

    async def test_an_unsafe_profile_id_never_reaches_the_downloader(self, monkeypatch, calls):
        tool = self._downloader(monkeypatch, calls)
        payload = (await tool.run({"profile_id": "a/b"})).structured_content
        assert payload["code"] == "invalid_profile_id"
        assert calls == [], "delegation must not run ahead of the unsafe-path guard"

    @pytest.mark.parametrize("bad", ["nope", "TOOLONG1PROFILE", "ab cd"])
    async def test_a_malformed_profile_id_is_refused_typed(self, monkeypatch, calls, bad):
        tool = self._downloader(monkeypatch, calls)
        payload = (await tool.run({"profile_id": bad})).structured_content
        assert payload["code"] == "invalid_profile_id"
        assert calls == []

    async def test_a_valid_id_reaches_the_guarded_downloader(self, monkeypatch, calls):
        tool = self._downloader(monkeypatch, calls)
        result = await tool.run({"profile_id": "abc123"})
        assert result.structured_content == {"ok": True}
        assert calls == [{"profile_id": "abc123"}]

    async def test_a_resolver_error_that_is_not_missing_is_forwarded_typed(self, monkeypatch, calls):
        """Whatever else the resolver answers is the caller's answer, verbatim.

        The shape guards above catch every malformed id a caller can send
        through the public surface, so this pins the forwarding contract
        itself: a resolver error other than \"missing\" (an access denial, for
        instance) must reach the caller as the typed payload rather than be
        swallowed into a downloader call with a None profile.
        """
        tool = self._downloader(monkeypatch, calls)
        denial = {"code": "access_denied", "error": "profile abcdef is not readable"}
        monkeypatch.setattr(spec_tools, "resolve_profile_id", lambda pid, **kwargs: (None, denial))
        payload = (await tool.run({"profile_id": "abcdef"})).structured_content
        assert payload == denial
        assert calls == [], "the delegation must not run when the resolver refused"


class TestSchemaErrorTextIsBounded:
    """An invalid value is named, not quoted back whole (and not kept whole)."""

    @pytest.fixture
    def calls(self):
        return []

    async def test_a_giant_invalid_value_is_described_within_a_cap(self, monkeypatch, calls):
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        tool = spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["getAnalyticsStatusSeries"])
        payload = (await tool.run({"profile_id": "abc123", "from": "-1d", "partials": "x" * 5000})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "x" * (spec_tools._SCHEMA_ERROR_MSG_CHARS + 1) not in payload["error"]
        assert len(payload["error"]) < 1200
        assert calls == []


class TestGeneratedSeriesBudget:
    """The documented 2000-point budget guards the generated door too.

    ``plotAnalytics`` refuses an over-budget range; the ``getAnalytics*Series``
    tools read the same endpoint with the same per-interval cost, so they get
    the same refusal. A call that names no interval takes the endpoint's own
    default, which this server did not choose and cannot bound.
    """

    @pytest.fixture
    def calls(self):
        return []

    def _tool(self, monkeypatch, calls):
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        return spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["getAnalyticsStatusSeries"])

    async def test_a_generated_series_range_beyond_the_budget_is_refused(self, monkeypatch, calls):
        """30 days at 60s intervals would answer ~43k points against a 2000 cap."""
        tool = self._tool(monkeypatch, calls)
        payload = (await tool.run({"profile_id": "abc123", "from": "-30d", "interval": 60})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "2000" in payload["error"]
        assert calls == []

    async def test_a_generated_series_without_an_interval_is_not_bounded_here(self, monkeypatch, calls):
        tool = self._tool(monkeypatch, calls)
        await tool.run({"profile_id": "abc123", "from": "-30d"})
        assert calls, "the request goes out; its interval is the endpoint's default"

    async def test_to_zero_is_a_window_end_not_an_omission(self, monkeypatch, calls):
        """``to=0`` is the epoch, not a missing value.

        ``query.get("to") or "now"`` treated the literal 0 as absent: an
        epoch-to-epoch window (zero span) was priced as now-minus-epoch -
        decades - and the call was refused for a budget it never spent.
        """
        tool = self._tool(monkeypatch, calls)
        result = await tool.run({"profile_id": "abc123", "from": 0, "to": 0, "interval": 60})
        payload = result.structured_content
        assert isinstance(payload, dict) and payload.get("code") == "invalid_argument"
        assert "2000" not in payload["error"], f"budget arithmetic treated to=0 as now: {payload['error']}"
        assert "end after" in payload["error"], "zero epoch span: range order is the honest refusal"

    async def test_a_wide_no_interval_range_is_bounded_at_the_adaptive_default(self, monkeypatch, calls):
        """No interval is no longer a pass-through: the default has no self-cap.

        Probed live 2026-11: a 4000-day range with no interval answers at
        interval 86400 with 4000 points - over the 2000-point cap. The call
        is priced at the probed adaptive default (86400 above 7-day windows)
        and refused before any request goes out.
        """
        tool = self._tool(monkeypatch, calls)
        payload = (await tool.run({"profile_id": "abc123", "from": "-4000d"})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "2000" in payload["error"]
        assert calls == []

    async def test_a_no_interval_window_inside_the_default_is_allowed(self, monkeypatch, calls):
        """Pricing the default interval is a bound, not a ban: -365d answers
        365 points at 86400 (probed), inside every budget, and goes out."""
        tool = self._tool(monkeypatch, calls)
        await tool.run({"profile_id": "abc123", "from": "-365d"})
        assert calls

    async def test_an_oversized_series_answer_is_refused_not_dumped(self, monkeypatch, calls):
        """Response-side backstop: the row count upstream returns is not ours
        to price up front, so a payload over the byte ceiling comes back as a
        typed refusal naming the size, never as a context flood."""

        async def fat_api(*args, **kwargs):
            calls.append((args, kwargs))
            return {"data": [{"name": f"row{i}", "hits": ["x"] * 60} for i in range(2000)]}

        monkeypatch.setattr(spec_tools, "_api_request_payload", fat_api)
        tool = spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["getAnalyticsStatusSeries"])
        payload = (await tool.run({"profile_id": "abc123", "from": "-30d", "interval": 86400})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "series response ceiling" in payload["error"]
        assert payload["response_bytes"] > spec_tools.MAX_SERIES_RESPONSE_BYTES
        assert calls, "the guard bounds what is returned, after the bounded fetch"

    async def test_a_budgeted_series_answer_passes_the_ceiling(self, monkeypatch, calls):
        """A small, budgeted answer goes through untouched."""
        tool = self._tool(monkeypatch, calls)
        result = await tool.run({"profile_id": "abc123", "from": "-365d", "interval": 86400})
        assert result.structured_content == {"data": []}
        assert calls


class TestSeriesBudgetHelperEdges:
    """The generated-door budget helper, called directly on its defensive edges."""

    def _route(self):
        return next(r for r in spec_tools.spec_routes() if r.path.endswith("/analytics/status;series"))

    def test_a_call_naming_no_from_is_not_priced_here(self):
        """The required SeriesFrom makes this defensive; the helper still says so."""
        assert spec_tools._series_budget_for_generated_call(self._route(), {}) is None

    def test_an_unresolvable_range_without_an_interval_is_left_to_the_endpoint(self):
        """No interval means no local budget arithmetic; a garbage range is
        the endpoint's 400 to give, not this helper's guess."""
        query = {"from": "not-a-time", "to": "also-not-a-time"}
        assert spec_tools._series_budget_for_generated_call(self._route(), query) is None


class TestSeriesStreamCeilingWiring:
    """The byte ceiling travels into the transport, where streaming enforces it."""

    @pytest.fixture
    def calls(self):
        return []

    def _series_tool(self, monkeypatch, calls):
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        return spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["getAnalyticsStatusSeries"])

    async def test_a_generated_series_call_passes_the_cap_to_the_transport(self, monkeypatch, calls):
        tool = self._series_tool(monkeypatch, calls)
        await tool.run({"profile_id": "abc123", "from": "-1d"})
        assert calls[0]["max_response_bytes"] == spec_tools.MAX_SERIES_RESPONSE_BYTES

    async def test_a_non_series_call_carries_no_cap(self, monkeypatch, calls):
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        tool = spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["getDenylist"])
        await tool.run({"profile_id": "abc123"})
        assert "max_response_bytes" not in calls[0]

    async def test_a_transport_ceiling_refusal_becomes_the_series_ceiling_payload(self, monkeypatch, calls):
        async def capped_transport(*args, **kwargs):
            return {"code": "response_too_large", "error": "exceeded the ceiling", "max_response_bytes": 1}

        tool = self._series_tool(monkeypatch, calls)
        monkeypatch.setattr(spec_tools, "_api_request_payload", capped_transport)
        payload = (await tool.run({"profile_id": "abc123", "from": "-1d"})).structured_content
        assert payload["code"] == "invalid_argument"
        assert "series response ceiling" in payload["error"]
        assert payload["max_response_bytes"] == spec_tools.MAX_SERIES_RESPONSE_BYTES
        assert "response_bytes" not in payload, "a mid-stream refusal has no measured size to claim"


class TestSchemaProblemStreamIsBounded:
    """Violations are streamed: retention and counting stay bounded, coverage does not."""

    @pytest.fixture
    def calls(self):
        return []

    def test_retention_stops_at_the_rendered_few_and_counting_at_the_cap(self):
        import jsonschema as js

        validator = js.Draft202012Validator({"type": "array", "items": {"required": ["id"]}})
        body = [{"wrong": i} for i in range(1500)]
        kept, extra = spec_tools._schema_problems(validator, body)
        assert len(kept) == spec_tools._SCHEMA_ERROR_LIMIT
        assert extra == spec_tools._SCHEMA_ERROR_COUNT_CAP  # counting stopped at the cap

    async def test_an_over_cap_violation_tail_says_at_least(self, monkeypatch, calls):
        monkeypatch.setattr(spec_tools, "_api_request_payload", _fake_api({"data": []}, calls))
        tool = spec_tools.SpecApiTool({r.operation_id: r for r in spec_tools.spec_routes()}["replaceDenylist"])
        body = [{"wrong": i} for i in range(spec_tools._SCHEMA_ERROR_COUNT_CAP + 100)]
        payload = (await tool.run({"profile_id": "abc123", "body": body})).structured_content
        assert payload["code"] == "invalid_argument"
        assert f"and at least {spec_tools._SCHEMA_ERROR_COUNT_CAP} more" in payload["error"]
        assert calls == []


def test_series_refusal_ignores_a_non_mapping_answer():
    """A transport that answers with a bare list gets no annotation, no crash."""
    route = next(r for r in spec_tools.spec_routes() if r.path.endswith("/analytics/status;series"))
    assert spec_tools._series_refusal(route, ["data", "rows"]) is None


class TestLegacyInjectedTransportCompatibility:
    """A transport injected before the response cap existed keeps the legacy signature.

    ``SpecApiTool(transport=...)`` is a published injection point; a callable
    with exactly ``(method, url, params=None, json_body=None)`` was valid
    until the capped ``;series`` call started passing ``max_response_bytes``.
    Binding that kwarg to a legacy signature raises TypeError before its body
    runs, so the capability must be probed - and the legacy path must still
    have its oversized answers refused by the serialized backstop.
    """

    async def test_a_legacy_four_argument_transport_runs_and_is_still_capped(self):
        seen: list[tuple[str, str]] = []

        async def legacy_transport(method, url, params=None, json_body=None):
            seen.append((method, url))
            return {"data": [{"x": "over-cap-padding-for-a-real-refusal", "i": i} for i in range(30_000)]}

        route = next(r for r in spec_tools.spec_routes() if r.operation_id == "getAnalyticsStatusSeries")
        tool = spec_tools.SpecApiTool(route, transport=legacy_transport)
        payload = (await tool.run({"profile_id": "abc123", "from": "-1d"})).structured_content

        assert seen, "the legacy transport must be called with the legacy signature, not killed by the cap kwarg"
        assert payload["code"] == "invalid_argument"
        assert "series response ceiling" in payload["error"]
        assert payload["response_bytes"] > spec_tools.MAX_SERIES_RESPONSE_BYTES  # the backstop priced it

    def test_the_probe_reads_named_and_variadic_signatures(self):
        async def legacy(method, url, params=None, json_body=None): ...

        async def modern(method, url, params=None, json_body=None, max_response_bytes=None): ...

        async def star(method, url, **extra): ...

        assert not spec_tools._transport_accepts_cap(legacy)
        assert spec_tools._transport_accepts_cap(modern)
        assert spec_tools._transport_accepts_cap(star)

    def test_the_default_transport_supports_the_cap(self):
        assert spec_tools._transport_accepts_cap(spec_tools._api_request_payload)

    def test_a_callable_without_an_introspectable_signature_is_treated_as_legacy(self):
        """The unpriced path still enforces the ceiling, so the guess is safe the only way."""
        assert not spec_tools._transport_accepts_cap(iter)  # builtin whose signature() raises ValueError


def test_a_big_non_series_payload_is_not_refused_by_the_series_backstop():
    """The ceiling is a ;series policy: a wide aggregate answer is data, not an overrun."""
    route = next(r for r in spec_tools.spec_routes() if r.operation_id == "getDenylist")
    big = {"data": [{"id": f"entry-{i}", "name": "x" * 40} for i in range(30_000)]}
    assert spec_tools._series_refusal(route, big) is None
