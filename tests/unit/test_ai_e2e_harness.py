"""Unit tests for the pure (network-free) helpers of the E2E harness.

These tests exercise the result classification, report aggregation, and REST
read-back helpers without touching the network or the live MCP server. The
harness is a plain script (not an installed package), so it is loaded directly
by file path.

SPDX-License-Identifier: MIT
"""

import importlib.util
import json
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


# =========================================================================== #
# result classification
# =========================================================================== #


class TestCheckFromCall:
    def test_passed_on_clean_structured_payload(self, h):
        r = h.check_from_call(
            "c1",
            "lists",
            "desc",
            mcp_tool="manageLists",
            mcp_response={"structured": {"data": []}, "is_error": False},
        )
        assert r.status == "passed"
        assert r.id == "c1" and r.section == "lists" and r.mcp_tool == "manageLists"

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
            mcp_tool="manageLists",
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
        assert h._list_absent(resp, ["b.com"])
        assert not h._list_absent(resp, ["a.com"])
        assert h._list_absent({"ok": False}, ["a.com"])

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
        import re

        assert re.fullmatch(r"TEST_PROFILE_\d{8}", name) is not None
        # date-based name is deterministic within a day
        assert h.make_profile_name() == h.make_profile_name()

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
                        "tool": "nextdns_manageProfiles",
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
                            "input": {"code": 'await tools.nextdns["queryAnalytics"]({ metric: "status" })'},
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
        assert calls[0].name == "manageProfiles" and calls[0].ok
        assert calls[0].args == {"operation": "list"}
        # an execute wrapper is attributed to the underlying MCP tool from its code.
        assert calls[1].name == "queryAnalytics"
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
                            "input": {"code": "const r = await tools.nextdns.manageProfiles({ operation: 'create' });"},
                            "output": "{}",
                        },
                    },
                }
            ),
        ]
        calls, _ = h.parse_actor_events(lines)
        # the dot form tools.nextdns.<tool> is also attributed to the MCP tool
        assert calls[0].name == "manageProfiles"

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
                                "name": "mcp__nextdns__manageProfiles",
                                "input": {"operation": "create", "name": "P"},
                            },
                        ],
                    },
                }
            ),
            json.dumps({"type": "result", "result": "Done creating the profile."}),
        ]
        calls, final = h.parse_actor_events(lines)
        assert len(calls) == 1 and calls[0].name == "mcp__nextdns__manageProfiles"
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
        # the API key goes to the MCP server's env, not the actor.
        assert cfg.server["env"]["NEXTDNS_API_KEY"] == "sk-test"
        assert cfg.server["env"]["NEXTDNS_API_BASE"] == h.API_BASE
        assert cfg.server["args"] == ["-m", "nextdns_mcp.server"]
        assert "PYTHONPATH" in cfg.server["env"]

    def test_plot_profile_auto_protected(self, h, monkeypatch):
        """NEXTDNS_PLOT_PROFILE is added to the server's write denylist."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "23228d")
        monkeypatch.delenv("NEXTDNS_PROTECTED_PROFILES", raising=False)
        cfg = h.HarnessConfig.load(None)
        assert cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"] == "23228d"

    def test_plot_profile_merges_existing_protected_list(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "23228d")
        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "2c2b8f")
        cfg = h.HarnessConfig.load(None)
        ids = set(cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"].split(","))
        assert ids == {"2c2b8f", "23228d"}

    def test_plot_profile_not_duplicated_in_protected_list(self, h, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_PLOT_PROFILE", "23228d")
        monkeypatch.setenv("NEXTDNS_PROTECTED_PROFILES", "23228d")
        cfg = h.HarnessConfig.load(None)
        ids = cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"].split(",")
        assert [p.lower() for p in ids].count("23228d") == 1

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
        assert cfg.server["env"]["NEXTDNS_API_KEY"] == "sk-test"  # merged, not dropped

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
            "state": {"input": {"code": "await tools.nextdns.manageProfiles({operation: 'list'})"}},
        }
        assert h._tool_event_name(part) == "manageProfiles"

    def test_call_tool_proxy_resolves_proxied_name(self, h):
        part = {
            "tool": "execute",
            "state": {
                "input": {"code": "await tools.nextdns.call_tool({name: 'manageLists', arguments: {operation: 'get'}})"}
            },
        }
        assert h._tool_event_name(part) == "manageLists"

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
        step = h.Step("x", "profiles", "manageProfiles", "d", "p", "profile_exists")
        out = h._feedback_block(step, self._run(h), "failed", "err", "", {}, verbose=True)
        attempts = [ln for ln in out if ln.strip().startswith("ATTEMPT")]
        assert len(attempts) == 2
        assert any("create_profile" in ln for ln in attempts)
        assert any("session_rename" in ln for ln in attempts)

    def test_quiet_omits_attempts(self, h):
        step = h.Step("x", "profiles", "manageProfiles", "d", "p", "profile_exists")
        out = h._feedback_block(step, self._run(h), "failed", "err", "", {}, verbose=False)
        assert not any(ln.strip().startswith("ATTEMPT") for ln in out)

    def test_successful_non_catalog_call_does_not_crash(self, h):
        """Regression: a *succeeded* call with a non-catalog name must not raise.

        The live crash was ``list | frozenset`` (TypeError) reached only when
        ``is_error`` was False, which the other tests short-circuited past.
        """
        step = h.Step("x", "profiles", "manageProfiles", "d", "p", "profile_exists")
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
        step = h.Step("x", "profiles", "manageProfiles", "d", "p", "profile_exists")
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
        assert srv["environment"]["NEXTDNS_API_KEY"] == "sk-test"
        assert srv["environment"]["NEXTDNS_API_BASE"] == h.API_BASE
        assert srv["enabled"] is True

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

    def test_build_task_prompt_embeds_all_targets(self, h):
        t = h.TaskTargets(profile_name="AI E2E Test Profile 123", tag="abc")
        p = h.build_task_prompt(t)
        assert "AI E2E Test Profile 123" in p
        assert "AI E2E Test Profile 123-renamed" in p
        assert "manageProfiles" in p and "dohLookup" in p
        for cat in h.SETTINGS_CATEGORIES:
            assert cat in p
        # Anchored (not bare-substring) matches: the hostnames must appear as
        # whole tokens, which also keeps CodeQL's incomplete-url-substring-
        # sanitization rule quiet.
        for host in ("ai-e2e-allow-abc.example.com", "ai-e2e-a-abc.example.com"):
            assert re.search(rf"(?<![A-Za-z0-9.-]){re.escape(host)}(?![A-Za-z0-9.-])", p)
        assert "nextdns-recommended" in p
        # The harness owns cleanup; the actor must not delete the profile.
        assert "Do NOT delete the test profile" in p


# =========================================================================== #
# coverage + measurement checks (pure; no network)
# =========================================================================== #


def _run_with(*calls):
    import ai_e2e_harness as hh

    r = hh.ActorRun(ok=True, exit_code=0, tool_calls=list(calls))
    return r


class TestCoverage:
    def test_coverage_passed_when_observed(self, h):
        run = _run_with(h.ToolCall(name="dohLookup", args={}))
        r = h._coverage_check(run, "dohLookup")
        assert r.status == "passed"

    def test_coverage_skipped_when_unobserved(self, h):
        run = _run_with()
        r = h._coverage_check(run, "plotAnalytics")
        assert r.status == "skipped"

    def test_coverage_counts_calls(self, h):
        run = _run_with(h.ToolCall(name="dohLookup"), h.ToolCall(name="dohLookup"))
        r = h._coverage_check(run, "dohLookup")
        assert r.status == "passed" and r.mcp_request == {"calls": 2}


class TestReadOnlyCoverage:
    def test_skipped_when_unobserved(self, h):
        run = _run_with()
        r = h._readonly_coverage(run, "queryAnalytics", "analytics", "analytics.query", "desc")
        assert r.status == "skipped" and r.section == "analytics" and r.id == "analytics.query"

    def test_passed_when_observed(self, h):
        run = _run_with(h.ToolCall(name="queryAnalytics", args={"metric": "status"}))
        r = h._readonly_coverage(run, "queryAnalytics", "analytics", "analytics.query", "desc")
        assert r.status == "passed" and r.mcp_request == {"calls": 1}


class TestMeasureActorRun:
    def test_passed_on_clean_exit(self, h):
        run = h.ActorRun(ok=True, exit_code=0, tool_calls=[h.ToolCall(name="x")])
        rep = h.Report(started_at=0.0, api_key_present=True)
        h._measure_actor_run(run, rep)
        assert rep.results[0].status == "passed"

    def test_failed_on_nonzero_exit(self, h):
        run = h.ActorRun(ok=False, exit_code=1, tool_calls=[], error="boom")
        rep = h.Report(started_at=0.0, api_key_present=True)
        h._measure_actor_run(run, rep)
        assert rep.results[0].status == "failed"
        assert "boom" in rep.results[0].error


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
    def test_profile_provision_and_rename(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(profiles=[{"id": "pid1", "name": "P-renamed"}])
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        prof = asyncio.run(h._measure_profile(m, t, rep))
        assert prof is not None and rep.profile_id == "pid1"
        statuses = {r.id: r.status for r in rep.results}
        assert statuses["profiles.provision"] == "passed"
        assert statuses["profiles.rename"] == "passed"

    def test_profile_provision_fails_when_absent(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(profiles=[])
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        prof = asyncio.run(h._measure_profile(m, t, rep))
        assert prof is None
        assert rep.results[0].id == "profiles.provision" and rep.results[0].status == "failed"

    def test_settings_verifies_target_values(self, h):
        m = _FakeClient(by_path={"/profiles/pid1/settings": {"ok": True, "body": {"web3": True}}})
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._measure_settings(m, "pid1", rep))
        general = next(r for r in rep.results if r.id == "settings.general")
        assert general.status == "passed"
        # a category whose read-back lacks the target value fails
        privacy = next(r for r in rep.results if r.id == "settings.privacy")
        assert privacy.status == "failed"

    def test_lists_verify_target_entries(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        entries = t.list_entries
        by_path = {}
        for spec in h._LIST_SPECS:
            by_path[f"/profiles/pid1{spec['path']}"] = {
                "ok": True,
                "body": {"data": [{"id": entries[spec["name"]]}]},
            }
        m = _FakeClient(by_path=by_path)
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._measure_lists(m, "pid1", t, rep))
        assert all(r.status == "passed" for r in rep.results)

    def test_lists_fail_when_entry_missing(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1/allowlist": {"ok": True, "body": {"data": []}}})
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._measure_lists(m, "pid1", t, rep))
        allow = next(r for r in rep.results if r.id == "lists.allowlist")
        assert allow.status == "failed"

    def test_rewrites_verify_target_record(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1/rewrites": {"ok": True, "body": {"data": [{"name": t.rewrite_name}]}}})
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._measure_rewrites(m, "pid1", t, rep))
        assert rep.results[0].status == "passed"

    def test_rewrites_fail_when_record_missing(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(by_path={"/profiles/pid1/rewrites": {"ok": True, "body": {"data": []}}})
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._measure_rewrites(m, "pid1", t, rep))
        assert rep.results[0].status == "failed"

    def test_cleanup_deletes_and_verifies_gone(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(profiles=[])  # gone: neither name resolves
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._cleanup(m, "pid1", t, rep))
        assert m.deleted == ["/profiles/pid1"]
        r = next(x for x in rep.results if x.id == "profiles.cleanup")
        assert r.status == "passed" and rep.cleanup == "passed"

    def test_cleanup_failed_when_delete_errors(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        m = _FakeClient(profiles=[], delete_ok=False)
        rep = h.Report(started_at=0.0, api_key_present=True)
        import asyncio

        asyncio.run(h._cleanup(m, "pid1", t, rep))
        r = next(x for x in rep.results if x.id == "profiles.cleanup")
        assert r.status == "failed" and "FAILED" in rep.cleanup

    def test_profile_gone_logic(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        import asyncio

        # gone when nothing resolves
        m = _FakeClient(profiles=[])
        assert asyncio.run(h._profile_gone(m, "pid1", t)) is True
        # present under the id
        m2 = _FakeClient(by_path={"/profiles/pid1": {"ok": True, "body": {"data": {"id": "pid1"}}}})
        assert asyncio.run(h._profile_gone(m2, "pid1", t)) is False
        # present under the original name
        m3 = _FakeClient(profiles=[{"id": "pid1", "name": "P"}])
        assert asyncio.run(h._profile_gone(m3, "pid1", t)) is False
        # present under the renamed name
        m4 = _FakeClient(profiles=[{"id": "pid1", "name": "P-renamed"}])
        assert asyncio.run(h._profile_gone(m4, "pid1", t)) is False

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

    def test_self_heal_falls_back_to_find_profile(self, h):
        t = h.TaskTargets(profile_name="P", tag="abc")
        import asyncio

        # POST ok but body shape unhelpful -> recover the id by name lookup.
        m = _FakeClient(
            by_path={"/profiles/pid1": {"status": 404, "ok": False, "body": {}}},
            profiles=[{"id": "pid-lookup", "name": "P-renamed"}],
            post_result={"status": 201, "ok": True, "body": {}},
        )
        assert asyncio.run(h._self_heal_profile(m, "pid1", t)) == "pid-lookup"

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
            "mcp_tool": "manageLists",
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
            "mcp_tool": "manageLists",
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
        "tool": "manageRewrites",
        "operation": "delete",
        "args": [HOST],
        "result": [],
        "data": True,
    }

    def _records(self, entry_id: str) -> list[dict]:
        return [
            {
                "tool": "manageRewrites",
                "operation": "list",
                "ok": True,
                "write": False,
                "args": '{"profile_id": "4a6326"}',
                "result": self.LIST_RESULT,
            },
            {
                "tool": "manageRewrites",
                "operation": "delete",
                "ok": True,
                "write": True,
                "args": f'{{"entry_id": "{entry_id}", "profile_id": "4a6326"}}',
                "result": '{"success": true}',
            },
        ]

    def test_the_id_the_server_returned_for_the_name_satisfies_the_expected_argument(self, h):
        ok, why = h._expected_call_ok(self._records("c41a6xwk"), self.EXPECT, "4a6326")
        assert ok is True, why

    def test_the_hostname_still_satisfies_it(self, h):
        records = self._records("c41a6xwk")
        records[1]["args"] = f'{{"name": "{self.HOST}", "profile_id": "4a6326"}}'
        ok, why = h._expected_call_ok(records, self.EXPECT, "4a6326")
        assert ok is True, why

    def test_deleting_a_different_record_still_fails(self, h):
        """The equivalence is per name: y2fpqzs9 is the AAAA record, not this one."""
        ok, why = h._expected_call_ok(self._records("y2fpqzs9"), self.EXPECT, "4a6326")
        assert ok is False
        assert "was called without" in why

    def test_an_id_the_server_never_returned_for_that_name_does_not_count(self, h):
        records = self._records("c41a6xwk")
        records.pop(0)  # no list call in the attempt: nothing established the id
        ok, why = h._expected_call_ok(records, self.EXPECT, "4a6326")
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
            "mcp_tool": "manageLists",
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
                "mcp_tool": "manageProfiles",
                "error": "profile not found via REST",
                "mcp_response": {"server_calls": [], "expected_call": {"ok": False, "why": "no call"}},
            },
            {"summary": {"notes": ["self-heal: test profile was deleted mid-run; recreated as 2c2b8f"]}},
        ]
        report.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        summary = h.summarize_reports([str(report)])
        assert "RUN NOT A CLEAN MEASUREMENT" in summary
        assert "recreated as 2c2b8f" in summary

    def test_a_clean_run_carries_no_warning(self, h, tmp_path):
        report = tmp_path / "clean.jsonl"
        rows = [
            {
                "id": "profiles.list",
                "status": "passed",
                "mcp_tool": "manageProfiles",
                "error": "",
                "mcp_response": {
                    "server_calls": [{"tool": "manageProfiles", "ok": True}],
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
                "mcp_tool": "manageProfiles",
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
        run.tool_calls = [h.ToolCall(name="manageLists", args={})]
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
    """Profiles the actor creates beyond the test profile are removed at the end."""

    def test_all_profile_ids_maps_names(self, h):
        import asyncio

        m = _FakeClient(
            by_path={"/profiles": {"ok": True, "body": {"data": [{"id": "a1", "name": "One"}, {"id": "b2"}]}}}
        )
        assert asyncio.run(h._all_profile_ids(m)) == {"a1": "One", "b2": ""}

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
                            {"id": "real", "name": "Pumpkin Bum"},  # pre-existing
                            {"id": "test", "name": "P-renamed"},  # the test profile
                            {"id": "stray", "name": "new-profile"},  # actor-created
                        ]
                    },
                }
            }
        )
        asyncio.run(h._sweep_stray_profiles(m, {"real"}, {"test"}))
        assert m.deleted == ["/profiles/stray"]

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
        asyncio.run(h._sweep_stray_profiles(m, set(), set()))
        assert m.deleted == []
        assert h._never_delete_ids() == {"home12", "plot99"}


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
        path.write_text('{"tool": "manageProfiles", "ok": true}\n', encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"tool": "manageLists", "operation": "add", "ok": false}\n')
        assert h._read_new_calls(path, offset) == [{"tool": "manageLists", "operation": "add", "ok": False}]
        assert h._read_new_calls(tmp_path / "absent.jsonl", 0) == []

    def test_expected_call_requires_the_exact_successful_call(self, h):
        step = h.Step(
            "lists.denylist.add",
            "lists",
            "manageLists",
            "d",
            "p",
            "list_add",
            expect=h._expect("manageLists", "add", args=("denylist", "ads.example.com")),
        )
        right = {
            "tool": "manageLists",
            "operation": "add",
            "ok": True,
            "write": True,
            "args": '{"list_type": "denylist", "entry": "ads.example.com"}',
            "result": '{"data": []}',
        }
        assert h._expected_call_ok([right], step.expect, "pid")[0]
        # wrong operation, wrong entry, and a failed call all miss
        wrong_op = {**right, "operation": "replace"}
        wrong_arg = {**right, "args": '{"list_type": "denylist", "entry": "other.com"}'}
        failed = {**right, "ok": False}
        for record in (wrong_op, wrong_arg, failed, {"tool": "search_tools", "ok": True}):
            assert not h._expected_call_ok([record], step.expect, "pid")[0]
        assert not h._expected_call_ok([], step.expect, "pid")[0]

    def test_expected_call_allows_confirming_reads_after_the_write(self, h):
        """Models routinely read back what they wrote; that is not a failure."""
        step = h.Step(
            "lists.denylist.replace",
            "lists",
            "manageLists",
            "d",
            "p",
            "list_replace",
            expect=h._expect("manageLists", "replace", args=("denylist",)),
        )
        write = {
            "tool": "manageLists",
            "operation": "replace",
            "ok": True,
            "write": True,
            "args": '{"list_type": "denylist"}',
            "result": '{"data": []}',
        }
        confirm = {
            "tool": "manageLists",
            "operation": "get",
            "ok": True,
            "write": False,
            "args": '{"list_type": "denylist"}',
            "result": '{"data": []}',
        }
        assert h._expected_call_ok([write, confirm], step.expect, "pid")[0]

    def test_expected_call_rejects_a_write_that_undoes_the_step(self, h):
        """The rewrite add-then-delete seen in a live run fails on structure."""
        step = h.Step(
            "rewrites.add.a",
            "rewrites",
            "manageRewrites",
            "d",
            "p",
            "rewrite_add",
            expect=h._expect("manageRewrites", "add", args=("a.example.com", "192.0.2.100")),
        )
        add = {
            "tool": "manageRewrites",
            "operation": "add",
            "ok": True,
            "write": True,
            "args": '{"name": "a.example.com", "content": "192.0.2.100"}',
            "result": '{"data": []}',
        }
        delete = {
            "tool": "manageRewrites",
            "operation": "delete",
            "ok": True,
            "write": True,
            "args": '{"name": "a.example.com"}',
            "result": '{"data": []}',
        }
        ok, why = h._expected_call_ok([add, delete], step.expect, "pid")
        assert not ok
        assert "followed by the write manageRewrites(delete)" in why

    def test_expected_call_ignores_a_failed_write_after_the_match(self, h):
        """A rejected duplicate changed nothing, so it cannot undo the step.

        Seen live in rewrites.add.a: the add succeeded, then the model added
        the same record again and upstream rejected the duplicate.
        """
        step = h.Step(
            "rewrites.add.a",
            "rewrites",
            "manageRewrites",
            "d",
            "p",
            "rewrite_add",
            expect=h._expect("manageRewrites", "add", args=("a.example.com",)),
        )
        add = {
            "tool": "manageRewrites",
            "operation": "add",
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
            "manageProfiles",
            "d",
            "p",
            "profile_exists",
            expect=h._expect("manageProfiles", "get", args=("{profile_id}",), result=("{profile_id}",)),
        )
        assert "no manageProfiles(get) call" in h._expected_call_ok([], step.expect, "abc")[1]
        failed = {"tool": "manageProfiles", "operation": "get", "ok": False, "args": "{}", "result": ""}
        assert "every such call failed" in h._expected_call_ok([failed], step.expect, "abc")[1]
        wrong_id = {
            "tool": "manageProfiles",
            "operation": "get",
            "ok": True,
            "write": False,
            "args": '{"profile_id": "other"}',
            "result": '{"data": {"id": "other"}}',
        }
        assert "without ['abc']" in h._expected_call_ok([wrong_id], step.expect, "abc")[1]
        empty = {
            "tool": "manageProfiles",
            "operation": "get",
            "ok": True,
            "write": False,
            "args": '{"profile_id": "abc"}',
            "result": '{"error": "notFound"}',
        }
        assert "answered without" in h._expected_call_ok([empty], step.expect, "abc")[1]

    def test_answered_accepts_an_empty_list_as_an_answer(self, h):
        """A fresh profile really does have an empty allowlist."""
        assert h._answered('{"data": []}')
        assert h._answered('{"content": ["image"]}')
        # Every write tool answers this way (verified against the live API).
        assert h._answered('{"success": true}')
        assert not h._answered("")
        assert not h._answered('{"error": "notFound"}')
        assert not h._answered('{"code": "http_error", "error": "400 Bad Request"}')

    def test_every_step_declares_the_call_it_expects(self, h):
        """A step without an expectation would fall back to REST-only vacuity."""
        targets = h.TaskTargets(profile_name="TEST_PROFILE_X", tag="e2ehxyz789")
        steps = h.build_steps(targets)
        assert len(steps) >= 89
        assert [s.id for s in steps if not s.expect] == []
        for step in steps:
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
        step = h.Step("x", "profiles", "manageProfiles", "d", "p", "profile_exists")
        run = h.ActorRun(command=["x"])
        calls = [{"tool": "manageLists", "operation": "get", "ok": False, "error": "boom"}]
        out = h._feedback_block(step, run, "failed", "err", "", {}, server_calls=calls)
        assert any("SERVER" in ln and "manageLists(get)" in ln for ln in out)
        assert not any("SERVER" in ln for ln in h._feedback_block(step, run, "failed", "err", "", {}))


class TestStepPrompts:
    """Prompts state the task unambiguously, and never say how to do it."""

    def test_prompts_never_name_an_mcp_tool(self, h, all_steps):
        for step in all_steps:
            for tool in h.common.MCP_TOOLS:
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
        r = h.Report(started_at=1.0, finished_at=2.0, setup="provisioned via manageProfiles(create)")
        assert r.summary()["setup"] == "provisioned via manageProfiles(create)"


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
        assert "manageProfiles(create)" in note
        assert captured["tool"] == "manageProfiles"
        assert captured["arguments"] == {"operation": "create", "name": "P"}
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
        why = "server recorded no manageLists(get) call in this attempt"
        row = {
            "id": "lists.allowlist.read",
            "status": "failed",
            "mcp_tool": "manageLists",
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
        why = "server recorded no manageLists(get) call in this attempt"
        row = {
            "id": "lists.denylist.update",
            "status": "failed",
            "mcp_tool": "manageLists",
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
            "tool": "manageSettings",
            "ok": False,
            "error": "ValidationError: 1 validation error for call[manageSettings]\ncategory\n  Missing required argument",
        }
        row = {
            "id": "settings.privacy.update",
            "status": "failed",
            "mcp_tool": "manageSettings",
            "error": "settings/privacy read-back does not match",
            "mcp_response": {"server_calls": [dict(record) for _ in range(3)], "expected_call": {"ok": True}},
        }
        modes = h._failure_modes(row)
        assert modes.count(("schema rejected argument 'category'", "manageSettings")) == 1
        assert ("end state: settings/privacy read-back does not match", "manageSettings") in modes

    def test_the_server_says_what_it_refused(self, h):
        rows = [
            (
                {"tool": "manageLists", "ok": False, "error": "entry_id is required for remove operation"},
                "tool rejected call: entry_id missing",
            ),
            (
                {
                    "tool": "queryAnalytics",
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
                "mcp_tool": "manageLists",
                "error": "server recorded no manageLists(get) call in this attempt",
                "mcp_response": {
                    "server_calls": [],
                    "expected_call": {"ok": False, "why": "server recorded no manageLists(get) call in this attempt"},
                },
            }
            for i in range(2)
        ] + [
            {
                "id": "settings.privacy.update",
                "status": "passed",
                "mcp_tool": "manageSettings",
                "error": "",
                "mcp_response": {
                    "server_calls": [{"tool": "manageSettings", "ok": True}],
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
