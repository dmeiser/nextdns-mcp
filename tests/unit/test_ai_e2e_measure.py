"""Unit tests for the E2E harness's REST-verifying measurements.

These tests exercise the measurement helpers against a fake client — no
network, no live MCP server. The rest of the harness suite lives in
``test_ai_e2e_harness.py``, ``test_ai_e2e_safety.py``,
``test_ai_e2e_steps.py``, and ``test_ai_e2e_run.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import os
from pathlib import Path
from typing import ClassVar

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ai_e2e_harness.py"


@pytest.fixture(autouse=True)
def _stub_probe_httpx(monkeypatch):
    """The profile-protection probe's HTTP answer is faked for the whole module.

    The probe fails closed, so an unstubbed probe would abort every config
    load in tests that set NEXTDNS_API_KEY (and would hit the real network).
    Tests that exercise probe behaviour install their own fake, which
    overrides this one.
    """

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())


# =========================================================================== #
# REST-verifying measurements (fake client, no network)
# =========================================================================== #


class _FakeClient:
    """A stand-in for MeasureClient that serves canned REST responses."""

    def __init__(self, profiles=None, by_path=None, delete_ok=True, post_result=None):
        self._profiles = profiles or []
        self._by_path = by_path or {}
        self._delete_ok = delete_ok
        self._post_result = post_result
        self.deleted: list[str] = []
        self.got: list[tuple[str, dict | None]] = []
        self.posted: list[tuple[str, dict]] = []

    async def rest_get(self, path, params=None):
        self.got.append((path, params))
        if path in self._by_path:
            return self._by_path[path]
        if path == "/profiles":
            # The listing answer: `_readable_rows` demands a `data` list, and
            # absence verdicts are only proven from a readable listing.
            return {"status": 200, "ok": True, "body": {"data": list(self._profiles)}}
        return {"status": 200, "ok": True, "body": {}}

    async def rest_post(self, path, json_body=None):
        self.posted.append((path, json_body or {}))
        if self._post_result is not None:
            return self._post_result
        return {"status": 200, "ok": True, "body": {"data": {"id": "pid-new"}}}

    async def rest_delete(self, path):
        self.deleted.append(path)
        if self._delete_ok:
            return {"status": 200, "ok": True, "body": {}}
        return {"status": 500, "ok": False, "body": "boom"}

    async def find_profile(self, name):
        for p in self._profiles:
            if p.get("name") == name:
                return p
        return None


class TestMeasureState:
    def test_profile_gone_logic(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")

        # gone when the id 404s and the listing resolves neither name
        m = _FakeClient(profiles=[], by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}})
        assert asyncio.run(h._profile_gone(m, "pid1", t)) == (True, h.ABSENT)
        # present under the id
        m2 = _FakeClient(by_path={"/profiles/pid1": {"ok": True, "body": {"data": {"id": "pid1"}}}})
        assert asyncio.run(h._profile_gone(m2, "pid1", t)) == (False, h.PRESENT)
        # present under the original name
        m3 = _FakeClient(profiles=[{"id": "pid1", "name": "P"}])
        assert asyncio.run(h._profile_gone(m3, "pid1", t)) == (False, h.PRESENT)
        # present under the renamed name
        m4 = _FakeClient(profiles=[{"id": "pid1", "name": "P-renamed"}])
        assert asyncio.run(h._profile_gone(m4, "pid1", t)) == (False, h.PRESENT)
        # present by name even when the id itself 404s
        m5 = _FakeClient(
            profiles=[{"id": "pid1", "name": "P-renamed"}],
            by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}},
        )
        assert asyncio.run(h._profile_gone(m5, "pid1", t)) == (False, h.PRESENT)

    def test_profile_gone_is_indeterminate_on_failed_reads(self, h):
        """A 503 is not a deletion: a failed read used to read as 'gone'."""
        t = h.TaskTargets(profile_name="P", tag="abc")

        m = _FakeClient(profiles=[], by_path={"/profiles/pid1": {"status": 503, "ok": False, "body": "boom"}})
        gone, verdict = asyncio.run(h._profile_gone(m, "pid1", t))
        assert gone is False
        assert verdict.startswith(h.INDETERMINATE)

        # Even a 404 id cannot prove absence when the name listing is broken.
        broken_listing = {"status": 500, "ok": False, "body": "boom"}
        m2 = _FakeClient(
            profiles=[],
            by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}, "/profiles": broken_listing},
        )
        gone2, verdict2 = asyncio.run(h._profile_gone(m2, "pid1", t))
        assert gone2 is False
        assert verdict2.startswith(h.INDETERMINATE)

    def test_profile_gone_checks_every_page_of_the_listing(self, h):
        """A by-id 404 plus the renamed profile on page 2 is PRESENT, not ABSENT.

        The listing half of the check used to read only the first page, so a
        renamed test profile living on page 2 confirmed a deletion that never
        happened.
        """

        class _Paged:
            async def rest_get(self, path, params=None):
                if path == "/profiles/pid1":
                    return {"status": 404, "ok": False, "body": {}}
                if (params or {}).get("cursor") == "c2":
                    return {"status": 200, "ok": True, "body": {"data": [{"id": "pid1", "name": "P-renamed"}]}}
                return {"status": 200, "ok": True, "body": {"data": [{"id": "a1", "name": "Other"}], "cursor": "c2"}}

        t = h.TaskTargets(profile_name="P", tag="abc")
        assert asyncio.run(h._profile_gone(_Paged(), "pid1", t)) == (False, h.PRESENT)

    def test_profile_gone_is_indeterminate_on_a_malformed_cursor(self, h):
        """The malformed-cursor raise must degrade to a verdict, not a crash."""

        class _MalformedListing:
            async def rest_get(self, path, params=None):
                if path == "/profiles/pid1":
                    return {"status": 404, "ok": False, "body": {}}
                return {"status": 200, "ok": True, "body": {"data": [], "cursor": 0}}

        t = h.TaskTargets(profile_name="P", tag="abc")
        gone, verdict = asyncio.run(h._profile_gone(_MalformedListing(), "pid1", t))
        assert gone is False
        assert verdict.startswith(h.INDETERMINATE)

    def test_find_profile_paginates_the_listing(self, h):
        """A one-page lookup missed a profile that lived on page 2."""

        m = h.MeasureClient("k")

        async def _paged(path, params=None):
            assert path == "/profiles"
            if (params or {}).get("cursor") == "c2":
                return {"status": 200, "ok": True, "body": {"data": [{"id": "b2", "name": "Two"}]}}
            return {"status": 200, "ok": True, "body": {"data": [{"id": "a1", "name": "One"}], "cursor": "c2"}}

        m.rest_get = _paged
        assert asyncio.run(m.find_profile("Two")) == {"id": "b2", "name": "Two"}
        assert asyncio.run(m.find_profile("Missing")) is None

    def test_self_heal_noop_when_profile_alive(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")

        m = _FakeClient(by_path={"/profiles/pid1": {"ok": True, "body": {"data": {"id": "pid1"}}}})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid1"
        assert m.posted == []  # alive -> no POST

    def test_self_heal_recreates_deleted_profile(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")

        # id 404s (deleted mid-run); POST recreates it and returns a new id.
        m = _FakeClient(by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid-new"
        assert m.posted == [("/profiles", {"name": "P-renamed"})]  # renamed name

    def test_self_heal_refuses_adopt_by_name_when_create_answered_no_id(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")

        # POST ok but body carried no id: there is no proof of what it made,
        # so self-heal fails. A profile answering the run's name must never
        # be adopted and later deleted on a name lookup's say-so.
        m = _FakeClient(
            by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}},
            profiles=[{"id": "pid-lookup", "name": "P-renamed"}],
            post_result={"status": 201, "ok": True, "body": {}},
        )
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == ""

    def test_self_heal_returns_empty_when_recreation_fails(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")

        m = _FakeClient(
            by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}},
            post_result={"status": 500, "ok": False, "body": "boom"},
        )
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == ""


class TestClientTransientsAreNotModelProse:
    """A model that says a tool is missing is not the client saying it."""

    WORDS = 'No tool named "nextdns_search_tools" is currently available.'

    def test_the_words_in_the_model_narration_are_not_a_transient(self, h):
        """nemotron-3-nano-omni made no calls at all and wrote this as prose.

        It was labelled "MCP tool not registered yet" and retried three times as
        infrastructure; the event stream was step_start/text/step_finish with no
        tool_use event, so the step measured a refusal, not a registration race.
        """
        run = h.ActorRun(command=["x"], final_text=self.WORDS)
        blob = self.WORDS.lower()  # the model's narration, and nothing else
        assert h._transient_actor_failure(run, blob) == ("", "")

    def test_the_same_words_from_a_failed_client_call_are_still_a_transient(self, h):
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="nextdns_search_tools", args={}, is_error=True, result_text=self.WORDS)]
        assert h._transient_actor_failure(run, self.WORDS.lower()) == (
            "unknown-tool",
            "MCP tool not registered yet",
        )

    def test_the_code_mode_path_is_still_detected_from_the_client_error(self, h):
        """Regression for the 25 live attempts that were real registration misses."""
        words = 'No tool named "tools.nextdns.search_tools" is currently available.'
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="execute", args={}, is_error=True, result_text=words)]
        assert h._transient_actor_failure(run, words.lower()) == (
            "wrong-tool-path",
            "actor called the code-mode tool name directly",
        )

    def test_the_harness_error_string_counts_as_client_reported(self, h):
        run = h.ActorRun(command=["x"], error=self.WORDS)
        assert self.WORDS.lower() in h._client_reported_errors(run)

    def test_a_resolution_error_recorded_as_a_successful_call_still_counts(self, h):
        """opencode answers a bad tool name with a *completed* call whose output
        is the error text — observed live as
        "Error calling tool 'call_tool': Unknown tool: 'delete_dns_rewrite_record'"
        recorded with ok=True. Filtering on the ok flag loses the real cases.
        """
        words = "Error calling tool 'call_tool': Unknown tool: 'delete_dns_rewrite_record'"
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="call_tool", args={}, result_text=words)]
        assert h._transient_actor_failure(run, words.lower()) == (
            "unknown-tool",
            "MCP tool not registered yet",
        )


class TestStepEndpointExpectations:
    """Each step's expectation is the endpoint of the tool that performs it.

    Steps are authored by tool name; the endpoint string is resolved from the
    shipped OpenAPI document, so the checklist breaks at build time when the
    document renames or drops an operation instead of silently matching no
    call in a live run.
    """

    def _steps(self, h) -> dict:
        return {step.id: step for step in h.build_steps(h.TaskTargets(profile_name="PN", tag="tg"))}

    def _operations(self, h) -> dict:
        from nextdns_mcp import spec_tools

        return {
            route.operation_id: f"{route.method} {route.path}"
            for route in spec_tools.spec_routes(spec_tools.load_spec())
            if route.operation_id
        }

    def test_every_endpoint_step_expects_a_real_operation(self, h):
        operations = set(self._operations(h).values())
        expected = [e["endpoints"][0] for step in self._steps(h).values() if (e := step.expect).get("endpoints")]
        assert len(expected) == 79, "the doh and plot steps expect local records, not endpoints"
        assert not [ep for ep in expected if ep not in operations]

    def test_the_step_tool_and_its_expected_endpoint_agree(self, h):
        """The step's label tool is exactly the one bound to the endpoint it expects."""
        operations = self._operations(h)
        for step in self._steps(h).values():
            expect = step.expect
            if expect.get("endpoints"):
                assert operations.get(step.tool) in expect["endpoints"], step.id
            else:
                assert expect["tool"] == step.tool, step.id

    def test_the_local_operation_steps_expect_their_own_record(self, h):
        """Chart making and the DoH lookup are what the server does itself.

        They have no endpoint to match, so their pass rule is the server's own
        tool record plus the payload it answered with - which is what makes
        them measured steps rather than skipped ones.
        """
        steps = self._steps(h)
        doh = steps["doh.lookup"].expect
        assert "endpoints" not in doh and doh["tool"] == "dohLookup"
        assert doh["result"] == ["example.com"]
        plot = steps["plots.status"].expect
        assert "endpoints" not in plot and plot["tool"] == "plotAnalytics"
        assert plot["result"] == ["AreaChart"]

    def test_an_operation_renamed_in_the_document_breaks_the_checklist_at_build_time(self, h, monkeypatch):
        """The alternative is a live run where one step silently matches nothing."""
        renamed = {**h._LIST_TOOLS["denylist"], "add": "insertDenylistEntry"}
        monkeypatch.setitem(h._LIST_TOOLS, "denylist", renamed)
        with pytest.raises(KeyError, match="insertDenylistEntry"):
            h.build_steps(h.TaskTargets(profile_name="PN", tag="tg"))

    def test_the_category_word_is_carried_by_the_endpoint_not_the_arguments(self, h):
        expect = self._steps(h)["lists.denylist.add"].expect
        assert expect["endpoints"] == ["POST /profiles/{profile_id}/denylist"]
        assert expect["args"] == ["ai-e2e-denylist-add-tg.example.com"]

    def test_a_parental_entry_is_addressed_by_its_id_parameter(self, h):
        """The spec spells that path parameter `id`, not `entry_id`."""
        steps = self._steps(h)
        assert steps["lists.parental_services.remove"].expect["endpoints"] == [
            "DELETE /profiles/{profile_id}/parentalControl/services/{id}"
        ]
        assert steps["lists.allowlist.remove"].expect["endpoints"] == [
            "DELETE /profiles/{profile_id}/allowlist/{entry_id}"
        ]

    def test_a_series_step_expects_the_series_endpoint(self, h):
        steps = self._steps(h)
        assert steps["analytics.series.devices"].expect["endpoints"] == [
            "GET /profiles/{profile_id}/analytics/devices;series"
        ]
        assert steps["analytics.aggregate.devices"].expect["endpoints"] == [
            "GET /profiles/{profile_id}/analytics/devices"
        ]

    def test_the_settings_categories_are_seven_distinct_endpoint_pairs(self, h):
        """Each category is its own endpoint pair; `logs` is not the logs endpoint.

        Re-home check for the surface switch: the web3 toggle lives on
        /settings itself, so the general update step is the updateSettings
        endpoint with web3 pinned in the arguments.
        """
        steps = self._steps(h)
        reads = {steps[f"settings.{cat}.read"].expect["endpoints"][0] for cat in h.SETTINGS_CATEGORIES}
        writes = {steps[f"settings.{cat}.update"].expect["endpoints"][0] for cat in h.SETTINGS_CATEGORIES}
        assert len(reads) == 7 and len(writes) == 7
        assert "GET /profiles/{profile_id}/settings/logs" in reads, "settings.logs, not the logs endpoint"
        general = steps["settings.general.update"].expect
        assert general["endpoints"] == ["PATCH /profiles/{profile_id}/settings"]
        assert general["args"] == ["web3"]

    def test_download_expects_the_download_endpoint(self, h):
        expect = self._steps(h)["logs.download"].expect
        assert expect["endpoints"] == ["GET /profiles/{profile_id}/logs/download"]
        assert expect["result"] == ["file_path"]

    def test_a_step_with_no_expectation_still_passes(self, h):
        assert h._expected_call_ok([], {}, "abc123") == (True, "")

    def test_the_endpoint_record_satisfies_the_step(self, h):
        records = [
            {
                "tool": "addToDenylist",
                "endpoint": "POST /profiles/{profile_id}/denylist",
                "profile_id": "abc123",
                "ok": True,
                "args": '{"id": "x.example.com"}',
                "result": '{"data": []}',
                "write": True,
            }
        ]
        expect = {"endpoints": ["POST /profiles/{profile_id}/denylist"], "args": ["x.example.com"], "data": True}
        ok, why = h._expected_call_ok(records, expect, "abc123")
        assert ok, why

    def test_a_different_endpoint_is_named_as_the_missing_call(self, h):
        records = [
            {
                "tool": "getDenylist",
                "endpoint": "GET /profiles/{profile_id}/denylist",
                "ok": True,
                "args": "{}",
                "result": '{"data": []}',
            }
        ]
        expect = {"endpoints": ["POST /profiles/{profile_id}/denylist"], "args": [], "data": True}
        ok, why = h._expected_call_ok(records, expect, "abc123")
        assert not ok
        assert why == "server recorded no POST /profiles/{profile_id}/denylist call in this attempt"

    def test_a_write_after_the_endpoint_call_undoes_the_step(self, h):
        records = [
            {
                "tool": "addToDenylist",
                "endpoint": "POST /profiles/{profile_id}/denylist",
                "profile_id": "abc123",
                "ok": True,
                "args": '{"id": "x"}',
                "result": '{"data": []}',
                "write": True,
            },
            {
                "tool": "removeFromDenylist",
                "endpoint": "DELETE /profiles/{profile_id}/denylist/{entry_id}",
                "profile_id": "abc123",
                "ok": True,
                "args": "{}",
                "result": '{"success": true}',
                "write": True,
            },
        ]
        expect = {"endpoints": ["POST /profiles/{profile_id}/denylist"], "args": ["x"], "data": True}
        ok, why = h._expected_call_ok(records, expect, "abc123")
        assert not ok
        assert "DELETE /profiles/{profile_id}/denylist/{entry_id}" in why

    def test_every_failed_call_to_the_endpoint_is_still_reported(self, h):
        records = [{"tool": "addToDenylist", "endpoint": "POST /profiles/{profile_id}/denylist", "ok": False}]
        expect = {"endpoints": ["POST /profiles/{profile_id}/denylist"], "args": [], "data": True}
        ok, why = h._expected_call_ok(records, expect, "abc123")
        assert not ok
        assert "every such call failed" in why

    def test_the_server_record_is_read_by_endpoint_when_it_has_one(self, h):
        assert h._call_label({"endpoint": "POST /profiles"}) == "POST /profiles"
        # A local operation has no endpoint; its tool name is the whole label.
        assert h._call_label({"tool": "plotAnalytics"}) == "plotAnalytics"

    def test_the_harness_profile_lifecycle_names_real_tools(self, h):
        """Otherwise a targeted run cannot provision or clean up its own profile.

        Observed on the first spec-surface run: the grouped-only cleanup call
        died with `Unknown tool: 'manageProfiles'` after the steps had all
        passed.
        """
        assert h._profile_lifecycle_call("create", name="PN") == ("createProfile", {"name": "PN"}, "createProfile")
        assert h._profile_lifecycle_call("delete", pid="abc123") == (
            "deleteProfile",
            {"profile_id": "abc123"},
            "deleteProfile",
        )


