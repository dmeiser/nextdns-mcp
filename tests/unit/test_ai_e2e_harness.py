"""Unit tests for the pure (network-free) helpers of the E2E harness.

These tests exercise the result classification, report aggregation, and REST
read-back helpers without touching the network or the live MCP server. The
harness is a plain script (not an installed package), so it is loaded directly
by file path. Measurement tests live in ``test_ai_e2e_measure.py``, secret
handling and protection probes in ``test_ai_e2e_safety.py``, the step
machinery in ``test_ai_e2e_steps.py``, and the run lifecycle in
``test_ai_e2e_run.py``.

SPDX-License-Identifier: MIT
"""

import json
import os
import re
import sys
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


# =========================================================================== #
# result classification
# =========================================================================== #


class TestCheckFromCall:
    def test_passed_on_clean_structured_payload(self, h):
        r = h.check_from_call(
            "c1",
            "lists",
            "desc",
            mcp_tool="getDenylist",
            mcp_response={"structured": {"data": []}, "is_error": False},
        )
        assert r.status == "passed"
        assert r.id == "c1" and r.section == "lists" and r.mcp_tool == "getDenylist"

    def test_failed_on_tool_exception(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"_tool_error": "boom", "_tool_error_type": "RuntimeError"},
        )
        assert r.status == "failed"
        assert "RuntimeError" in r.error and "boom" in r.error

    def test_failed_on_error_body(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"structured": {"error": "Write access denied"}, "is_error": False},
        )
        assert r.status == "failed"
        assert "Write access denied" in r.error

    def test_failed_on_raised_http_error(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"structured": None, "is_error": True, "text": "HTTP error 403 ..."},
        )
        assert r.status == "failed"
        assert "raised" in r.error.lower()

    def test_skipped_on_skip_reason(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"structured": {"skip_reason": "no query history"}, "is_error": False},
        )
        assert r.status == "skipped"
        assert "no query history" in r.reason

    def test_skipped_on_unsupported_structured(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={
                "structured": {"unsupported": True, "detail": "this operation is not available"},
                "is_error": False,
            },
        )
        assert r.status == "skipped"
        assert "not available" in r.reason

    def test_carries_request_and_response(self, h):
        req = {"list_type": "allowlist", "operation": "add", "entry": "a.com"}
        resp = {"structured": {"data": []}, "is_error": False}
        r = h.check_from_call(
            "c1",
            "lists",
            "d",
            mcp_tool="getDenylist",
            mcp_request=req,
            mcp_response=resp,
            rest_verification={"verified": True},
            duration_ms=12,
        )
        assert r.mcp_request == req
        assert r.mcp_response is resp
        assert r.rest_verification == {"verified": True}
        assert r.duration_ms == 12
        d = r.to_dict()
        assert d["id"] == "c1" and d["status"] == "passed" and d["duration_ms"] == 12


# =========================================================================== #
# detector helpers
# =========================================================================== #


class TestDetectors:
    def test_extract_tool_error(self, h):
        assert h._extract_tool_error({"_tool_error": "x", "_tool_error_type": "E"}) is not None
        assert h._extract_tool_error({"structured": {}}) is None
        assert h._extract_tool_error("not a dict") is None

    def test_structured(self, h):
        assert h._structured({"structured": 5}) == 5
        assert h._structured({"nope": 1}) is None
        assert h._structured(None) is None

    def test_is_error_body(self, h):
        assert h._is_error_body({"structured": {"error": "x"}})
        assert not h._is_error_body({"structured": {"error": ""}})
        assert not h._is_error_body({"structured": {"data": []}})

    def test_error_body_text(self, h):
        assert h._error_body_text({"structured": {"error": "denied"}}) == "denied"
        assert h._error_body_text({"structured": "raw"}) == "raw"

    def test_is_skip(self, h):
        assert h._is_skip({"structured": {"skip_reason": "r"}})
        assert h._is_skip({"structured": {"skipped": True}})
        assert not h._is_skip({"structured": {"data": []}})

    def test_is_not_supported(self, h):
        assert h._is_not_supported({"structured": {"unsupported": True}})
        assert h._is_not_supported({"structured": {"detail": "unsupported thing"}})
        assert h._is_not_supported({"is_error": True, "text": "Unsupported feature"})
        assert not h._is_not_supported({"structured": {"data": []}})

    def test_skip_reason(self, h):
        assert h._skip_reason({"structured": {"skip_reason": "r"}}) == "r"
        assert h._skip_reason({"structured": {"skipped": True, "reason": "x"}}) == "x"
        assert h._skip_reason({"structured": {"unsupported": True, "detail": "d"}}) == "d"

    def test_not_supported_reason(self, h):
        assert h._not_supported_reason({"structured": {"detail": "d"}}) == "d"
        assert h._not_supported_reason({"structured": {"unsupported": True}}) == "unsupported"

    def test_is_raised_error(self, h):
        assert h._is_raised_error({"is_error": True, "text": "boom"})
        assert not h._is_raised_error({"is_error": False, "text": "ok"})
        assert not h._is_raised_error({"is_error": True, "text": ""})
        assert not h._is_raised_error("not a dict")

    def test_raised_error_text_truncates(self, h):
        assert h._raised_error_text({"text": "x" * 600}) == "x" * 500
        assert h._raised_error_text("not a dict") == ""


