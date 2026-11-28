"""Unit tests for the E2E harness's run lifecycle and scratch isolation.

Covers the harness profile lifecycle, the step-range CLI, failure-mode
summaries, actor stream collection, scratch isolation and run-scoped safety
helpers, the creation-receipt gate, evidence-backed extraction, and the
never-delete id normalization. The rest of the harness suite lives in
``test_ai_e2e_harness.py``, ``test_ai_e2e_measure.py``,
``test_ai_e2e_safety.py``, and ``test_ai_e2e_steps.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

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


class TestHarnessProfileLifecycle:
    """Targeted setup/teardown calls the MCP tool, so the server's ACLs apply."""

    class _Result:
        def __init__(self, data=None, content=None, is_error=False):
            self.data = data
            self.structured_content = data
            self.content = content or []
            self.is_error = is_error

    def _fake_client(self, h, monkeypatch, result):
        """Replace fastmcp's Client so no server is spawned; return the capture."""
        captured: dict[str, object] = {}

        class FakeClient:
            def __init__(self, transport):
                captured["transport"] = transport

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def call_tool(self, tool, arguments):
                captured["tool"] = tool
                captured["arguments"] = arguments
                return result

        monkeypatch.setattr("fastmcp.Client", FakeClient)
        return captured

    def test_provision_creates_through_the_mcp_tool(self, h, monkeypatch):

        captured = self._fake_client(h, monkeypatch, self._Result(data={"data": {"id": "abc123"}}))
        cfg = h.HarnessConfig()
        cfg.server["env"] = {"NEXTDNS_API_KEY": "sk-test", "NEXTDNS_WRITABLE_PROFILES": "ALL"}
        targets = h.TaskTargets(profile_name="P", tag="t0")
        pid, note = asyncio.run(h._harness_provision_profile(cfg, targets))
        assert pid == "abc123"
        assert "createProfile" in note
        assert captured["tool"] == "createProfile"
        assert captured["arguments"] == {"name": "P"}
        # the same server spec (and ACL env) the actor's opencode.json gets
        assert captured["transport"].env["NEXTDNS_WRITABLE_PROFILES"] == "ALL"
        assert captured["transport"].args == ["-m", "nextdns_mcp.server"]

    def test_provision_failure_is_reported_not_raised(self, h, monkeypatch):

        self._fake_client(h, monkeypatch, self._Result(data={"error": "Write access denied"}))
        pid, note = asyncio.run(
            h._harness_provision_profile(h.HarnessConfig(), h.TaskTargets(profile_name="P", tag="t0"))
        )
        assert pid == ""
        assert "Write access denied" in note

    def test_teardown_reports_the_tool_error(self, h, monkeypatch):

        self._fake_client(h, monkeypatch, self._Result(data={"error": "notFound"}, is_error=True))
        err = asyncio.run(h._harness_delete_profile(h.HarnessConfig(), "abc123"))
        assert "notFound" in err

    def test_teardown_success_is_silent(self, h, monkeypatch):

        self._fake_client(h, monkeypatch, self._Result(data={"ok": True}))
        assert asyncio.run(h._harness_delete_profile(h.HarnessConfig(), "abc123")) == ""