class TestTransientActorFailure:
    """Only infrastructure transients re-run a step; slow-but-working attempts do not."""

    def test_lazy_tool_registration_is_retried(self, h):
        run = h.ActorRun(command=["x"])
        run.tool_calls = [
            h.ToolCall(name="nextdns.foo", args={}, is_error=True, result_text="failed with unknown tool 'nextdns.foo'")
        ]
        assert h._transient_actor_failure(run, "failed with unknown tool 'nextdns.foo'") == (
            "unknown-tool",
            "MCP tool not registered yet",
        )

    def test_opencodes_other_registration_wording_is_also_a_transient(self, h):
        """Scored as a model failure until the wording was read from a live run.

        The name in the message is the one opencode registers for a direct MCP
        tool, `<server>_<tool>`: that call was aimed correctly and met a server
        that had not finished registering. The words have to come from a failed
        call — the same sentence in the model's own narration is a refusal, not
        a registration race (see TestClientTransientsAreNotModelProse).
        """
        run = h.ActorRun(command=["x"])
        blob = 'No tool named "nextdns_search_tools" is currently available.'
        run.tool_calls = [h.ToolCall(name="nextdns_search_tools", args={}, is_error=True, result_text=blob)]
        assert h._transient_actor_failure(run, blob.lower()) == (
            "unknown-tool",
            "MCP tool not registered yet",
        )

    def test_the_code_mode_name_called_directly_is_its_own_kind(self, h):
        """`tools.nextdns.search_tools` is a code-mode path, not a registered tool.

        25 attempts across two runs used it directly and none ever reached the
        server, so retrying it three times as a registration hiccup measured the
        same thing each time and labelled the loss as infrastructure.
        """
        run = h.ActorRun(command=["x"])
        blob = 'No tool named "tools.nextdns.search_tools" is currently available.'
        run.tool_calls = [h.ToolCall(name="execute", args={}, is_error=True, result_text=blob)]
        assert h._transient_actor_failure(run, blob.lower()) == (
            "wrong-tool-path",
            "actor called the code-mode tool name directly",
        )

    def test_that_loss_is_named_in_the_summary(self, h):
        """A step lost to it is not reported as an unexplained no-call."""
        row = {
            "mcp_tool": "getDenylist",
            "mcp_response": {
                "server_calls": [],
                "transients": ["wrong-tool-path: actor called the code-mode tool name directly"],
            },
        }
        modes = [mode for mode, _tool in h._failure_modes(row)]
        assert "actor called a tool path this client does not expose" in modes
        assert "no call reached the server" not in modes

    def test_a_provider_that_refuses_the_model_is_a_transient(self, h):
        """Reproduced with a bare `opencode run`: the provider, not the server.

        "Function id '...' version 'null': Specified function in account '...'
        is not found" - the attempt produced no tool call, so the step measured
        nothing and must not be scored as a model failure.
        """
        run = h.ActorRun(command=["x"])
        run.error = (
            "Function id '9fa6fd04-ba2c-4bb3-90b7-ede407a9290f' version 'null': "
            "Specified function in account 'X1UCRn4VumVsuKcJJB603mP5Yx09bRnJ67Idchb50ys' is not found"
        )
        blob = " ".join([run.error, run.final_text or ""]).lower()
        assert h._transient_actor_failure(run, blob) == (
            "provider-unavailable",
            "model provider is not serving this model",
        )
        assert h._provider_outage(run) is True

    def test_the_outage_reaches_the_harness_as_an_event_stream_error(self, h):
        """The message is an {"type":"error"} line on stdout, not stderr.

        The first version of the detector read only `error`/`stderr` and missed
        every step of a live outage run.
        """
        run = h.ActorRun(command=["x"], exit_code=1)
        run.raw_lines = [
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "message": "Function id '9fa6fd04': Specified function in account 'X1UCRn4V' is not found"
                    },
                }
            )
        ]
        run.error = "actor harness exited with code 1"
        blob = " ".join(h._extract_error_events(run)).lower()
        assert h._transient_actor_failure(run, blob) == (
            "provider-unavailable",
            "model provider is not serving this model",
        )
        assert h._provider_outage(run) is True

    def test_a_model_answer_is_never_read_as_an_outage(self, h):
        """The same words inside a tool result mean the server answered."""
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="execute", result_text="profile is not found")]
        assert h._provider_outage(run) is False
        assert h._transient_actor_failure(run, "profile is not found")[0] == ""

    def test_an_outage_step_is_named_in_the_summary(self, h):
        row = {
            "mcp_tool": "getDenylist",
            "mcp_response": {
                "server_calls": [],
                "transients": ["provider-unavailable: model provider is not serving this model"],
            },
        }
        modes = [mode for mode, _tool in h._failure_modes(row)]
        assert modes == ["model provider was not serving the model"]