# =========================================================================== #
# REST read-back helpers
# =========================================================================== #


class TestRestHelpers:
    def test_resp_status(self, h):
        assert h._resp_status({"status": 200}) == 200
        assert h._resp_status("x") is None

    def test_verify_settings_assert_match_toplevel(self, h):
        assert h._verify_settings_assert({"ok": True, "body": {"web3": True}}, {"web3": True})

    def test_verify_settings_assert_match_nested(self, h):
        assert h._verify_settings_assert({"ok": True, "body": {"data": {"web3": True}}}, {"web3": True})

    def test_verify_settings_assert_multi(self, h):
        body = {"ecs": True, "cacheBoost": True}
        assert h._verify_settings_assert({"ok": True, "body": body}, {"ecs": True, "cacheBoost": True})

    def test_verify_settings_assert_mismatch(self, h):
        assert not h._verify_settings_assert({"ok": True, "body": {"ecs": False}}, {"ecs": True})

    def test_verify_settings_assert_bad_response(self, h):
        assert not h._verify_settings_assert({"ok": False, "body": {}}, {"web3": True})
        assert not h._verify_settings_assert({"ok": True, "body": "str"}, {"web3": True})

    def test_list_values(self, h):
        tag = "abc"
        assert h._list_values("allowlist", tag) == [f"ai-e2e-allow-{tag}.example.com"]
        assert h._list_values("denylist", tag) == [f"ai-e2e-deny-{tag}.example.com"]
        assert h._list_values("privacy_blocklists", tag) == ["nextdns-recommended"]
        assert h._list_values("privacy_natives", tag) == ["alexa"]
        assert h._list_values("security_tlds", tag) == ["zip"]
        assert h._list_values("parental_categories", tag) == ["gambling"]
        assert h._list_values("parental_services", tag) == ["tiktok"]
        with pytest.raises(ValueError):
            h._list_values("nope", tag)

    def test_list_added(self, h):
        assert h._list_added("allowlist", "abc") == "ai-e2e-allowlist-add-abc.example.com"
        assert h._list_added("denylist", "abc") == "ai-e2e-denylist-add-abc.example.com"
        assert h._list_added("security_tlds", "abc") is None

    def test_list_contains(self, h):
        resp = {"ok": True, "body": {"data": [{"id": "a.com", "active": True}]}}
        assert h._list_contains(resp, ["a.com"])
        assert not h._list_contains(resp, ["b.com"])
        assert not h._list_contains({"ok": False, "body": {}}, ["a.com"])

    def test_list_absent(self, h):
        resp = {"ok": True, "body": {"data": [{"id": "a.com"}]}}
        assert h._list_absent(resp, ["b.com"]) == (True, h.ABSENT)
        assert h._list_absent(resp, ["a.com"]) == (False, h.PRESENT)
        # A failed read proves nothing: a 503 used to be reported as absence
        # and made a passing deletion out of a cleanup nobody observed.
        absent, verdict = h._list_absent({"ok": False, "status": 503}, ["a.com"])
        assert absent is False
        assert verdict.startswith(h.INDETERMINATE)
        # A malformed body proves nothing either; a 404 on the collection is the
        # strongest absence the API offers.
        assert h._list_absent({"ok": True, "body": "not-a-list"}, ["a.com"])[0] is False
        assert h._list_absent({"ok": False, "status": 404}, ["a.com"]) == (True, h.ABSENT)

    def test_entry_ids(self, h):
        resp = {"ok": True, "body": {"data": [{"id": "a"}, {"id": "b"}, {"x": 1}]}}
        assert h._entry_ids(resp) == ["a", "b"]
        assert h._entry_ids({"ok": False}) == []
        assert h._entry_ids({"ok": True, "body": "str"}) == []

    def test_rewrites_contains(self, h):
        resp = {"ok": True, "body": {"data": [{"name": "a.com", "content": "1.2.3.4"}]}}
        assert h._rewrites_contains(resp, ["a.com"])
        assert not h._rewrites_contains(resp, ["b.com"])
        assert not h._rewrites_contains({"ok": False, "body": {}}, ["a.com"])

    def test_analytics_has_data_positive(self, h):
        assert h._analytics_has_data({"ok": True, "body": {"queries": 42}})
        assert h._analytics_has_data({"ok": True, "body": {"data": [{"k": 1}]}})
        assert h._analytics_has_data({"ok": True, "body": {"total": 3}})

    def test_analytics_has_data_negative(self, h):
        assert not h._analytics_has_data({"ok": True, "body": {"queries": 0}})
        assert not h._analytics_has_data({"ok": True, "body": {"data": [{"k": 0}]}})
        assert not h._analytics_has_data("x")

    def test_random_tag_and_profile_name(self, h):
        tag = h._random_tag(5)
        assert tag.startswith("e2eh") and len(tag) == 9  # "e2eh" + 5
        name = h.make_profile_name()
        assert name.startswith("TEST_PROFILE_")

        assert re.fullmatch(r"TEST_PROFILE_\d{8}_[0-9a-f]{4}", name) is not None
        # The name is run-unique WITHIN a process (two resolutions must agree)
        # and differs BETWEEN processes: that is what scopes destructive
        # harness cleanup to what the run proved it created.
        assert h.make_profile_name() == name
        import subprocess

        other = subprocess.run(
            [sys.executable, "-c", "from scripts.ai_e2e_common import make_profile_name; print(make_profile_name())"],
            check=True,
            capture_output=True,
            text=True,
            cwd=os.path.dirname(h.__file__).removesuffix("/scripts"),
        ).stdout.strip()
        assert other != name

    def test_list_specs_shape(self, h):
        assert len(h._LIST_SPECS) == 7
        names = [s["name"] for s in h._LIST_SPECS]
        assert names == [
            "allowlist",
            "denylist",
            "privacy_blocklists",
            "privacy_natives",
            "security_tlds",
            "parental_categories",
            "parental_services",
        ]
        updatable = {s["name"] for s in h._LIST_SPECS if s.get("updatable")}
        assert updatable == {"allowlist", "denylist", "parental_categories", "parental_services"}
        bl = next(s for s in h._LIST_SPECS if s["name"] == "privacy_blocklists")
        assert bl.get("restore") == "nextdns-recommended"

    def test_metric_lists(self, h):
        assert len(h.AGGREGATE_METRICS) == 11
        assert "domains" in h.AGGREGATE_METRICS and "destinations" in h.AGGREGATE_METRICS
        assert len(h.SERIES_METRICS) == 10
        assert "domains" not in h.SERIES_METRICS
        assert len(h.PLOT_METRICS) == 9
        assert set(h.PLOT_METRICS) == set(h.SERIES_METRICS) - {"destinations"}


