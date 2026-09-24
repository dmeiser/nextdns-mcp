"""Unit tests for the pure (network-free) helpers in ``scripts/ai_e2e_harness.py``.

These tests exercise the result classification, report aggregation, and REST
read-back helpers without touching the network or the live MCP server. The
harness is a plain script (not an installed package), so it is loaded directly
by file path.

SPDX-License-Identifier: MIT
"""

import importlib.util
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

    def test_passed_on_success_flag(self, h):
        r = h.check_from_call("c1", "x", "d", mcp_response={"structured": {"success": True}, "is_error": False})
        assert r.status == "passed"

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

    def test_skipped_on_skipped_flag(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"structured": {"skipped": True, "reason": "nothing"}, "is_error": False},
        )
        assert r.status == "skipped"
        assert r.reason == "nothing"

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

    def test_skipped_on_unsupported_detail_prefix(self, h):
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"structured": {"detail": "unsupported by the API"}, "is_error": False},
        )
        assert r.status == "skipped"

    def test_unsupported_beats_error_body(self, h):
        # A raised result whose text starts with "unsupported" is a skip, not a fail.
        r = h.check_from_call(
            "c1",
            "x",
            "d",
            mcp_response={"structured": None, "is_error": True, "text": "unsupported: not here"},
        )
        assert r.status == "skipped"

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
        # non-domain types use a plain suffix
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
        # a failed read means nothing can be present
        assert h._list_absent({"ok": False}, ["a.com"])

    def test_entry_ids(self, h):
        resp = {"ok": True, "body": {"data": [{"id": "a"}, {"id": "b"}, {"x": 1}]}}
        assert h._entry_ids(resp) == ["a", "b"]
        assert h._entry_ids({"ok": False}) == []
        assert h._entry_ids({"ok": True, "body": "str"}) == []

    def test_rewrites_contains(self, h):
        resp = {
            "ok": True,
            "body": {"data": [{"name": "a.com", "content": "1.2.3.4"}]},
        }
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
        # two names are (essentially) unique
        assert h.make_profile_name() != h.make_profile_name()

    def test_list_specs_shape(self, h):
        # 7 list types, exactly 4 updatable (allowlist, denylist, parental x2)
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
        # blocklists get a post-run restore entry
        bl = next(s for s in h._LIST_SPECS if s["name"] == "privacy_blocklists")
        assert bl.get("restore") == "nextdns-recommended"

    def test_metric_lists(self, h):
        # queryAnalytics has 11 metrics, plotAnalytics has 9, series excludes domains
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
        import json

        assert lines[-1].startswith('{"summary"')
        assert json.loads(lines[0])["id"] == "a"

    def test_write_skip_report(self, h, tmp_path):
        import json

        p = tmp_path / "skip.jsonl"
        h.write_skip_report(str(p))
        assert json.loads(p.read_text())["verdict"] == "SKIP"

    @staticmethod
    def _mk(h):
        rep = h.Report(started_at=0.0, api_key_present=True, finished_at=0.1)
        rep.results.append(h.CheckResult(id="a", section="s", description="d", status="passed"))
        return rep

    def test_section_definitions_cover_checklist(self, h):
        # The 8 grouped tools map onto these sections; ensure all expected sections exist.
        for key in ("profiles", "settings", "lists", "rewrites", "analytics", "doh", "logs", "plots"):
            assert key in h.SECTIONS

    def test_settings_categories_seven(self, h):
        assert len(h.SETTINGS_CATEGORIES) == 7
        for cat, spec in h.SETTINGS_CATEGORIES.items():
            for field in ("path", "set", "assert", "restore", "assert_restore"):
                assert field in spec, f"{cat} missing {field}"
        # every path is a real REST sub-resource (except /settings which is "general")
        assert h.SETTINGS_CATEGORIES["general"]["path"] == "/settings"
        assert h.SETTINGS_CATEGORIES["blockpage"]["path"] == "/settings/blockPage"