class TestExpectedCallAcceptsServerIssuedIds:
    """Deleting a record by the id the server gave it names that record.

    The live run listed the rewrites, deleted `c41a6xwk`, and the REST read-back
    confirmed `ai-e2e-a-…example.com` was gone — yet the step failed because the
    delete call did not repeat the hostname. The server's own list answer is
    what tells a caller the id, and its 404 hint says "pass the rewrite's name
    and it will be resolved", so both spellings identify the same record.
    """

    HOST = "ai-e2e-a-e2eh5l1kdb.example.com"
    LIST_RESULT = (
        '{"data": [{"content": "192.0.2.100", "id": "c41a6xwk", "name": '
        '"ai-e2e-a-e2eh5l1kdb.example.com", "type": "A"}, '
        '{"content": "2001:db8::dead", "id": "y2fpqzs9", "name": '
        '"ai-e2e-aaaa-e2eh5l1kdb.example.com", "type": "AAAA"}]}'
    )
    EXPECT: ClassVar[dict] = {
        "endpoints": ["DELETE /profiles/{profile_id}/rewrites/{entry_id}"],
        "args": [HOST],
        "result": [],
        "data": True,
    }

    def _records(self, entry_id: str) -> list[dict]:
        return [
            {
                "tool": "listRewrites",
                "endpoint": "GET /profiles/{profile_id}/rewrites",
                "profile_id": "cc33cc",
                "ok": True,
                "write": False,
                "args": '{"profile_id": "cc33cc"}',
                "result": self.LIST_RESULT,
            },
            {
                "tool": "deleteRewrite",
                "endpoint": "DELETE /profiles/{profile_id}/rewrites/{entry_id}",
                "profile_id": "cc33cc",
                "ok": True,
                "write": True,
                "args": f'{{"entry_id": "{entry_id}", "profile_id": "cc33cc"}}',
                "result": '{"success": true}',
            },
        ]

    def test_the_id_the_server_returned_for_the_name_satisfies_the_expected_argument(self, h):
        ok, why = h._expected_call_ok(self._records("c41a6xwk"), self.EXPECT, "cc33cc")
        assert ok is True, why

    def test_the_hostname_still_satisfies_it(self, h):
        records = self._records("c41a6xwk")
        records[1]["args"] = f'{{"name": "{self.HOST}", "profile_id": "cc33cc"}}'
        ok, why = h._expected_call_ok(records, self.EXPECT, "cc33cc")
        assert ok is True, why

    def test_deleting_a_different_record_still_fails(self, h):
        """The equivalence is per name: y2fpqzs9 is the AAAA record, not this one."""
        ok, why = h._expected_call_ok(self._records("y2fpqzs9"), self.EXPECT, "cc33cc")
        assert ok is False
        assert "was called without" in why

    def test_an_id_the_server_never_returned_for_that_name_does_not_count(self, h):
        records = self._records("c41a6xwk")
        records.pop(0)  # no list call in the attempt: nothing established the id
        ok, why = h._expected_call_ok(records, self.EXPECT, "cc33cc")
        assert ok is False, why

    def test_labels_are_not_treated_as_names(self, h):
        """`type: "A"` must not tie every entry in the list to the word "A"."""
        assert h._identifiers_from_answers([self._records("c41a6xwk")[0]]) == {
            "192.0.2.100": ["c41a6xwk"],
            self.HOST: ["c41a6xwk"],
            "2001:db8::dead": ["y2fpqzs9"],
            "ai-e2e-aaaa-e2eh5l1kdb.example.com": ["y2fpqzs9"],
        }