# =========================================================================== #
# Report aggregation
# =========================================================================== #


class TestReport:
    def _report(self, h, statuses):
        rep = h.Report(started_at=0.0, api_key_present=True)
        for i, st in enumerate(statuses):
            rep.results.append(h.CheckResult(id=f"c{i}", section="s", description="d", status=st))
        return rep

    def test_counts(self, h):
        rep = self._report(h, ["passed", "skipped", "failed", "passed"])
        assert rep.passed == 2 and rep.skipped == 1 and rep.failed == 1 and rep.total == 4
        assert rep.verdict == "FAIL"

    def test_verdict_pass_when_no_failures(self, h):
        rep = self._report(h, ["passed", "skipped"])
        assert rep.verdict == "PASS"

    def test_summary_shape(self, h):
        rep = self._report(h, ["passed", "skipped", "failed"])
        rep.profile_id = "abc123"
        rep.profile_name = "Home"
        rep.finished_at = 1.5
        s = rep.summary()
        assert s["verdict"] == "FAIL"
        assert s["profile_id"] == "abc123"
        assert s["total"] == 3
        assert s["duration_ms"] == int(1.5 * 1000)

    def test_check_result_to_dict_keys(self, h):
        r = h.CheckResult(id="x", section="y", description="z", status="passed")
        d = r.to_dict()
        for key in (
            "id",
            "section",
            "description",
            "status",
            "mcp_tool",
            "mcp_request",
            "mcp_response",
            "rest_verification",
            "reason",
            "error",
            "duration_ms",
        ):
            assert key in d


