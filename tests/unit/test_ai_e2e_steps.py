"""Unit tests for the E2E harness's step machinery.

Covers profile-scoped call matching, checklist expectation shape, verify step
verdicts, the missing-profile-id gate, server-call evidence, step prompts and
ranges. The rest of the harness suite lives in ``test_ai_e2e_harness.py``,
``test_ai_e2e_measure.py``, ``test_ai_e2e_safety.py``, and
``test_ai_e2e_run.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # the `h` fixture is supplied by tests/unit/conftest.py
    import ai_e2e_harness as h

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


# ===========================================================================
# profile-scoped expected calls: a step is judged on the profile it targeted
# =========================================================================== #


class TestProfileScopedCallMatching:
    def _rec(self, profile_id: str) -> dict:
        return {
            "tool": "getLogs",
            "endpoint": "GET /profiles/{profile_id}/logs",
            "profile_id": profile_id,
            "ok": True,
            "write": False,
            "args": "{}",
            "result": '{"data": []}',
        }

    def _expect(self, profile: str) -> dict:
        return {
            "endpoints": ["GET /profiles/{profile_id}/logs"],
            "args": [],
            "result": [],
            "data": True,
            "profile": profile,
        }

    def test_default_scope_requires_the_recorded_test_profile(self, h):
        expect = self._expect("")
        del expect["profile"]
        assert h._expected_call_ok([self._rec("testpid")], expect, "testpid")[0]
        ok, why = h._expected_call_ok([self._rec("otherpid")], expect, "testpid")
        assert not ok and "otherpid, not the testpid under test" in why

    def test_any_profile_token_ignores_the_recorded_profile(self, h):
        """createProfile/listProfiles carry no profile_id; they cannot be scoped."""
        expect = self._expect(h.ANY_PROFILE)
        rec = self._rec("someone-else")
        del rec["profile_id"]  # the row the server really writes for listProfiles
        assert h._expected_call_ok([rec], expect, "testpid")[0]

    def test_plot_token_matches_the_alt_profile(self, h):
        expect = self._expect(h.PLOT_PROFILE)
        assert h._expected_call_ok([self._rec("plotpid")], expect, "testpid", alt_pid="plotpid")[0]
        ok, why = h._expected_call_ok([self._rec("plotpid")], expect, "testpid")
        assert not ok and "not the testpid under test" in why

    def test_plot_token_falls_back_to_the_test_profile(self, h):
        """With no NEXTDNS_PLOT_PROFILE configured the step targets the test profile."""
        expect = self._expect(h.PLOT_PROFILE)
        assert h._expected_call_ok([self._rec("testpid")], expect, "testpid", alt_pid="")[0]

    def test_expect_label_shows_the_profile_scope(self, h):
        step = h.Step(
            id="plots.status",
            section="plots",
            tool="plotAnalytics",
            description="d",
            prompt="p",
            verify="plots",
            expect=h._expect_local("plotAnalytics", args=("status",), result=("image",), profile=h.PLOT_PROFILE),
        )
        label = h._expect_label(step)
        assert "plotAnalytics" in label
        assert f"profile={h.PLOT_PROFILE}" in label


# ===========================================================================
# the checklist's expectations: profile scope + the list-update payload pin
# =========================================================================== #


class TestChecklistExpectationShape:
    @pytest.fixture()
    def steps(self, h) -> dict:
        return {s.id: s for s in h.build_steps(h.TaskTargets(profile_name="P", tag="tg"))}

    def test_steps_are_built_with_aligned_fields(self, h, steps):
        """Keyword construction: every field names its own step (a positional swap would not)."""
        for step in steps.values():
            assert step.id
            assert step.section == step.id.split(".")[0]
            assert step.description and step.prompt
            assert step.verify and step.expect

    def test_profile_scoped_steps(self, h, steps):
        assert steps["profiles.create"].expect["profile"] == h.ANY_PROFILE
        assert steps["profiles.list"].expect["profile"] == h.ANY_PROFILE
        assert steps["plots.status"].expect["profile"] == h.PLOT_PROFILE
        assert steps["logs.download"].expect["profile"] == h.PLOT_PROFILE
        # Everything else is scoped to the test profile by default.
        assert "profile" not in steps["lists.denylist.add"].expect
        assert "profile" not in steps["logs.get"].expect
        assert "profile" not in steps["profiles.delete"].expect

    def test_list_update_steps_pin_the_entry_and_the_false_value(self, h, steps):
        """`args=("active",)` alone passed for active=true and for other entries."""
        targets = h.TaskTargets(profile_name="P", tag="tg")
        for name in ("allowlist", "denylist", "parental_categories", "parental_services"):
            args = steps[f"lists.{name}.update"].expect["args"]
            assert args[0] == targets.list_entries[name]
            assert '"active": false' in args

    def test_list_entries_come_from_the_shared_table(self, h):
        targets = h.TaskTargets(profile_name="P", tag="zz9")
        assert targets.list_entries["denylist"] == "ai-e2e-deny-zz9.example.com"
        assert targets.list_entries["parental_services"] == "tiktok"
        assert set(targets.list_entries) == set(h.LIST_ENTRIES)


# ===========================================================================
# verification verdicts: absence and state need proof, not a failed read
# =========================================================================== #


class TestVerifyStepVerdicts:
    def _step(self, h, verify: str, vdata=None) -> "h.Step":
        return h.Step(
            id=f"v.{verify}",
            section="lists",
            tool="t",
            description="d",
            prompt="p",
            verify=verify,
            vdata=vdata or {},
        )

    def _run(self, h):
        return h.ActorRun(command=["x"])

    def test_profile_gone_never_confirms_an_empty_id(self, h):
        """A failed profiles.create left pid == "": the verifier used to answer
        (True, ABSENT) and report the deletion verified with zero REST reads."""

        targets = h.TaskTargets(profile_name="P", tag="tg")
        client = _FakeClient()
        status, err, _, rest = asyncio.run(
            h._verify_step(client, targets, "", self._step(h, "profile_gone"), self._run(h))
        )
        assert status == "failed"
        assert "no profile id" in err
        assert rest["verified_gone"] is False and rest["verdict"] == h.INDETERMINATE
        assert client.got == [] and client.deleted == []  # nothing was read or deleted

    def test_list_update_requires_the_entry_read_back_inactive(self, h):

        targets = h.TaskTargets(profile_name="P", tag="tg")
        step = self._step(
            h,
            "list_update",
            {"name": "denylist", "spec": {"path": "/denylist"}, "values": ["x.example.com"]},
        )

        def client(active_rows):
            return _FakeClient(by_path={"/profiles/pid1/denylist": {"ok": True, "body": {"data": active_rows}}})

        status, err, _, rest = asyncio.run(
            h._verify_step(client([{"id": "x.example.com", "active": False}]), targets, "pid1", step, self._run(h))
        )
        assert status == "passed" and rest["verified_inactive"] is True
        # active=true is not the outcome the step asked for.
        status, err, _, _ = asyncio.run(
            h._verify_step(client([{"id": "x.example.com", "active": True}]), targets, "pid1", step, self._run(h))
        )
        assert status == "failed" and "active=True" in err
        # An update that touched a different entry did not do this step's work.
        status, _, _, rest = asyncio.run(
            h._verify_step(client([{"id": "other.example.com", "active": False}]), targets, "pid1", step, self._run(h))
        )
        assert status == "failed" and rest["verdict"] == h.ENTRY_MISSING
        # A failed read proves nothing.
        broken = _FakeClient(by_path={"/profiles/pid1/denylist": {"status": 503, "ok": False, "body": "boom"}})
        status, err, _, rest = asyncio.run(h._verify_step(broken, targets, "pid1", step, self._run(h)))
        assert status == "failed" and h.INDETERMINATE in rest["verdict"]

    def test_list_removal_needs_a_confirmed_absence(self, h):

        targets = h.TaskTargets(profile_name="P", tag="tg")
        step = self._step(
            h,
            "list_remove",
            {"name": "denylist", "spec": {"path": "/denylist"}, "values": ["x.example.com"]},
        )
        gone = _FakeClient(by_path={"/profiles/pid1/denylist": {"ok": True, "body": {"data": []}}})
        status, _, _, rest = asyncio.run(h._verify_step(gone, targets, "pid1", step, self._run(h)))
        assert status == "passed" and rest["verdict"] == h.ABSENT
        # A 503 used to count as "gone".
        broken = _FakeClient(by_path={"/profiles/pid1/denylist": {"status": 503, "ok": False, "body": "boom"}})
        status, err, _, rest = asyncio.run(h._verify_step(broken, targets, "pid1", step, self._run(h)))
        assert status == "failed" and h.INDETERMINATE in rest["verdict"]
        assert "not confirmed" in err
        # Verification must not itself delete anything (the old _req helper
        # DELETEd for any non-GET method a caller might pass).
        assert broken.deleted == [] and gone.deleted == []

    def test_rewrite_deletion_needs_a_confirmed_absence(self, h):

        targets = h.TaskTargets(profile_name="P", tag="tg")
        step = self._step(h, "rewrite_delete", {"record": {"name": "a.example.com", "content": "1.2.3.4"}})
        gone = _FakeClient(by_path={"/profiles/pid1/rewrites": {"ok": True, "body": {"data": []}}})
        status, _, _, rest = asyncio.run(h._verify_step(gone, targets, "pid1", step, self._run(h)))
        assert status == "passed" and rest["verdict"] == h.ABSENT
        four_oh_four = _FakeClient(by_path={"/profiles/pid1/rewrites": {"status": 404, "ok": False, "body": {}}})
        status, _, _, _ = asyncio.run(h._verify_step(four_oh_four, targets, "pid1", step, self._run(h)))
        assert status == "passed"
        # A malformed body is not proof the record is gone.
        malformed = _FakeClient(by_path={"/profiles/pid1/rewrites": {"ok": True, "body": {"data": "not-a-list"}}})
        status, err, _, rest = asyncio.run(h._verify_step(malformed, targets, "pid1", step, self._run(h)))
        assert status == "failed" and h.INDETERMINATE in rest["verdict"]
        assert "not confirmed" in err

    def test_skipped_plot_and_log_steps_report_the_path_they_read(self, h, monkeypatch):
        """MAINT-17: the evidence said `GET {test profile}/...` for a read of the plot profile."""

        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "plotpid")
        targets = h.TaskTargets(profile_name="P", tag="tg")
        no_data = _FakeClient(by_path={"/profiles/plotpid/analytics/status": {"ok": True, "body": {"data": []}}})
        status, _, _, rest = asyncio.run(
            h._verify_step(no_data, targets, "pid1", self._step(h, "plots", {"metric": "status"}), self._run(h))
        )
        assert status == "skipped"
        assert rest["endpoint"] == "GET /profiles/plotpid/analytics/status"

        no_logs = _FakeClient(by_path={"/profiles/plotpid/logs": {"ok": True, "body": {"data": []}}})
        status, _, _, rest = asyncio.run(
            h._verify_step(no_logs, targets, "pid1", self._step(h, "logs_download"), self._run(h))
        )
        assert status == "skipped"
        assert rest["endpoint"] == "GET /profiles/plotpid/logs"

    @pytest.mark.parametrize(
        "verify",
        ["logs_bogus", "list_bogus", "rewrite_bogus", "profile_bogus", "settings_bogus", "bogus"],
    )
    def test_unknown_verifier_makes_no_request_and_names_itself(self, h, verify):
        """A verifier name no branch implements is a bug, not a live API call.

        Dispatch used to be by prefix (`startswith("logs_")`), and the family
        helper's last branch was an unguarded fallthrough, so a typo such as
        `logs_bogus` landed in `logs_clear` — a real `DELETE /profiles/<id>/logs`
        against the account, reported as a pass. Unknown `list_*` / `rewrite_*`
        names fell into the last branch and raised KeyError on vdata the step
        never carried. Every unknown name now answers the pre-split contract:
        failed, `unknown verifier <name>`, no REST call of any kind.
        """

        targets = h.TaskTargets(profile_name="P", tag="tg")
        client = _FakeClient()
        status, err, reason, rest = asyncio.run(
            h._verify_step(client, targets, "pid1", self._step(h, verify), self._run(h))
        )
        assert status == "failed"
        assert err == f"unknown verifier {verify!r}"
        assert reason == "" and rest == {}
        assert client.got == [] and client.deleted == [] and client.posted == []

    def test_no_catalog_step_names_an_unimplemented_verifier(self, h, all_steps):
        """The catalog and the dispatcher must agree on the verifier names."""
        v = h._verify
        implemented = (
            set(v._PROFILE_VERIFIERS)
            | set(v._SETTINGS_VERIFIERS)
            | set(v._LIST_VERIFIERS)
            | set(v._REWRITE_VERIFIERS)
            | set(v._PLOT_VERIFIERS)
            | set(v._LOG_VERIFIERS)
            | {"analytics", "tool_ok"}
        )
        named = {s.verify for s in all_steps}
        assert named - implemented == set(), f"catalog verifiers with no branch: {sorted(named - implemented)}"


class TestMissingProfileIdNeverLaunchesTheActor:
    """profiles.delete used to run with state.pid == "" after a failed create:
    the actor got ``Delete the profile ''.`` and the verifier confirmed the
    deletion of nothing. Profile-id-dependent steps are now skipped before
    the actor is launched; profiles.create names no id and still runs.
    """

    def _state(self, h, tmp_path, step):
        run_mod = sys.modules["ai_e2e_run"]
        targets = h.TaskTargets(profile_name="P", tag="t0")
        cfg = h.HarnessConfig()
        return run_mod._RunState(
            config=cfg,
            targets=targets,
            report=h.Report(),
            workdir=tmp_path,
            call_log=tmp_path / "calls.jsonl",
            step_cfg=cfg,
            selected=[(1, step)],
            total=1,
        )

    def test_delete_step_is_skipped_before_the_actor_runs(self, h, tmp_path, monkeypatch):

        run_mod = sys.modules["ai_e2e_run"]
        step = next(s for s in h.build_steps(h.TaskTargets(profile_name="P", tag="t0")) if s.id == "profiles.delete")
        state = self._state(h, tmp_path, step)
        assert state.pid == ""

        async def _boom(*args, **kwargs):
            raise AssertionError("the actor must not be launched without a profile id")

        monkeypatch.setattr(run_mod, "_attempt_step", _boom)
        asyncio.run(run_mod._run_one_step(state, None, 1, step))
        [row] = state.report.results
        assert row.id == "profiles.delete" and row.status == "skipped"

    def test_create_step_is_not_caught_by_the_skip(self, h, tmp_path, monkeypatch):
        """profiles.create names no {profile_id}: it must still reach the actor."""

        run_mod = sys.modules["ai_e2e_run"]
        step = next(s for s in h.build_steps(h.TaskTargets(profile_name="P", tag="t0")) if s.id == "profiles.create")
        state = self._state(h, tmp_path, step)

        class _Reached(Exception):
            pass

        async def _sentinel(*args, **kwargs):
            raise _Reached

        monkeypatch.setattr(run_mod, "_attempt_step", _sentinel)
        with pytest.raises(_Reached):
            asyncio.run(run_mod._run_one_step(state, None, 1, step))
        assert state.report.results == []  # not skipped: the attempt was made


# =========================================================================== #
# targeted runs: an inclusive range of the step checklist
# =========================================================================== #


@pytest.fixture(scope="module")
def all_steps(h):
    """The full checklist a run would execute."""
    return h.build_steps(h.TaskTargets(profile_name="P", tag="t0"))


class TestServerCallEvidence:
    """A REST assertion proves the state, not who produced it."""

    def test_read_new_calls_returns_only_lines_appended_since_offset(self, h, tmp_path):
        path = tmp_path / h.CALL_LOG_NAME
        path.write_text('{"tool": "createProfile", "ok": true}\n', encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"tool": "addToDenylist", "endpoint": "POST /profiles/{profile_id}/denylist", "ok": false}\n')
        assert h._read_new_calls(path, offset) == [
            {"tool": "addToDenylist", "endpoint": "POST /profiles/{profile_id}/denylist", "ok": False}
        ]
        assert h._read_new_calls(tmp_path / "absent.jsonl", 0) == []

    def test_expected_call_requires_the_exact_successful_call(self, h):
        step = h.Step(
            "lists.denylist.add",
            "lists",
            "addToDenylist",
            "d",
            "p",
            "list_add",
            expect=h._expect("POST /profiles/{profile_id}/denylist", args=("ads.example.com",)),
        )
        right = {
            "tool": "addToDenylist",
            "endpoint": "POST /profiles/{profile_id}/denylist",
            "profile_id": "pid",
            "ok": True,
            "write": True,
            "args": '{"entry_id": "ads.example.com"}',
            "result": '{"data": []}',
        }
        assert h._expected_call_ok([right], step.expect, "pid")[0]
        # a different endpoint, the entry missing, and a failed call all miss
        wrong_ep = {**right, "endpoint": "PUT /profiles/{profile_id}/denylist"}
        wrong_arg = {**right, "args": '{"entry_id": "other.example.com"}'}
        failed = {**right, "ok": False}
        for record in (wrong_ep, wrong_arg, failed, {"tool": "search_tools", "ok": True}):
            assert not h._expected_call_ok([record], step.expect, "pid")[0]
        assert not h._expected_call_ok([], step.expect, "pid")[0]

    def test_expected_call_allows_confirming_reads_after_the_write(self, h):
        """Models routinely read back what they wrote; that is not a failure."""
        step = h.Step(
            "lists.denylist.replace",
            "lists",
            "replaceDenylist",
            "d",
            "p",
            "list_replace",
            expect=h._expect("PUT /profiles/{profile_id}/denylist"),
        )
        write = {
            "tool": "replaceDenylist",
            "endpoint": "PUT /profiles/{profile_id}/denylist",
            "profile_id": "pid",
            "ok": True,
            "write": True,
            "args": '{"entry_ids": ["x"]}',
            "result": '{"data": []}',
        }
        confirm = {
            "tool": "getDenylist",
            "endpoint": "GET /profiles/{profile_id}/denylist",
            "profile_id": "pid",
            "ok": True,
            "write": False,
            "args": "{}",
            "result": '{"data": []}',
        }
        assert h._expected_call_ok([write, confirm], step.expect, "pid")[0]

    def test_expected_call_rejects_a_write_that_undoes_the_step(self, h):
        """The rewrite add-then-delete seen in a live run fails on structure."""
        step = h.Step(
            "rewrites.add.a",
            "rewrites",
            "addRewrite",
            "d",
            "p",
            "rewrite_add",
            expect=h._expect("POST /profiles/{profile_id}/rewrites", args=("a.example.com", "192.0.2.100")),
        )
        add = {
            "tool": "addRewrite",
            "endpoint": "POST /profiles/{profile_id}/rewrites",
            "profile_id": "pid",
            "ok": True,
            "write": True,
            "args": '{"name": "a.example.com", "content": "192.0.2.100"}',
            "result": '{"data": []}',
        }
        delete = {
            "tool": "deleteRewrite",
            "endpoint": "DELETE /profiles/{profile_id}/rewrites/{entry_id}",
            "ok": True,
            "write": True,
            "args": '{"entry_id": "c41a6xwk"}',
            "result": '{"success": true}',
        }
        ok, why = h._expected_call_ok([add, delete], step.expect, "pid")
        assert not ok
        assert "followed by the write DELETE /profiles/{profile_id}/rewrites/{entry_id}" in why

    def test_expected_call_ignores_a_failed_write_after_the_match(self, h):
        """A rejected duplicate changed nothing, so it cannot undo the step.

        Seen live in rewrites.add.a: the add succeeded, then the model added
        the same record again and upstream rejected the duplicate.
        """
        step = h.Step(
            "rewrites.add.a",
            "rewrites",
            "addRewrite",
            "d",
            "p",
            "rewrite_add",
            expect=h._expect("POST /profiles/{profile_id}/rewrites", args=("a.example.com",)),
        )
        add = {
            "tool": "addRewrite",
            "endpoint": "POST /profiles/{profile_id}/rewrites",
            "profile_id": "pid",
            "ok": True,
            "write": True,
            "args": '{"name": "a.example.com", "content": "192.0.2.100"}',
            "result": '{"data": []}',
        }
        duplicate = {**add, "ok": False, "result": '{"error": "already exists"}'}
        assert h._expected_call_ok([add, duplicate], step.expect, "pid")[0]

    def test_expected_call_reason_names_the_failed_condition(self, h):
        step = h.Step(
            "profiles.get",
            "profiles",
            "getProfile",
            "d",
            "p",
            "profile_exists",
            expect=h._expect("GET /profiles/{profile_id}", args=("{profile_id}",), result=("{profile_id}",)),
        )
        assert "no GET /profiles/{profile_id} call" in h._expected_call_ok([], step.expect, "abc")[1]
        failed = {
            "tool": "getProfile",
            "endpoint": "GET /profiles/{profile_id}",
            "ok": False,
            "args": "{}",
            "result": "",
        }
        assert "every such call failed" in h._expected_call_ok([failed], step.expect, "abc")[1]
        wrong_id = {
            "tool": "getProfile",
            "endpoint": "GET /profiles/{profile_id}",
            "profile_id": "abc",
            "ok": True,
            "write": False,
            "args": '{"profile_id": "other"}',
            "result": '{"data": {"id": "other"}}',
        }
        assert "without ['abc']" in h._expected_call_ok([wrong_id], step.expect, "abc")[1]
        empty = {
            "tool": "getProfile",
            "endpoint": "GET /profiles/{profile_id}",
            "profile_id": "abc",
            "ok": True,
            "write": False,
            "args": '{"profile_id": "abc"}',
            "result": '{"error": "notFound"}',
        }
        assert "answered without" in h._expected_call_ok([empty], step.expect, "abc")[1]
        # The call ran against a different readable profile: the endpoint
        # template matched, the argument matched, but this is not an answer
        # about the profile under test.
        wrong_profile = {
            "tool": "getProfile",
            "endpoint": "GET /profiles/{profile_id}",
            "profile_id": "other",
            "ok": True,
            "write": False,
            "args": '{"profile_id": "abc"}',
            "result": '{"data": {"id": "abc"}}',
        }
        why = h._expected_call_ok([wrong_profile], step.expect, "abc")[1]
        assert "not the abc under test" in why

    def test_expected_call_accepts_the_chart_marker_from_evidence_only(self, h):
        """A chart marker lifted into the evidence column is the answer.

        Plot and dashboard tools answer with a lossy result text; the
        component facts travel in the ``evidence`` column, so a step that
        expects the chart marker must see it there. Marker-free text is the
        negative: evidence without the marker is not an answer either.
        """
        step = h.Step(
            "plots.status",
            "plots",
            "plotAnalytics",
            "d",
            "p",
            "chart",
            expect=h._expect_local("plotAnalytics", args=("status",), result=("AreaChart",), profile=h.PLOT_PROFILE),
        )
        base = {
            "tool": "plotAnalytics",
            "profile_id": "plotpid",
            "ok": True,
            "write": False,
            "args": '{"metric": "status"}',
            "result": '{"content": ["image"]}',
        }
        ok, why = h._expected_call_ok(
            [dict(base, evidence={"components": ["AreaChart", "ChartSeries", "Heading"]})],
            step.expect,
            "testpid",
            alt_pid="plotpid",
        )
        assert ok, why
        ok, why = h._expected_call_ok(
            [dict(base, evidence={"components": ["Card", "DataTable"]})],
            step.expect,
            "testpid",
            alt_pid="plotpid",
        )
        assert not ok
        assert "answered without ['AreaChart']" in why
        ok, why = h._expected_call_ok([base], step.expect, "testpid", alt_pid="plotpid")
        assert not ok
        assert "answered without ['AreaChart']" in why

    def test_expected_call_accepts_the_prefab_marker_in_the_result_text(self, h):
        """The dashboard's $prefab marker rides the result projection itself."""
        step = h.Step(
            "dashboard.overview",
            "dashboard",
            "overviewDashboard",
            "d",
            "p",
            "dashboard",
            expect=h._expect_local("overviewDashboard", result=("$prefab",), profile=h.PLOT_PROFILE),
        )
        record = {
            "tool": "overviewDashboard",
            "profile_id": "plotpid",
            "ok": True,
            "write": False,
            "args": "{}",
            "result": '{"content": "[Rendered NextDNS Dashboard]", "$prefab": true}',
        }
        ok, why = h._expected_call_ok([record], step.expect, "testpid", alt_pid="plotpid")
        assert ok, why
        marker_free = {**record, "result": '{"content": "[Rendered NextDNS Dashboard]"}'}
        ok, why = h._expected_call_ok([marker_free], step.expect, "testpid", alt_pid="plotpid")
        assert not ok
        assert "answered without ['$prefab']" in why

    def test_answered_accepts_an_empty_list_as_an_answer(self, h):
        """A fresh profile really does have an empty allowlist."""
        assert h._answered('{"data": []}')
        assert h._answered('{"content": ["image"]}')
        # Each published tool answers with its own document shape; the ok flag
        # on the record already excluded the calls that answered with an error.
        assert h._answered('{"name": "example.com", "addresses": ["93.184.216.34"]}')
        # Every write tool answers this way (verified against the live API).
        assert h._answered('{"success": true}')
        assert not h._answered("")
        assert not h._answered("{}")
        assert not h._answered("null")

    def test_every_step_declares_the_call_it_expects(self, h):
        """A step without an expectation would fall back to REST-only vacuity."""
        targets = h.TaskTargets(profile_name="TEST_PROFILE_X", tag="e2ehxyz789")
        steps = h.build_steps(targets)
        assert len(steps) >= 89
        assert [s.id for s in steps if not s.expect] == []
        for step in steps:
            if step.expect.get("endpoints"):
                assert step.expect["endpoints"] == [s for s in step.expect["endpoints"] if s]
            else:
                assert step.expect["tool"] == step.tool

    def test_config_wires_the_call_log_and_closes_the_subagent_channel(self, h, tmp_path, monkeypatch):
        """Observed live: the model delegated a step to a subagent."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        cfg = h.HarnessConfig.load(None)
        h.write_mcp_config(cfg, tmp_path)
        doc = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
        assert doc["mcp"]["nextdns"]["environment"]["NEXTDNS_CALL_LOG"] == str(tmp_path / h.CALL_LOG_NAME)
        assert doc["tools"]["subagent"] is False
        # The agent CLI's own meta-tools stay available: the point is that the
        # MCP server works in the client as it is, not that the client is
        # trimmed until the model cannot go anywhere else.
        assert "opencode*" not in doc["tools"]

    def test_feedback_shows_the_servers_own_record(self, h):
        step = h.Step("x", "profiles", "getProfile", "d", "p", "profile_exists")
        run = h.ActorRun(command=["x"])
        calls = [
            {
                "tool": "getDenylist",
                "endpoint": "GET /profiles/{profile_id}/denylist",
                "ok": False,
                "error": "boom",
            }
        ]
        out = h._feedback_block(step, run, "failed", "err", "", {}, server_calls=calls)
        assert any("SERVER" in ln and "GET /profiles/{profile_id}/denylist" in ln for ln in out)
        assert not any("SERVER" in ln for ln in h._feedback_block(step, run, "failed", "err", "", {}))


class TestStepPrompts:
    """Prompts state the task unambiguously, and never say how to do it."""

    def test_prompts_never_name_an_mcp_tool(self, h, all_steps):
        for step in all_steps:
            for tool in h.allowed_tools():
                assert tool not in step.prompt, f"{step.id} prompt leaked {tool}"

    def test_rewrite_add_describes_a_mapping_not_an_arrow(self, h, all_steps):
        """'A -> B' read as a sequence: the model added the record, deleted it,
        and reported the task as done ('added and then deleted')."""
        step = next(s for s in all_steps if s.id == "rewrites.add.cname")
        assert "->" not in step.prompt
        assert "hostname 'ai-e2e-cname-t0.example.com'" in step.prompt
        assert "resolves to 'ai-e2e-target-t0.example.com'" in step.prompt

    def test_prompt_is_the_task_and_nothing_else(self, h, all_steps):
        """No role framing, no rules — the harness asks for an outcome."""
        step = next(s for s in all_steps if s.id == "profiles.delete")
        assert step.prompt == "Do the following in NextDNS:\n\nDelete the profile '{profile_id}'."

    def test_prompts_never_describe_the_agent_cli(self, h, all_steps):
        """No client-specific affordances in the task message.

        An earlier rule ("if a tool is reported as unknown, retry it once") was
        opencode's lazy MCP registration leaking into the prompt. The server's
        own instructions cover how its tools are found.
        """
        markers = (
            "execute",
            "tools.nextdns",
            "unknown tool",
            "not registered",
            "opencode",
            "search_tools",
            "validation agent",
            "Rules:",
        )
        for step in all_steps:
            for marker in markers:
                assert marker not in step.prompt, f"{step.id} prompt mentions {marker!r}"


class TestStepRange:
    def test_no_selection_selects_every_step(self, h, all_steps):
        sel = h.select_steps(all_steps)
        assert [s.id for _, s in sel] == [s.id for s in all_steps]
        assert sel[0][0] == 1 and sel[-1][0] == len(all_steps)

    def test_id_range_is_inclusive(self, h, all_steps):
        sel = h.select_steps(all_steps, "profiles.get-profiles.update")
        assert [s.id for _, s in sel] == ["profiles.get", "profiles.update"]

    def test_positions_are_the_ones_the_catalog_prints(self, h, all_steps):
        assert [s.id for _, s in h.select_steps(all_steps, "1-2")] == ["profiles.create", "profiles.list"]

    def test_a_bare_id_or_position_selects_one_step(self, h, all_steps):
        assert [s.id for _, s in h.select_steps(all_steps, "lists.allowlist.add")] == ["lists.allowlist.add"]
        assert [s.id for _, s in h.select_steps(all_steps, "1")] == ["profiles.create"]

    def test_positions_keep_full_run_numbering(self, h, all_steps):
        """A targeted report stays comparable with a full one: [89/89], not [1/1]."""
        assert h.select_steps(all_steps, "profiles.delete") == [(len(all_steps), all_steps[-1])]

    def test_comma_separated_selection_is_a_union_in_checklist_order(self, h, all_steps):
        sel = h.select_steps(all_steps, "profiles.delete,20-21,lists.privacy_natives.replace-36")
        assert [s.id for _, s in sel] == [
            "lists.allowlist.replace",
            "lists.allowlist.add",
            "lists.privacy_natives.replace",
            "lists.privacy_natives.remove",
            "profiles.delete",
        ]

    def test_overlapping_entries_collapse(self, h, all_steps):
        assert h.select_steps(all_steps, "1-3,2-4,3") == h.select_steps(all_steps, "1-4")

    def test_unknown_entry_is_rejected(self, h, all_steps):
        with pytest.raises(h.StepRangeError, match="not a step id"):
            h.select_steps(all_steps, "nope")
        with pytest.raises(h.StepRangeError, match="not a position"):
            h.select_steps(all_steps, str(len(all_steps) + 1))

    def test_reversed_range_is_rejected(self, h, all_steps):
        with pytest.raises(h.StepRangeError, match="comes before"):
            h.select_steps(all_steps, "profiles.delete-profiles.create")

    def test_harness_owns_lifecycle_only_when_the_selection_excludes_it(self, h, all_steps):
        full = h.select_steps(all_steps)
        assert h.needs_harness_setup(full) is False
        assert h.needs_harness_teardown(full) is False
        middle = h.select_steps(all_steps, "settings.general.read-logs.get")
        assert h.needs_harness_setup(middle) is True
        assert h.needs_harness_teardown(middle) is True
        create_only = h.select_steps(all_steps, "profiles.create-profiles.list")
        assert h.needs_harness_setup(create_only) is False
        assert h.needs_harness_teardown(create_only) is True

    def test_start_end_flags_fold_into_the_same_selection(self, h):
        cfg = h.HarnessConfig(start_step="logs.get", end_step="logs.clear")
        assert cfg.step_selection(89) == "logs.get-logs.clear"
        assert h.HarnessConfig(start_step="logs.get").step_selection(89) == "logs.get-89"
        assert h.HarnessConfig(end_step="logs.get").step_selection(89) == "1-logs.get"
        cfg = h.HarnessConfig(steps="20-21,profiles.delete", start_step="32", end_step="32")
        assert cfg.step_selection(89) == "20-21,profiles.delete,32-32"
        assert h.HarnessConfig().step_selection(89) == ""

    def test_catalog_lists_ids_and_the_positions_they_also_take(self, h, all_steps):
        text = h.render_step_catalog(all_steps)
        assert f"Step catalog ({len(all_steps)} steps)" in text
        entry = next(ln for ln in text.splitlines() if ln.split()[:2] == ["1", "profiles.create"])
        assert entry.split()[2] == "[profiles]"

    def test_report_records_harness_setup(self, h):
        r = h.Report(started_at=1.0, finished_at=2.0, setup="provisioned via createProfile")
        assert r.summary()["setup"] == "provisioned via createProfile"