class TestInstrumentVisibility:
    """The verdict is made from the server's call log; the actor must not read it."""

    LOG = "/tmp/nextdns_e2e_fr8u250p/mcp_calls.jsonl"

    def test_reading_the_log_by_path_is_detected(self, h):
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="read", args={"path": self.LOG})]
        assert h._actor_read_the_instrument(run, self.LOG) is True

    def test_reaching_it_through_code_mode_is_detected(self, h):
        """The actor in the live run got there with `fs.promises.readFile`."""
        run = h.ActorRun(command=["x"])
        run.tool_calls = [
            h.ToolCall(
                name="execute", args={"code": "await fs.promises.readFile('/tmp/nextdns_e2e_fr8u250p/mcp_calls.jsonl')"}
            )
        ]
        assert h._actor_read_the_instrument(run, self.LOG) is True

    def test_reading_its_own_mcp_config_is_not_flagged(self, h):
        """The config sits in the same directory and names the log; reading the
        config is the actor inspecting its own environment, not the record."""
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="read", args={"path": "/tmp/nextdns_e2e_fr8u250p/opencode.json"})]
        assert h._actor_read_the_instrument(run, self.LOG) is False

    def test_no_log_path_means_nothing_to_read(self, h):
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="read", args={"path": "/tmp/whatever/mcp_calls.jsonl"})]
        assert h._actor_read_the_instrument(run, "") is False

    def test_the_run_passes_a_path_object_not_a_string(self, h):
        """`run_harness` holds the log as a Path; the guard must accept both.

        The first live run after this guard landed died with
        `'PosixPath' object has no attribute 'lower'` on step 1.
        """
        import pathlib

        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="read", args={"path": self.LOG})]
        assert h._actor_read_the_instrument(run, pathlib.Path(self.LOG)) is True
        assert h._actor_read_the_instrument(run, pathlib.Path(self.LOG).parent / "other.jsonl") is False

    def test_the_step_is_named_as_a_measurement_problem_not_a_model_failure(self, h):
        row = {
            "id": "lists.allowlist.remove_added",
            "status": "failed",
            "mcp_tool": "getDenylist",
            "error": "list allowlist still contains added entry",
            "mcp_response": {"server_calls": [], "instrument_read": True},
        }
        modes = [mode for mode, _tool in h._failure_modes(row)]
        assert "actor read the harness call log" in modes