# =========================================================================== #
# env + report rendering + skip report
# =========================================================================== #


class TestEnvAndRender:
    def test_render_text_report(self, h):
        rep = h.Report(started_at=0.0, api_key_present=True)
        rep.results.append(h.CheckResult(id="a", section="s", description="d", status="passed"))
        rep.results.append(h.CheckResult(id="b", section="s", description="d", status="failed", error="boom"))
        rep.results.append(h.CheckResult(id="c", section="s", description="d", status="skipped", reason="why"))
        rep.finished_at = 1.0
        out = h.render_text_report(rep)
        assert "VERDICT : FAIL" in out
        assert "[PASS] a" in out and "[FAIL] b" in out and "[SKIP] c" in out
        assert "error: boom" in out and "reason: why" in out

    def test_write_report_stdout(self, h, capsys):
        rep = self._mk(h)
        h.write_report(rep, "-")
        out = capsys.readouterr().out
        assert '"summary"' in out
        assert '"passed"' in out

    def test_write_report_file(self, h, tmp_path):
        rep = self._mk(h)
        p = tmp_path / "sub" / "rep.jsonl"
        h.write_report(rep, str(p))
        lines = p.read_text().strip().splitlines()
        assert lines[-1].startswith('{"summary"')
        assert json.loads(lines[0])["id"] == "a"

    def test_write_skip_report(self, h, tmp_path):
        p = tmp_path / "skip.jsonl"
        h.write_skip_report(str(p))
        assert json.loads(p.read_text())["verdict"] == "SKIP"

    @staticmethod
    def _mk(h):
        rep = h.Report(started_at=0.0, api_key_present=True, finished_at=0.1)
        rep.results.append(h.CheckResult(id="a", section="s", description="d", status="passed"))
        return rep

    def test_section_definitions_cover_checklist(self, h):
        for key in ("profiles", "settings", "lists", "rewrites", "analytics", "doh", "logs", "plots", "dashboard"):
            assert key in h.SECTIONS

    def test_settings_categories_seven(self, h):
        assert len(h.SETTINGS_CATEGORIES) == 7
        for cat, spec in h.SETTINGS_CATEGORIES.items():
            for field in ("path", "set", "assert", "restore", "assert_restore"):
                assert field in spec, f"{cat} missing {field}"
        assert h.SETTINGS_CATEGORIES["general"]["path"] == "/settings"
        assert h.SETTINGS_CATEGORIES["blockpage"]["path"] == "/settings/blockPage"


# =========================================================================== #
# actor event parsing (harness-agnostic, best-effort)
# =========================================================================== #


class TestParseActorEvents:
    def test_opencode_tool_use_events(self, h):
        lines = [
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "nextdns_getProfile",
                        "state": {"status": "completed", "input": {"operation": "list"}, "output": "ok"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "execute",
                        "state": {
                            "status": "completed",
                            "input": {"code": 'await tools.nextdns["getAnalyticsStatus"]({ metric: "status" })'},
                            "output": "{}",
                        },
                    },
                }
            ),
            json.dumps({"type": "text", "part": {"text": "All done."}}),
        ]
        calls, final = h.parse_actor_events(lines)
        assert len(calls) == 2
        # opencode names MCP tools nextdns_<tool>; the prefix is normalised.
        assert calls[0].name == "getProfile" and calls[0].ok
        assert calls[0].args == {"operation": "list"}
        # an execute wrapper is attributed to the underlying MCP tool from its code.
        assert calls[1].name == "getAnalyticsStatus"
        assert final == "All done."

    def test_execute_wrapper_dot_form(self, h):
        lines = [
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "execute",
                        "state": {
                            "status": "completed",
                            "input": {"code": "const r = await tools.nextdns.getProfile({ operation: 'create' });"},
                            "output": "{}",
                        },
                    },
                }
            ),
        ]
        calls, _ = h.parse_actor_events(lines)
        # the dot form tools.nextdns.<tool> is also attributed to the MCP tool
        assert calls[0].name == "getProfile"

    def test_execute_wrapper_without_mcp_reference(self, h):
        lines = [
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "execute",
                        "state": {"status": "completed", "input": {"code": "const x = 1"}, "output": "1"},
                    },
                }
            ),
        ]
        calls, _ = h.parse_actor_events(lines)
        assert calls[0].name == "execute"  # falls back to the wrapper name

    def test_error_state_marks_call_failed(self, h):
        lines = [
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "nextdns_dohLookup",
                        "state": {"status": "error", "input": {"domain": "example.com"}, "output": "boom"},
                    },
                }
            ),
        ]
        calls, _ = h.parse_actor_events(lines)
        assert calls[0].name == "dohLookup"
        assert not calls[0].ok

    def test_claude_stream_json_events(self, h):
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Working on it."},
                            {
                                "type": "tool_use",
                                "name": "mcp__nextdns__getProfile",
                                "input": {"operation": "create", "name": "P"},
                            },
                        ],
                    },
                }
            ),
            json.dumps({"type": "result", "result": "Done creating the profile."}),
        ]
        calls, final = h.parse_actor_events(lines)
        assert len(calls) == 1 and calls[0].name == "mcp__nextdns__getProfile"
        assert calls[0].args == {"operation": "create", "name": "P"}
        assert final == "Done creating the profile."

    def test_ignores_non_event_and_bad_json_lines(self, h):
        lines = [
            "",
            "not json",
            json.dumps({"type": "step_finish"}),
            json.dumps(
                {"type": "tool_use", "part": {"tool": "nextdns_plotAnalytics", "state": {"status": "completed"}}}
            ),
        ]
        calls, _ = h.parse_actor_events(lines)
        assert len(calls) == 1 and calls[0].name == "plotAnalytics"

    def test_empty_stream(self, h):
        calls, final = h.parse_actor_events([])
        assert calls == [] and final == ""


