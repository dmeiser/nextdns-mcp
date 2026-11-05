"""Unit tests for the pure (network-free) helpers of the E2E harness.

These tests exercise the result classification, report aggregation, and REST
read-back helpers without touching the network or the live MCP server. The
harness is a plain script (not an installed package), so it is loaded directly
by file path.

SPDX-License-Identifier: MIT
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import ClassVar

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ai_e2e_harness.py"


@pytest.fixture(scope="module")
def h():
    """Import the harness module by path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("ai_e2e_harness", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ai_e2e_harness"] = module
    spec.loader.exec_module(module)
    return module


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
        import os
        import subprocess
        import sys

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
        for key in ("profiles", "settings", "lists", "rewrites", "analytics", "doh", "logs", "plots"):
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
        assert not hasattr(h, "_actor_contract_text")


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
        self.posted: list[tuple[str, dict]] = []

    async def rest_get(self, path, params=None):
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
        import asyncio

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
        import asyncio

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

    def test_self_heal_noop_when_profile_alive(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        import asyncio

        m = _FakeClient(by_path={"/profiles/pid1": {"ok": True, "body": {"data": {"id": "pid1"}}}})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid1"
        assert m.posted == []  # alive -> no POST

    def test_self_heal_recreates_deleted_profile(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        import asyncio

        # id 404s (deleted mid-run); POST recreates it and returns a new id.
        m = _FakeClient(by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid-new"
        assert m.posted == [("/profiles", {"name": "P-renamed"})]  # renamed name

    def test_self_heal_refuses_adopt_by_name_when_create_answered_no_id(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        import asyncio

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
        import asyncio

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
        assert plot["result"] == ["image"]

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
        import asyncio

        m = _FakeClient(
            by_path={"/profiles": {"ok": True, "body": {"data": [{"id": "a1", "name": "One"}, {"id": "b2"}]}}}
        )
        assert asyncio.run(h._all_profile_ids(m)) == {"a1": "One", "b2": ""}

    def test_all_profile_ids_is_none_when_the_listing_fails(self, h):
        """An empty mapping from a failed read looked like an empty account."""
        import asyncio

        m = _FakeClient(by_path={"/profiles": {"status": 503, "ok": False, "body": "boom"}})
        assert asyncio.run(h._all_profile_ids(m)) is None

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
        import asyncio

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
        import asyncio

        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(
            by_path={"/profiles": {"ok": True, "body": {"data": [{"id": "other", "name": "Someone else's"}]}}}
        )
        asyncio.run(h._sweep_stray_profiles(m, set(), set()))
        assert m.deleted == []

    def test_sweep_never_touches_protected_or_plot_profiles(self, h, monkeypatch):
        import asyncio

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
        import asyncio

        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(by_path={"/profiles": {"ok": True, "body": {"data": []}}})
        note = asyncio.run(h._sweep_stray_profiles(m, {"gone1"}, set()))
        assert m.deleted == []
        assert note == ""

    def test_sweep_stands_down_when_the_listing_fails(self, h, monkeypatch):
        """Account state uncertain -> delete nothing, and say so in the report."""
        import asyncio

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
        import asyncio

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
        import asyncio

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "home12")
        # Both readable profiles are excluded: one is the write target of the
        # run, the other is on the denylist. No probe may pick them.
        m = _FakeClient(profiles=[{"id": "testpid", "name": "P"}, {"id": "home12", "name": "Home"}])
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert "NEXTDNS_PLOT_PROFILE" not in os.environ
        assert "no readable profile" in note

    def test_a_configured_plot_profile_is_left_alone(self, h, monkeypatch):
        import asyncio

        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "aa11aa")
        m = _FakeClient()
        assert asyncio.run(h._ensure_plot_profile(m, "testpid")) == ""
        assert os.environ["NEXTDNS_PLOT_PROFILE"] == "aa11aa"

    def test_a_failed_listing_says_so_and_sets_nothing(self, h, monkeypatch):
        import asyncio

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        m = _FakeClient(by_path={"/profiles": {"status": 503, "ok": False, "body": "boom"}})
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert "NEXTDNS_PLOT_PROFILE" not in os.environ
        assert "listing failed" in note

    def test_no_data_bearing_profile_skips_with_an_honest_note(self, h, monkeypatch):
        import asyncio

        monkeypatch.delenv("NEXTDNS_PLOT_PROFILE", raising=False)
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        m = _FakeClient(
            profiles=[{"id": "other", "name": "Other"}],
            by_path={"/profiles/other/analytics/status": {"ok": True, "body": {"data": []}}},
        )
        note = asyncio.run(h._ensure_plot_profile(m, "testpid"))
        assert "NEXTDNS_PLOT_PROFILE" not in os.environ
        assert "data-starved" in note


# ===========================================================================
# secret handling: the key travels as a reference, never a value (SEC-4)
# =========================================================================== #


class TestSecretHandling:
    def test_api_base_reads_env_at_call_time(self, h, monkeypatch):
        """Resolved when used, not at import: main() loads dotenv after importing."""
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://api.example.test/base/")
        assert h.api_base() == "https://api.example.test/base"
        monkeypatch.delenv("NEXTDNS_API_BASE")
        assert h.api_base() == "https://api.nextdns.io"

    def test_config_server_env_uses_the_api_base_as_loaded(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://late.example")
        cfg = h.HarnessConfig.load(None)
        assert cfg.server["env"]["NEXTDNS_API_BASE"] == "https://late.example"

    def test_redacted_server_env_masks_an_inlined_key(self, h):
        srv = {"env": {"NEXTDNS_API_KEY": "sk-inline-9f3a", "NEXTDNS_TOOL_SEARCH": "off"}}
        out = h._redacted_server_env(srv)
        assert out["NEXTDNS_API_KEY"] == h.ENV_REF_API_KEY
        assert out["NEXTDNS_TOOL_SEARCH"] == "off"
        assert "sk-inline-9f3a" not in json.dumps(out)
        assert h._redacted_server_env({}) == {}
        assert h._redacted_server_env({"env": {}}) == {}

    def test_expand_env_refs_resolves_against_the_process_environment(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-real-value")
        out = h._expand_env_refs({"NEXTDNS_API_KEY": h.ENV_REF_API_KEY, "NEXTDNS_TOOL_SEARCH": "off"})
        assert out == {"NEXTDNS_API_KEY": "sk-real-value", "NEXTDNS_TOOL_SEARCH": "off"}
        monkeypatch.delenv("NEXTDNS_API_KEY")
        assert h._expand_env_refs({"K": "{env:NEXTDNS_API_KEY}"}) == {"K": ""}

    def test_render_argv_does_not_rescan_substituted_values(self, h):
        """One pass: a placeholder token inside a VALUE must stay literal.

        The prompt is model-facing text; a prompt that spells {server_env}
        would otherwise have the server environment — including the API key —
        expanded into the rendered command line through it.
        """
        argv = h.render_argv(
            ["harness", "{prompt}", "{server_env}"],
            {"{prompt}": "type {server_env} verbatim", "{server_env}": '{"NEXTDNS_API_KEY": "x"}'},
        )
        assert argv == ["harness", "type {server_env} verbatim", '{"NEXTDNS_API_KEY": "x"}']

    def test_run_actor_renders_the_server_env_redacted(self, h, monkeypatch, tmp_path):
        """The {server_env} placeholder that reaches argv never carries the key."""

        async def go():
            cfg = h.HarnessConfig()
            cfg.server["env"] = {"NEXTDNS_API_KEY": "sk-inline-9f3a"}
            captured: dict = {}

            def fake_render(self, values):
                captured.update(values)
                return []  # empty argv -> run_actor stops before spawning anything

            monkeypatch.setattr(h.HarnessConfig, "render_argv", fake_render)
            run = await h.run_actor(cfg, "prompt text", tmp_path)
            assert run.error  # the command-not-found path proves nothing ran
            env_json = captured[h.PLACEHOLDER_SERVER_ENV]
            assert "sk-inline-9f3a" not in env_json
            assert h.ENV_REF_API_KEY in env_json

        import asyncio

        asyncio.run(go())


class TestHttpsBaseRequired:
    """An http base must be refused before the API key is sent anywhere (SEC)."""

    def test_http_base_is_refused_before_any_request(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.example.test")
        calls: list[str] = []
        monkeypatch.setattr("httpx.get", lambda url, **kw: calls.append(url))
        with pytest.raises(ValueError, match="https://"):
            h.HarnessConfig.load(None)
        # The probe never fired: the key never left for a plaintext host.
        assert calls == []

    def test_https_base_proceeds_to_the_probe(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://api.example.test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        calls: list[str] = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"id": "pre22"}]}

        def _get(url, **kw):
            calls.append(url)
            return _Resp()

        monkeypatch.setattr("httpx.get", _get)
        cfg = h.HarnessConfig.load(None)
        assert calls == ["https://api.example.test/profiles"]
        assert cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"] == "pre22"


class TestProfileProtectionProbeFailsClosed:
    """A probe that cannot establish the pre-existing set must abort the run."""

    def test_probe_timeout_aborts_the_config_load(self, h, monkeypatch):
        import httpx

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)

        def _boom(url, **kw):
            raise httpx.ConnectTimeout("timed out reaching the API")

        monkeypatch.setattr("httpx.get", _boom)
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)

    def test_probe_http_500_aborts_instead_of_continuing(self, h, monkeypatch):
        import httpx

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)

        class _Resp:
            status_code = 500

            def raise_for_status(self):
                raise httpx.HTTPStatusError("500 Internal Server Error", request=None, response=None)

            def json(self):  # pragma: no cover - raise_for_status fires first
                return {}

        monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)

    def test_probe_unparseable_body_aborts(self, h, monkeypatch):
        import json

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                raise json.JSONDecodeError("bad", "", 0)

        monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())
        with pytest.raises(h.HarnessAbortError, match="profile-protection probe failed"):
            h.HarnessConfig.load(None)

    def test_probe_without_a_key_is_a_no_op(self, h, monkeypatch):
        monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
        calls: list[str] = []
        monkeypatch.setattr("httpx.get", lambda url, **kw: calls.append(url))
        h.HarnessConfig.load(None)
        assert calls == []

    def test_write_mcp_config_redacts_a_key_inlined_by_a_config_file(self, h, tmp_path):
        """--config may inline a key; the generated opencode.json still may not."""
        cfg = h.HarnessConfig()
        cfg.server["env"] = {"NEXTDNS_API_KEY": "sk-inline-9f3a"}
        out = h.write_mcp_config(cfg, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "sk-inline-9f3a" not in text
        assert "{env:NEXTDNS_API_KEY}" in text


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

    def test_list_update_requires_the_entry_read_back_inactive(self, h):
        import asyncio

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
        import asyncio

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
        import asyncio

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
        import asyncio

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
        import asyncio

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
        import asyncio

        self._fake_client(h, monkeypatch, self._Result(data={"error": "Write access denied"}))
        pid, note = asyncio.run(
            h._harness_provision_profile(h.HarnessConfig(), h.TaskTargets(profile_name="P", tag="t0"))
        )
        assert pid == ""
        assert "Write access denied" in note

    def test_teardown_reports_the_tool_error(self, h, monkeypatch):
        import asyncio

        self._fake_client(h, monkeypatch, self._Result(data={"error": "notFound"}, is_error=True))
        err = asyncio.run(h._harness_delete_profile(h.HarnessConfig(), "abc123"))
        assert "notFound" in err

    def test_teardown_success_is_silent(self, h, monkeypatch):
        import asyncio

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
                "88",
                "--quiet",
                "--report",
                str(tmp_path / "r.jsonl"),
            ]
        )
        assert seen == {"steps": "20-21,profiles.delete", "start": "logs.get", "end": "88"}


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

        scratch = h._prepare_scratch_dir()
        try:
            assert scratch.is_dir()
            assert home in scratch.parents
            assert os.environ["TMPDIR"] == str(scratch)
        finally:
            h.shutil.rmtree(scratch, ignore_errors=True)

    def test_prepare_scratch_leaves_other_instances_downloads_alone(self, h, tmp_path, monkeypatch):
        """A download from another server instance is not this run's to delete."""
        old_tmp = tmp_path / "oldtmp"
        in_flight = old_tmp / "nextdns_logs_abc123"
        in_flight.mkdir(parents=True)
        home = tmp_path / "home"
        monkeypatch.setattr(h.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("TMPDIR", "preset")

        scratch = h._prepare_scratch_dir()
        try:
            assert in_flight.exists()
            assert in_flight.parent == old_tmp
        finally:
            h.shutil.rmtree(scratch, ignore_errors=True)

    def test_the_startup_sweep_is_gone(self, h):
        """No global temp sweep may come back: it deletes other processes' files."""
        assert not hasattr(h, "_sweep_stale_download_dirs")


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
        import asyncio

        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1": {"status": 503, "ok": False, "body": "boom"}})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid1"
        assert m.posted == [], "a 503 is not proof of deletion; POSTing would duplicate the account"

    def test_a_malformed_read_does_not_trigger_a_recreate(self, h):
        import asyncio

        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1": None})
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid1"
        assert m.posted == []


def test_never_delete_ids_use_the_server_normalization(h, monkeypatch):
    """Upper-case env entries match the lower-case ids the sweep compares."""
    monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "Home12 , PLOT99,")
    monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "Plot99")
    assert h._never_delete_ids() == {"home12", "plot99"}