class TestRunValidityNotes:
    """A number that did not measure the model has to say so on the report."""

    def test_notes_reach_the_summary_row(self, h):
        report = h.Report()
        report.notes.append("self-heal: test profile was deleted mid-run")
        assert report.summary()["notes"] == ["self-heal: test profile was deleted mid-run"]

    def test_summarize_warns_before_the_modes(self, h, tmp_path):
        report = tmp_path / "r.jsonl"
        rows = [
            {
                "id": "profiles.get",
                "status": "failed",
                "mcp_tool": "getProfile",
                "error": "profile not found via REST",
                "mcp_response": {"server_calls": [], "expected_call": {"ok": False, "why": "no call"}},
            },
            {"summary": {"notes": ["self-heal: test profile was deleted mid-run; recreated as ff66ff"]}},
        ]
        report.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        summary = h.summarize_reports([str(report)])
        assert "RUN NOT A CLEAN MEASUREMENT" in summary
        assert "recreated as ff66ff" in summary

    def test_a_clean_run_carries_no_warning(self, h, tmp_path):
        report = tmp_path / "clean.jsonl"
        rows = [
            {
                "id": "profiles.list",
                "status": "passed",
                "mcp_tool": "getProfile",
                "error": "",
                "mcp_response": {
                    "server_calls": [{"tool": "getProfile", "ok": True}],
                    "expected_call": {"ok": True},
                },
            },
            {"summary": {"notes": []}},
        ]
        report.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        assert "RUN NOT A CLEAN MEASUREMENT" not in h.summarize_reports([str(report)])

    def test_steps_skipped_by_an_abort_are_not_counted_as_failures(self, h, tmp_path):
        """Aborting leaves dozens of skipped rows; grouping them as failure
        modes would bury the one mode that mattered."""
        report = tmp_path / "abort.jsonl"
        rows = [
            {
                "id": f"profiles.step{i}",
                "status": "skipped",
                "mcp_tool": "getProfile",
                "reason": "model provider is not serving this model (run aborted)",
                "mcp_response": {},
            }
            for i in range(4)
        ] + [{"summary": {"notes": ["provider outage after 3 step(s): Specified function in account"]}}]
        report.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        summary = h.summarize_reports([str(report)])
        assert "no failures" in summary
        assert "4 skipped" in summary
        assert "model provider is not serving this model (run aborted)" in summary
        assert "no call reached the server" not in summary

    def test_provider_stall_is_retried(self, h):
        run = h.ActorRun(command=["x"], error="actor harness timed out after 300s")
        assert h._transient_actor_failure(run, "") == ("stall", "actor harness timed out after 300s")

    def test_timeout_after_work_is_not_retried(self, h):
        """A step that DID act and still timed out is the model's own doing."""
        run = h.ActorRun(command=["x"], error="actor harness timed out after 300s")
        run.tool_calls = [h.ToolCall(name="getDenylist", args={})]
        assert h._transient_actor_failure(run, "") == ("", "")

    def test_provider_stream_drop_is_retried(self, h):
        """Observed live: the provider ended mid tool call, and the client
        recorded call_tool({}) — the model never made that call."""
        run = h.ActorRun(command=["x"])
        assert h._transient_actor_failure(run, "openai chat stream ended without finish_reason") == (
            "provider-stream",
            "model provider ended the response mid tool call",
        )

    def test_state_error_text_reaches_the_verdict_inputs(self, h):
        """opencode puts the reason in state.error, not state.output."""
        lines = [
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "tools.nextdns.call_tool",
                        "state": {
                            "status": "error",
                            "input": {},
                            "error": "OpenAI Chat stream ended without finish_reason",
                        },
                    },
                }
            )
        ]
        calls, _ = h.parse_actor_events(lines)
        assert calls[0].name == "call_tool"
        assert not calls[0].ok
        assert "finish_reason" in calls[0].result_text

    def test_ordinary_failure_is_not_retried(self, h):
        run = h.ActorRun(command=["x"], error="")
        assert h._transient_actor_failure(run, "list does not contain entry") == ("", "")

    def test_stalls_get_more_attempts_than_registration_hiccups(self, h):
        """A stall costs the step its whole verdict, so it deserves more chances."""
        assert h.TRANSIENT_ATTEMPTS["stall"] > h.TRANSIENT_ATTEMPTS["unknown-tool"]
        assert all(n >= 2 for n in h.TRANSIENT_ATTEMPTS.values())