class TestStepRangeCli:
    """--print-steps is how a user finds the selection terms; it needs no API key."""

    def test_print_steps_works_without_a_key(self, h, monkeypatch, capsys):
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        assert h.main(["--print-steps"]) == 0
        out = capsys.readouterr().out
        assert "Step catalog (" in out
        assert "profiles.delete" in out

    def test_bad_selection_prints_the_catalog_and_fails(self, h, monkeypatch, capsys):
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        assert h.main(["--steps", "20-21,not-a-step"]) == 2
        assert "Step catalog (" in capsys.readouterr().out

    def test_selection_flags_reach_the_config(self, h, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        seen: dict[str, object] = {}

        async def fake_run(api_key, config):
            seen["steps"] = config.steps
            seen["start"] = config.start_step
            seen["end"] = config.end_step
            return h.Report(started_at=1.0, finished_at=2.0)

        monkeypatch.setattr(h, "run_harness", fake_run)
        h.main(
            [
                "--steps",
                "20-21,profiles.delete",
                "--start-step",
                "logs.get",
                "--end-step",
                "99",
                "--quiet",
                "--report",
                str(tmp_path / "r.jsonl"),
            ]
        )
        assert seen == {"steps": "20-21,profiles.delete", "start": "logs.get", "end": "99"}


class TestFailureModeSummary:
    """The tuning loop fixes one failure mode at a time, so a report has to say
    which modes cost the most steps - not list 89 verdicts and leave the reader
    to group them."""

    def test_a_step_that_never_reached_the_server_is_its_own_mode(self, h):
        why = "server recorded no GET /profiles/{profile_id}/denylist call in this attempt"
        row = {
            "id": "lists.denylist.read",
            "status": "failed",
            "mcp_tool": "getDenylist",
            "error": why,
            "mcp_response": {
                "server_calls": [],
                "expected_call": {"ok": False, "why": why},
            },
        }
        modes = [mode for mode, _ in h._failure_modes(row)]
        assert modes == ["no call reached the server", "no matching call recorded"]
        # `error` repeats the transcript verdict here; counting it again would
        # overstate the mode.
        assert not [mode for mode in modes if mode.startswith("end state")]

    def test_a_stalled_step_is_not_counted_as_a_contract_miss(self, h):
        """`no call reached the server` reads like the server refused the call.

        When the harness knows the actor stalled - it retried and timed out -
        that is infrastructure noise, and grouping it with real misses sends the
        tuning loop to change the server for nothing.
        """
        why = "server recorded no PATCH /profiles/{profile_id}/denylist/{entry_id} call in this attempt"
        row = {
            "id": "lists.denylist.update",
            "status": "failed",
            "mcp_tool": "updateDenylistEntry",
            "error": why,
            "mcp_response": {
                "server_calls": [],
                "transients": ["stall: actor harness timed out after 300s"] * 2,
                "expected_call": {"ok": False, "why": why},
            },
        }
        modes = [mode for mode, _ in h._failure_modes(row)]
        assert "actor stalled without calling" in modes
        assert "no call reached the server" not in modes

    def test_repeated_rejections_in_one_step_cost_one_step(self, h):
        record = {
            "tool": "updatePrivacySettings",
            "ok": False,
            "error": (
                "ValidationError: 1 validation error for call[updatePrivacySettings]\n"
                "privacySettings\n  Missing required argument"
            ),
        }
        row = {
            "id": "settings.privacy.update",
            "status": "failed",
            "mcp_tool": "updatePrivacySettings",
            "error": "settings/privacy read-back does not match",
            "mcp_response": {"server_calls": [dict(record) for _ in range(3)], "expected_call": {"ok": True}},
        }
        modes = h._failure_modes(row)
        assert modes.count(("schema rejected argument 'privacySettings'", "updatePrivacySettings")) == 1
        assert ("end state: settings/privacy read-back does not match", "updatePrivacySettings") in modes

    def test_the_server_says_what_it_refused(self, h):
        rows = [
            (
                {
                    "tool": "removeFromDenylist",
                    "ok": False,
                    "error": (
                        "ValidationError: 1 validation error for call[removeFromDenylist]\n"
                        "entry_id\n  Missing required argument"
                    ),
                },
                "schema rejected argument 'entry_id'",
            ),
            (
                {
                    "tool": "getAnalyticsStatus",
                    "ok": False,
                    "error": "HTTP error in GET /x: Client error '400 Bad Request'",
                },
                "upstream returned 400",
            ),
        ]
        for record, expected in rows:
            row = {
                "id": "x",
                "status": "failed",
                "mcp_tool": record["tool"],
                "error": "",
                "mcp_response": {"server_calls": [record], "expected_call": {"ok": True}},
            }
            assert expected in [mode for mode, _ in h._failure_modes(row)]

    def test_the_summary_ranks_modes_and_names_the_steps(self, h, tmp_path):
        report = tmp_path / "r.jsonl"
        rows = [
            {
                "id": f"lists.item{i}.read",
                "status": "failed",
                "mcp_tool": "getDenylist",
                "error": "server recorded no GET /profiles/{profile_id}/denylist call in this attempt",
                "mcp_response": {
                    "server_calls": [],
                    "expected_call": {
                        "ok": False,
                        "why": "server recorded no GET /profiles/{profile_id}/denylist call in this attempt",
                    },
                },
            }
            for i in range(2)
        ] + [
            {
                "id": "settings.privacy.update",
                "status": "passed",
                "mcp_tool": "updatePrivacySettings",
                "error": "",
                "mcp_response": {
                    "server_calls": [{"tool": "updatePrivacySettings", "ok": True}],
                    "expected_call": {"ok": True},
                },
            }
        ]
        report.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        summary = h.summarize_reports([str(report)])
        assert "3 step(s) (1 passed, 2 failed" in summary
        line = next(ln for ln in summary.splitlines() if "no matching call recorded" in ln)
        assert line.startswith("  2  ")
        assert "lists.item0.read" in line and "lists.item1.read" in line

    def test_a_report_with_no_steps_says_so(self, h, tmp_path):
        report = tmp_path / "empty.jsonl"
        report.write_text('{"meta": "run"}\n', encoding="utf-8")
        assert "no step results" in h.summarize_reports([str(report)])


class TestActorStreamCollection:
    """A timed-out attempt used to lose every event the actor had produced."""

    def _config(self, h, script: str, timeout_s: float):
        cfg = h.HarnessConfig()
        cfg.command = ["python3", "-c", script]
        cfg.timeout_s = timeout_s
        cfg.model = "test/model"
        return cfg

    async def test_events_before_a_timeout_are_kept(self, h, tmp_path):
        script = (
            "import json,sys,time;"
            "print(json.dumps({'type':'tool_use','part':{'tool':'search_tools',"
            "'state':{'status':'completed','input':{'query':'denylist'},'output':'[]'}}}));"
            "sys.stdout.flush(); time.sleep(30)"
        )
        cfg = self._config(h, script, timeout_s=2)
        run = await h.run_actor(cfg, "do the thing", tmp_path, retries=0)
        assert "timed out" in run.error
        assert len(run.raw_lines) == 1, run.raw_lines
        assert [call.name for call in run.tool_calls] == ["search_tools"]

    async def test_a_clean_run_is_still_parsed_normally(self, h, tmp_path):
        script = (
            "import json,sys;"
            "print(json.dumps({'type':'tool_use','part':{'tool':'call_tool',"
            "'state':{'status':'completed','input':{},'output':'{}'}}}));"
            "print(json.dumps({'type':'text','part':{'text':'done'}}))"
        )
        cfg = self._config(h, script, timeout_s=30)
        run = await h.run_actor(cfg, "do the thing", tmp_path, retries=0)
        assert run.ok, run.error
        assert [call.name for call in run.tool_calls] == ["call_tool"]
        assert run.final_text == "done"

    async def test_a_long_event_line_is_captured_not_dropped(self, h, tmp_path):
        """A >64 KiB single-line event used to abort the drain silently.

        readline() on the default 64 KiB StreamReader limit raised
        LimitOverrunError, the drain gather swallowed the dead task, and the
        run reported zero captured events with an empty error.
        """
        script = (
            "import json;"
            "print(json.dumps({'type':'tool_use','part':{'tool':'call_tool',"
            "'state':{'status':'completed','input':{},'output':'x'*70000}}}));"
            "print(json.dumps({'type':'text','part':{'text':'done'}}))"
        )
        cfg = self._config(h, script, timeout_s=30)
        run = await h.run_actor(cfg, "do the thing", tmp_path, retries=0)
        assert run.ok, run.error
        assert len(run.raw_lines) == 2, [len(ln) for ln in run.raw_lines]
        assert [call.name for call in run.tool_calls] == ["call_tool"]
        assert run.final_text == "done"

    async def test_a_stalled_actor_is_still_a_stall(self, h, tmp_path):
        cfg = self._config(h, "import time; time.sleep(30)", timeout_s=2)
        run = await h.run_actor(cfg, "do the thing", tmp_path, retries=0)
        assert "timed out" in run.error
        assert run.tool_calls == []


class TestScratchIsolation:
    """Live-run forensics: downloadLogs keeps the CSV it downloads (the caller
    gets the path), and a retained-logs CSV is ~500 MB. Weeks of runs left
    ~6 GB of nextdns_logs_* dirs on /tmp — a tmpfs with a per-user quota —
    until EDQUOT failed a live download and degraded four later sessions.

    The startup sweep of every ``nextdns_logs_*`` directory in the system temp
    that reclaimed that space was removed: a downloadLogs in flight from
    another server instance (the product keeps the CSV by design) had its
    directory deleted mid-download. Scratch isolation now owns only what the
    run itself creates, and removes it at exit.
    """

    def test_prepare_scratch_moves_tmpdir_to_home(self, h, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(h.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("TMPDIR", "preset")  # snapshot + restore TMPDIR around the test

        old_cached = tempfile.tempdir
        scratch = h._prepare_scratch_dir()
        try:
            assert scratch.is_dir()
            assert home in scratch.parents
            assert os.environ["TMPDIR"] == str(scratch)
            assert tempfile.tempdir == str(scratch)
        finally:
            tempfile.tempdir = old_cached
            h.shutil.rmtree(scratch, ignore_errors=True)

    def test_prepare_scratch_leaves_other_instances_downloads_alone(self, h, tmp_path, monkeypatch):
        """A download from another server instance is not this run's to delete."""
        old_tmp = tmp_path / "oldtmp"
        in_flight = old_tmp / "nextdns_logs_abc123"
        in_flight.mkdir(parents=True)
        home = tmp_path / "home"
        monkeypatch.setattr(h.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("TMPDIR", "preset")

        old_cached = tempfile.tempdir
        scratch = h._prepare_scratch_dir()
        try:
            assert in_flight.exists()
            assert in_flight.parent == old_tmp
        finally:
            tempfile.tempdir = old_cached
            h.shutil.rmtree(scratch, ignore_errors=True)

    def test_no_startup_sweep_touches_other_runs_files(self, h, tmp_path, monkeypatch):
        """No global temp sweep may come back: it deletes other processes' files.

        Pinned as behaviour, not structure: another server instance's in-flight
        download sits beside this run's scratch; after the run prepares its
        scratch and the run's finally removes it again, the foreign file must
        still be there. (This used to be ``not hasattr(h, '_sweep_stale_download_dirs')``,
        which only proved a name was gone, not that no sweep ran.)
        """
        old_tmp = tmp_path / "oldtmp"
        in_flight = old_tmp / "nextdns_logs_abc123"
        in_flight.mkdir(parents=True)
        home = tmp_path / "home"
        monkeypatch.setattr(h.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("TMPDIR", "preset")

        old_cached = tempfile.tempdir
        scratch = h._prepare_scratch_dir()
        try:
            marker = in_flight / "partial.csv"
            marker.write_text("another run's partial download", encoding="utf-8")
            h.shutil.rmtree(scratch, ignore_errors=True)
            assert marker.read_text(encoding="utf-8") == "another run's partial download"
        finally:
            tempfile.tempdir = old_cached
            h.shutil.rmtree(scratch, ignore_errors=True)


class TestRunScopedSafetyHelpers:
    """The cleanup/permissions helpers the re-review asked for, pinned directly."""

    def test_protect_server_profile_is_additive_and_idempotent(self, h):
        cfg = h.HarnessConfig()
        cfg.server["env"] = {"NEXTDNS_PROTECTED_PROFILES": "AAAAAA"}
        h._protect_server_profile(cfg, "bbbbbb")
        h._protect_server_profile(cfg, "BBBBBB")
        ids = set(cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"].lower().split(","))
        assert ids == {"aaaaaa", "bbbbbb"}

    def test_harness_artifacts_are_written_private(self, h, tmp_path):
        import os
        import stat

        path = tmp_path / "report.jsonl"
        h.common.write_private_text(path, '{"x": 1}\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        os.chmod(path, 0o644)
        h.common.write_private_text(path, '{"x": 2}\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_read_new_calls_reuses_the_server_offset_reader(self, h, tmp_path):
        path = tmp_path / "calls.jsonl"
        path.write_text('{"tool": "one"}\n{"tool": "two"}\n', encoding="utf-8")
        first = h._read_new_calls(path, 0)
        assert [r["tool"] for r in first] == ["one", "two"]
        offset = len(b'{"tool": "one"}\n')
        assert [r["tool"] for r in h._read_new_calls(path, offset)] == ["two"]
        assert h._read_new_calls(tmp_path / "missing.jsonl", 0) == []


class TestCreationReceiptGate:
    """Adoption and teardown require a create receipt; a name is never proof."""

    def test_name_lookup_without_receipt_is_refused(self, h):
        # A profile answering the run's exact name, created by nobody in this
        # run (operator-pinned name, a concurrent run, or the actor's own
        # createProfile under a different step): never adopted.
        assert h._proven_created_id({"id": "pid-foreign", "name": "TEST_PROFILE_20261102_ab12"}, set()) == ""
        assert h._proven_created_id({"id": "pid-foreign"}, {"pid-someone-else"}) == ""

    def test_name_lookup_backed_by_a_receipt_is_adopted(self, h):
        assert h._proven_created_id({"id": "pid-ours"}, {"pid-ours"}) == "pid-ours"
        assert h._proven_created_id(None, {"pid-ours"}) == ""

    def test_teardown_delete_requires_membership(self, h):
        assert h._deletable_by_this_run("pid-ours", {"pid-ours"})
        assert not h._deletable_by_this_run("pid-foreign", {"pid-ours"})
        assert not h._deletable_by_this_run("", {"pid-ours"})


def test_write_private_text_refuses_when_the_mode_cannot_be_established(h, tmp_path, monkeypatch):
    """No 0600, no artifact (fail closed, and no double-close on the way out)."""
    import os

    def boom(fd: int, mode: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(os, "fchmod", boom)
    path = tmp_path / "report.jsonl"
    with pytest.raises(PermissionError):
        h.common.write_private_text(path, "secret\n")
    assert not path.exists() or path.stat().st_size == 0


class TestEvidenceBackedExtraction:
    """Receipts and identities read from the evidence column, not the lossy result."""

    def test_created_id_comes_from_evidence_when_result_is_a_truncated_head(self, h):
        row = {
            "endpoint": "POST /profiles",
            "ok": True,
            # The result column: a truncation envelope whose head never
            # reaches data.id - json.loads succeeds, the id is simply absent.
            "result": '{"truncated": true, "head": "{\\"data\\": {\\"nam"}',
            "evidence": {"created_id": "pid-created-77"},
        }
        assert h._created_ids_from_records([row]) == {"pid-created-77"}

    def test_created_id_falls_back_to_the_result_for_older_rows(self, h):
        row = {"endpoint": "POST /profiles", "ok": True, "result": '{"data": {"id": "pid-old"}}'}
        assert h._created_ids_from_records([row]) == {"pid-old"}

    def test_identity_pairs_beyond_the_projection_still_map(self, h):
        """A list row projected to five items still yields all evidence identities."""
        row = {
            "endpoint": "GET /profiles/{profile_id}/denylist",
            "ok": True,
            "result": '{"data": [{"id": "id-one-aaaa", "name": "entry-one"}]}',
            "evidence": {
                "ids": [
                    {"id": "id-six-bbbb", "name": "entry-six"},
                    {"id": "id-seven-cccc", "name": "entry-seven"},
                ]
            },
        }
        ident = h._identifiers_from_answers([row])
        assert ident["entry-six"] == ["id-six-bbbb"]
        assert ident["entry-seven"] == ["id-seven-cccc"]
        assert ident["entry-one"] == ["id-one-aaaa"]


class TestSelfHealRequiresConfirmedDeletion:
    """Only a 404 proves the profile is gone; indeterminate reads must not create."""

    def test_a_503_read_does_not_trigger_a_recreate(self, h):

        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1": {"status": 503, "ok": False, "body": "boom"}})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid1"
        assert m.posted == [], "a 503 is not proof of deletion; POSTing would duplicate the account"

    def test_a_malformed_read_does_not_trigger_a_recreate(self, h):

        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1": None})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid1"
        assert m.posted == []


def test_never_delete_ids_use_the_server_normalization(h, monkeypatch):
    """Upper-case env entries match the lower-case ids the sweep compares."""
    monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "Home12 , PLOT99,")
    monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "Plot99")
    assert h._never_delete_ids() == {"home12", "plot99"}