# =========================================================================== #
# harness config + command template rendering
# =========================================================================== #


class TestHarnessConfig:
    def test_as_argv_from_list_and_string(self, h):
        assert h._as_argv(["opencode", "run"]) == ["opencode", "run"]
        assert h._as_argv("opencode run --standalone") == ["opencode", "run", "--standalone"]
        assert h._as_argv(None) == h.DEFAULT_HARNESS_COMMAND
        assert h._as_argv("") == h.DEFAULT_HARNESS_COMMAND

    def test_defaults_and_server_env(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_API_BASE", raising=False)
        monkeypatch.delenv("NEXTDNS_E2E_HARNESS_CMD", raising=False)
        monkeypatch.delenv("NEXTDNS_MCP_PYTHON", raising=False)
        cfg = h.HarnessConfig.load(None)
        # opencode is the documented default harness (MCP-native).
        assert cfg.command[0] == "opencode"
        assert h.PLACEHOLDER_PROMPT in cfg.command
        assert cfg.model == ""
        # the API key goes to the MCP server's env as a `{env:...}` reference,
        # never the value: this env is serialized into the actor's argv through
        # the {server_env} placeholder and the run report.
        assert cfg.server["env"]["NEXTDNS_API_KEY"] == h.ENV_REF_API_KEY
        assert cfg.server["env"]["NEXTDNS_API_BASE"] == h.api_base()
        assert cfg.server["args"] == ["-m", "nextdns_mcp.server"]
        assert "PYTHONPATH" in cfg.server["env"]

    def test_plot_profile_auto_protected(self, h, monkeypatch):
        """NEXTDNS_PLOT_PROFILE is added to the server's write denylist."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "aa11aa")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        cfg = h.HarnessConfig.load(None)
        assert cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"] == "aa11aa"

    def test_plot_profile_merges_existing_protected_list(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "aa11aa")
        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "ff66ff")
        cfg = h.HarnessConfig.load(None)
        ids = set(cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"].split(","))
        assert ids == {"ff66ff", "aa11aa"}

    def test_plot_profile_not_duplicated_in_protected_list(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "aa11aa")
        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "aa11aa")
        cfg = h.HarnessConfig.load(None)
        ids = cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"].split(",")
        assert [p.lower() for p in ids].count("aa11aa") == 1

    def test_env_overrides_command_and_server(self, h, monkeypatch):
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        monkeypatch.setenv("NEXTDNS_E2E_HARNESS_CMD", "claude -p --mcp-config /tmp/m.json")
        monkeypatch.setenv("NEXTDNS_E2E_MODEL", "sonnet")
        monkeypatch.setenv("NEXTDNS_MCP_PYTHON", "/usr/bin/python3.14")
        monkeypatch.setenv("NEXTDNS_MCP_ARGS", "-m nextdns_mcp.server")
        cfg = h.HarnessConfig.load(None)
        assert cfg.command == ["claude", "-p", "--mcp-config", "/tmp/m.json"]
        assert cfg.model == "sonnet"
        assert cfg.server["command"] == "/usr/bin/python3.14"
        assert cfg.server["args"] == ["-m", "nextdns_mcp.server"]

    def test_json_config_layers_over_defaults(self, h, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        cfg_file = tmp_path / "run.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "command": ["myharness", "--mcp", "{config_file}", "{prompt}"],
                    "model": "my-model",
                    "prompt": "custom task",
                    "timeout_s": 120,
                    "server": {"command": "/bin/x", "args": ["-m", "y"], "env": {"FOO": "bar"}},
                }
            )
        )
        cfg = h.HarnessConfig.load(str(cfg_file))
        assert cfg.model == "my-model" and cfg.prompt == "custom task"
        assert cfg.timeout_s == 120
        assert cfg.server["command"] == "/bin/x" and cfg.server["args"] == ["-m", "y"]
        assert cfg.server["env"]["FOO"] == "bar"
        assert cfg.server["env"]["NEXTDNS_API_KEY"] == h.ENV_REF_API_KEY  # merged, not dropped

    def test_render_argv_substitutes_placeholders(self, h):
        argv = h.render_argv(
            ["opencode", "run", "--model", "{model}", "{prompt}"],
            {"{model}": "m1", "{prompt}": "DO THE TASK"},
        )
        assert argv == ["opencode", "run", "--model", "m1", "DO THE TASK"]

    def test_render_argv_drops_empty_flag_pair(self, h):
        # An unset model must not leave a dangling --model flag behind.
        argv = h.render_argv(
            ["opencode", "run", "--model", "{model}", "{prompt}"],
            {"{model}": "", "{prompt}": "DO THE TASK"},
        )
        assert argv == ["opencode", "run", "DO THE TASK"]

    def test_render_argv_drops_bare_empty_tokens(self, h):
        argv = h.render_argv(["harness", "{maybe_flag}", "{prompt}"], {"{maybe_flag}": "", "{prompt}": "T"})
        assert argv == ["harness", "T"]

    def test_render_argv_keeps_flag_that_is_not_a_prefix(self, h):
        # A placeholder rendered empty between two real tokens keeps the rest.
        argv = h.render_argv(
            ["harness", "{p1}", "-x", "y", "{p2}"],
            {"{p1}": "", "{p2}": "Z"},
        )
        assert argv == ["harness", "-x", "y", "Z"]

    def test_render_argv_all_placeholders(self, h, tmp_path):
        values = {
            h.PLACEHOLDER_PROMPT: "P",
            h.PLACEHOLDER_MODEL: "M",
            h.PLACEHOLDER_WORKDIR: str(tmp_path),
            h.PLACEHOLDER_CONFIG_FILE: str(tmp_path / "opencode.json"),
            h.PLACEHOLDER_SERVER_COMMAND: "py -m nextdns_mcp.server",
            h.PLACEHOLDER_SERVER_ENV: '{"K": "v"}',
        }
        argv = h.render_argv(
            ["h", "{prompt}", "{model}", "{workdir}", "{config_file}", "{server_command}", "{server_env}"],
            values,
        )
        assert argv[0] == "h"
        assert "P" in argv and "M" in argv
        assert str(tmp_path) in argv
        assert "py -m nextdns_mcp.server" in argv
        assert '{"K": "v"}' in argv


class TestToolEventNameResolution:
    """Tool-name extraction, including progressive-disclosure proxies."""

    def test_direct_execute_call_resolves_tool(self, h):
        part = {
            "tool": "execute",
            "state": {"input": {"code": "await tools.nextdns.getProfile({operation: 'list'})"}},
        }
        assert h._tool_event_name(part) == "getProfile"

    def test_call_tool_proxy_resolves_proxied_name(self, h):
        part = {
            "tool": "execute",
            "state": {
                "input": {"code": "await tools.nextdns.call_tool({name: 'getDenylist', arguments: {operation: 'get'}})"}
            },
        }
        assert h._tool_event_name(part) == "getDenylist"

    def test_search_tools_stays_discovery(self, h):
        part = {"tool": "execute", "state": {"input": {"code": "await tools.nextdns.search_tools({query: 'x'})"}}}
        assert h._tool_event_name(part) == "search_tools"

    def test_first_class_mcp_attempt_prefix_stripped(self, h):
        part = {"tool": "tools.nextdns.search_tools", "state": {"input": {}, "status": "error"}}
        assert h._tool_event_name(part) == "search_tools"


class TestFeedbackBlockAttempts:
    """Verbose feedback surfaces failed/unknown tool attempts (bm25 debugging)."""

    def _run(self, h):
        run = h.ActorRun(command=["x"])
        run.tool_calls = [
            h.ToolCall(
                name="create_profile",
                args={"code": "tools.nextdns.create_profile(...)"},
                is_error=True,
                result_text="Unknown tool",
            ),
            h.ToolCall(
                name="session_rename",
                args={"code": "tools.opencode.session_rename(...)"},
                is_error=True,
                result_text="no such session",
            ),
        ]
        return run

    def test_verbose_shows_attempts(self, h):
        step = h.Step("x", "profiles", "getProfile", "d", "p", "profile_exists")
        out = h._feedback_block(step, self._run(h), "failed", "err", "", {}, verbose=True)
        attempts = [ln for ln in out if ln.strip().startswith("ATTEMPT")]
        assert len(attempts) == 2
        assert any("create_profile" in ln for ln in attempts)
        assert any("session_rename" in ln for ln in attempts)

    def test_quiet_omits_attempts(self, h):
        step = h.Step("x", "profiles", "getProfile", "d", "p", "profile_exists")
        out = h._feedback_block(step, self._run(h), "failed", "err", "", {}, verbose=False)
        assert not any(ln.strip().startswith("ATTEMPT") for ln in out)

    def test_successful_non_catalog_call_does_not_crash(self, h):
        """Regression: a *succeeded* call with a non-catalog name must not raise.

        The live crash was ``list | frozenset`` (TypeError) reached only when
        ``is_error`` was False, which the other tests short-circuited past.
        """
        step = h.Step("x", "profiles", "getProfile", "d", "p", "profile_exists")
        run = h.ActorRun(command=["x"])
        run.tool_calls = [
            h.ToolCall(name="session_rename", args={"code": "tools.opencode.session_rename(...)"}),  # ok=True
            h.ToolCall(name="execute", args={"code": "1+1"}),  # builtin, ok
        ]
        out = h._feedback_block(step, run, "passed", "", "", {}, verbose=True)
        attempts = [ln for ln in out if ln.strip().startswith("ATTEMPT")]
        assert len(attempts) == 1
        assert "session_rename" in attempts[0]
        assert "not an advertised tool" in attempts[0]

    def test_discovery_calls_shown_when_no_direct_mcp_calls(self, h):
        step = h.Step("x", "profiles", "getProfile", "d", "p", "profile_exists")
        run = h.ActorRun(command=["x"])
        run.tool_calls = [h.ToolCall(name="search_tools", args={"code": "search_tools({query:'profile'})"})]
        out = h._feedback_block(step, run, "passed", "", "", {})
        assert any("search_tools" in ln for ln in out)
        assert not any("no MCP operation observed" in ln for ln in out)


# =========================================================================== #
# generated MCP client config (opencode.json)
# =========================================================================== #


class TestMcpConfigGeneration:
    def test_write_mcp_config_renders_local_stdio_server(self, h, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        cfg = h.HarnessConfig.load(None)
        out = h.write_mcp_config(cfg, tmp_path)
        assert out == tmp_path / "opencode.json"
        doc = json.loads(out.read_text(encoding="utf-8"))
        srv = doc["mcp"]["nextdns"]
        assert srv["type"] == "local"
        assert srv["command"][0] == cfg.server["command"]
        assert srv["command"][1:] == cfg.server["args"]
        assert srv["cwd"] == cfg.server["cwd"]
        assert srv["environment"]["NEXTDNS_API_KEY"] == "{env:NEXTDNS_API_KEY}"
        assert srv["environment"]["NEXTDNS_API_BASE"] == h.api_base()
        assert srv["enabled"] is True

    def test_the_config_carries_no_inline_api_key(self, h, tmp_path, monkeypatch):
        """The workdir file must never hold the real key.

        A live run showed the model reads ``opencode.json`` with opencode's
        built-in read tool; whatever this file contains reaches the model
        endpoint. The key must therefore travel only as an ``{env:...}``
        reference that opencode expands from the actor's own environment.
        """
        secret = "sk-live-secret-9f3a"
        monkeypatch.setenv("NEXTDNS_API_KEY", secret)
        cfg = h.HarnessConfig.load(None)
        out = h.write_mcp_config(cfg, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert secret not in text
        assert json.loads(text)["mcp"]["nextdns"]["environment"]["NEXTDNS_API_KEY"] == "{env:NEXTDNS_API_KEY}"

    def test_filesystem_browsing_builtins_are_disabled(self, h, tmp_path):
        """Nothing outside the nextdns tools may touch the workdir or network.

        A live run burned a step browsing the workdir (`read` on the directory
        listing and on this config) instead of calling the contract, and read
        the config back into model context. Browsing and fetching tools are off
        alongside the write/exec ones so the model's only move is the MCP call.
        """
        cfg = h.HarnessConfig.load(None)
        doc = json.loads(h.write_mcp_config(cfg, tmp_path).read_text(encoding="utf-8"))
        tools = doc["tools"]
        assert tools["nextdns*"] is True
        for name in (
            "read",
            "glob",
            "grep",
            "list",
            "webfetch",
            "execute",
            "shell",
            "bash",
            "write",
            "edit",
            "patch",
            "subagent",
            "task",
            "websearch",
            "search",
            "question",
            "opencode_*",
            "experimental_*",
        ):
            assert tools[name] is False

    def test_codemode_off_registers_the_tools_natively(self, h, tmp_path):
        """`codemode: false` is opencode's per-server opt-out of Code Mode.

        With it the server's tools are native `nextdns_<tool>` tools rather than
        JavaScript bindings inside `execute`. Under progressive disclosure that
        is `nextdns_search_tools` and `nextdns_call_tool`, so the disclosure
        contract stays what is measured - only the client-side JS evaluation in
        front of it goes away.
        """
        cfg = h.HarnessConfig.load(None)
        assert cfg.codemode is False  # off by default; `--codemode on` restores opencode's default
        doc = json.loads(h.write_mcp_config(cfg, tmp_path).read_text(encoding="utf-8"))
        assert doc["mcp"]["nextdns"]["codemode"] is False

        cfg.codemode = True
        doc = json.loads(h.write_mcp_config(cfg, tmp_path).read_text(encoding="utf-8"))
        assert doc["mcp"]["nextdns"]["codemode"] is True

    def test_the_codemode_flag_reaches_the_config(self, h, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        seen: dict[str, object] = {}

        async def fake_run(api_key, config):
            seen["codemode"] = config.codemode
            return h.Report(started_at=1.0, finished_at=2.0)

        monkeypatch.setattr(h, "run_harness", fake_run)
        h.main(["--codemode", "on", "--quiet", "--report", str(tmp_path / "r.jsonl")])
        assert seen == {"codemode": True}
        seen.clear()
        h.main(["--quiet", "--report", str(tmp_path / "r2.jsonl")])
        assert seen == {"codemode": False}

    def test_write_mcp_config_is_valid_json_schema_shaped(self, h, tmp_path):
        cfg = h.HarnessConfig.load(None)
        out = h.write_mcp_config(cfg, tmp_path)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert "$schema" in doc
        # The file must parse back cleanly (it is read by opencode at startup).
        assert isinstance(doc["mcp"], dict)

    def test_write_mcp_config_injects_nothing_model_facing(self, h, tmp_path):
        """The generated config is client plumbing: no prompt text, no instructions file.

        How the server should be used is the server's own business — it publishes
        that through the MCP ``initialize`` instructions, its tool descriptions
        and its usage prompt/resource. The harness sends tasks and nothing else,
        so it cannot tell the model how to behave inside a particular agent CLI.
        """
        cfg = h.HarnessConfig.load(None)
        out = h.write_mcp_config(cfg, tmp_path)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert "instructions" not in doc
        assert not (tmp_path / "actor_contract.md").exists()


# =========================================================================== #
# task targets + prompt
# =========================================================================== #


class TestTaskTargets:
    def test_renamed_name(self, h):
        t = h.TaskTargets(profile_name="AI E2E Test Profile X", tag="abc")
        assert t.renamed_name == "AI E2E Test Profile X-renamed"

    def test_list_entries_cover_all_seven_types(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        entries = t.list_entries
        assert set(entries) == {
            "allowlist",
            "denylist",
            "privacy_blocklists",
            "privacy_natives",
            "security_tlds",
            "parental_categories",
            "parental_services",
        }
        assert entries["allowlist"] == "ai-e2e-allow-abc.example.com"
        assert entries["denylist"] == "ai-e2e-deny-abc.example.com"

    def test_rewrite_targets(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        assert t.rewrite_name == "ai-e2e-a-abc.example.com"
        assert t.rewrite_content == "192.0.2.100"