class TestStraySweep:
    """Profiles this run's receipts prove it created are removed at the end.

    The sweep used to diff the account against a before/after snapshot and
    delete everything that appeared during the run: a failed baseline read
    exposed every pre-existing profile, and a profile created concurrently by
    another user or run was treated as this run's to delete. Now only ids from
    successful createProfile rows (plus harness provisioning/self-heal) are
    candidates, and a failed listing stands the sweep down entirely.
    """

    def test_all_profile_ids_maps_names(self, h):

        m = _FakeClient(
            by_path={"/profiles": {"ok": True, "body": {"data": [{"id": "a1", "name": "One"}, {"id": "b2"}]}}}
        )
        assert asyncio.run(h._all_profile_ids(m)) == {"a1": "One", "b2": ""}

    def test_all_profile_ids_is_none_when_the_listing_fails(self, h):
        """An empty mapping from a failed read looked like an empty account."""

        m = _FakeClient(by_path={"/profiles": {"status": 503, "ok": False, "body": "boom"}})
        assert asyncio.run(h._all_profile_ids(m)) is None

    def test_all_profile_ids_follows_the_listing_cursor(self, h):
        """One GET is one page; the map must cover every profile on the account."""

        class _Paged:
            def __init__(self):
                self.calls: list[str] = []

            async def rest_get(self, path, params=None):
                assert path == "/profiles"
                self.calls.append((params or {}).get("cursor"))
                if self.calls[-1] == "c2":
                    return {"status": 200, "ok": True, "body": {"data": [{"id": "b2", "name": "Two"}]}}
                return {"status": 200, "ok": True, "body": {"data": [{"id": "a1", "name": "One"}], "cursor": "c2"}}

        m = _Paged()
        assert asyncio.run(h._all_profile_ids(m)) == {"a1": "One", "b2": "Two"}
        assert m.calls == [None, "c2"]

    def test_all_profile_ids_is_none_when_a_later_page_fails(self, h):
        """A truncated first page must not stand the stray sweep on a partial map."""

        class _Paged:
            async def rest_get(self, path, params=None):
                if params and params.get("cursor"):
                    return {"status": 503, "ok": False, "body": "boom"}
                return {"status": 200, "ok": True, "body": {"data": [{"id": "a1"}], "cursor": "c2"}}

        assert asyncio.run(h._all_profile_ids(_Paged())) is None

    def test_all_profile_ids_is_none_on_a_cursor_cycle(self, h):
        """A repeated cursor is a server-side cycle, not the end of the list.

        The loop used to break on the repeat and return the rows gathered so
        far as a complete map — a partial inventory the stray sweep then
        trusted. It must fail closed instead.
        """

        class _Cyclic:
            async def rest_get(self, path, params=None):
                if (params or {}).get("cursor") == "c2":
                    return {"status": 200, "ok": True, "body": {"data": [{"id": "b2"}], "cursor": "c2"}}
                return {"status": 200, "ok": True, "body": {"data": [{"id": "a1"}], "cursor": "c2"}}

        assert asyncio.run(h._all_profile_ids(_Cyclic())) is None

    def test_all_profile_ids_is_none_on_a_malformed_cursor(self, h):
        """A present-but-invalid cursor is a failed page, not a crash.

        ``_profile_list_cursor`` raises ``HarnessAbortError`` — fatal for the
        startup probe — but mid-run that raise used to escape the step loop
        and kill the harness with a traceback and no report, where the
        pre-pagination code returned a plain verdict.
        """

        class _Malformed:
            async def rest_get(self, path, params=None):
                return {"status": 200, "ok": True, "body": {"data": [{"id": "a1"}], "cursor": 0}}

        assert asyncio.run(h._all_profile_ids(_Malformed())) is None

    def test_all_profile_ids_is_none_when_the_listing_never_ends(self, h):
        """Fresh unique cursors forever are a runaway loop, not a huge account.

        Unbounded, quickly-answered pages never trip the per-request timeout
        and the step budget is only checked between steps, so the run would
        hang mid-step with no report. The loop is capped like the probe's.
        """

        class _Endless:
            def __init__(self):
                self.calls = 0

            async def rest_get(self, path, params=None):
                self.calls += 1
                body = {"data": [{"id": f"p{self.calls}"}], "cursor": f"c{self.calls}"}
                return {"status": 200, "ok": True, "body": body}

        m = _Endless()
        assert asyncio.run(h._all_profile_ids(m)) is None
        assert m.calls == h._MAX_PROFILE_PAGES

    def test_created_ids_come_from_successful_create_receipts(self, h):
        records = [
            {"endpoint": "POST /profiles", "ok": True, "result": '{"data": {"id": "new1"}}'},
            {"endpoint": "POST /profiles", "ok": True, "result": '{"id": "new2"}'},
            {"endpoint": "POST /profiles", "ok": False, "result": '{"error": "boom"}'},
            {"endpoint": "GET /profiles", "ok": True, "result": '{"data": [{"id": "seen"}]}'},
            {"endpoint": "POST /profiles", "ok": True, "result": "not json"},
        ]
        assert h._created_ids_from_records(records) == {"new1", "new2"}

    def test_sweep_removes_only_run_created_profiles(self, h, monkeypatch):

        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(
            by_path={
                "/profiles": {
                    "ok": True,
                    "body": {
                        "data": [
                            {"id": "real", "name": "Pumpkin Bum"},  # pre-existing, never a target
                            {"id": "test", "name": "P-renamed"},  # the test profile
                            {"id": "stray", "name": "new-profile"},  # actor-created extra
                        ]
                    },
                }
            }
        )
        note = asyncio.run(h._sweep_stray_profiles(m, {"stray", "test"}, {"test"}))
        assert m.deleted == ["/profiles/stray"]
        assert note == ""

    def test_sweep_ignores_ids_that_have_no_receipt(self, h, monkeypatch):
        """A profile that merely appeared (another user, another run) is not deleted."""

        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(
            by_path={"/profiles": {"ok": True, "body": {"data": [{"id": "other", "name": "Someone else's"}]}}}
        )
        asyncio.run(h._sweep_stray_profiles(m, set(), set()))
        assert m.deleted == []

    def test_sweep_never_touches_protected_or_plot_profiles(self, h, monkeypatch):

        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "home12")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "plot99")
        m = _FakeClient(
            by_path={
                "/profiles": {
                    "ok": True,
                    "body": {"data": [{"id": "home12", "name": "Home"}, {"id": "plot99", "name": "Plot"}]},
                }
            }
        )
        asyncio.run(h._sweep_stray_profiles(m, {"home12", "plot99"}, set()))
        assert m.deleted == []
        assert h._never_delete_ids() == {"home12", "plot99"}

    def test_sweep_skips_created_ids_already_gone(self, h, monkeypatch):
        """A receipt for a profile deleted during the run needs no cleanup."""

        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(by_path={"/profiles": {"ok": True, "body": {"data": []}}})
        note = asyncio.run(h._sweep_stray_profiles(m, {"gone1"}, set()))
        assert m.deleted == []
        assert note == ""

    def test_sweep_stands_down_when_the_listing_fails(self, h, monkeypatch):
        """Account state uncertain -> delete nothing, and say so in the report."""

        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(by_path={"/profiles": {"status": 503, "ok": False, "body": "boom"}})
        note = asyncio.run(h._sweep_stray_profiles(m, {"stray1", "stray2"}, set()))
        assert m.deleted == []
        assert "stray sweep skipped" in note
        assert "stray1" in note and "stray2" in note


