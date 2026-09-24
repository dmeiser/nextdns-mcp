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
        assert h._list_added("security_tlds", "abc") == "security_tlds-add-abc"

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
        assert name.startswith("AI E2E Test Profile ")
        assert h.make_profile_name() != h.make_profile_name()

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
        srv = doc["mcp"]["servers"]["nextdns"]
        assert srv["type"] == "local"
        assert srv["command"][0] == cfg.server["command"]
        assert srv["command"][1:] == cfg.server["args"]
        assert srv["cwd"] == cfg.server["cwd"]
        assert srv["environment"]["NEXTDNS_API_KEY"] == "sk-test"
        assert srv["environment"]["NEXTDNS_API_BASE"] == h.API_BASE
        assert srv["enabled"] is True

    def test_write_mcp_config_is_valid_json_schema_shaped(self, h, tmp_path):
        cfg = h.HarnessConfig.load(None)
        out = h.write_mcp_config(cfg, tmp_path)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert "$schema" in doc
        # The file must parse back cleanly (it is read by opencode at startup).
        assert isinstance(doc["mcp"]["servers"], dict)


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

    def __init__(self, profiles=None, by_path=None, delete_ok=True):
        self._profiles = profiles or []
        self._by_path = by_path or {}
        self._delete_ok = delete_ok
        self.deleted: list[str] = []

    async def rest_get(self, path, params=None):
        if path in self._by_path:
            return self._by_path[path]
        return {"status": 200, "ok": True, "body": {}}

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