class TestPlotProfileFallback:
    """With NEXTDNS_PLOT_PROFILE unset the plot/log steps used to read the
    empty test profile and skip as data-starved, measuring nothing without
    saying why. The resolver picks a data-bearing profile instead, or reports
    honestly why it could not."""

    def test_unset_falls_back_to_the_first_data_bearing_profile(self, h, monkeypatch):

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        m = _FakeClient(
            profiles=[
                {"id": "testpid", "name": "ai-e2e-x"},
                {"id": "empty1", "name": "Empty"},
                {"id": "home12", "name": "Home"},
            ],
            by_path={
                "/profiles/empty1/analytics/status": {"ok": True, "body": {"data": []}},
                "/profiles/home12/analytics/status": {"ok": True, "body": {"data": [{"queries": 42}]}},
            },
        )
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert os.environ.get("NEXTDNS_PLOT_PROFILE") == "home12"
        assert "home12" in note and "unset" in note
        # The fallback registers as the plot profile, so the stray sweep and
        # the never-delete set both protect it.
        assert "home12" in h._never_delete_ids()

    def test_the_test_profile_and_protected_ids_are_never_candidates(self, h, monkeypatch):

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "home12")
        # Both readable profiles are excluded: one is the write target of the
        # run, the other is on the denylist. No probe may pick them.
        m = _FakeClient(profiles=[{"id": "testpid", "name": "P"}, {"id": "home12", "name": "Home"}])
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert "NEXTDNS_PLOT_PROFILE" not in os.environ
        assert "no readable profile" in note

    def test_a_configured_plot_profile_is_left_alone(self, h, monkeypatch):

        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "aa11aa")
        m = _FakeClient()
        assert asyncio.run(h._ensure_plot_profile(m, "testpid")) == ""
        assert os.environ["NEXTDNS_PLOT_PROFILE"] == "aa11aa"

    def test_a_failed_listing_says_so_and_sets_nothing(self, h, monkeypatch):

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(by_path={"/profiles": {"status": 503, "ok": False, "body": "boom"}})
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert "NEXTDNS_PLOT_PROFILE" not in os.environ
        assert "listing failed" in note

    def test_no_data_bearing_profile_skips_with_an_honest_note(self, h, monkeypatch):

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        m = _FakeClient(
            profiles=[{"id": "other", "name": "Other"}],
            by_path={"/profiles/other/analytics/status": {"ok": True, "body": {"data": []}}},
        )
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert "NEXTDNS_PLOT_PROFILE" not in os.environ
        assert "data-starved" in note
