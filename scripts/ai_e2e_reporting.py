"""Report rendering and aggregation for the NextDNS MCP E2E harness.

SPDX-License-Identifier: MIT

The JSONL report writer (``write_report`` / ``write_skip_report``), the human
text rendering (``render_text_report``), and the cross-report failure
summarizer (``summarize_reports``) that groups failures by what went wrong.
Split out of ``ai_e2e_harness.py``.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import ai_e2e_common as common
from ai_e2e_common import Report


def write_report(report: Report, path: str, actor_log: str | None = None) -> None:
    if path == "-":
        _print_report(report)
        return
    _write_report_file(report, path, actor_log)


def _print_report(report: Report) -> None:
    for result in report.results:
        print(json.dumps(result.to_dict()))
    print(json.dumps({"summary": report.summary()}))
    if report.actor_raw_lines:
        print("--- actor event stream ---", file=sys.stderr)
        for ln in report.actor_raw_lines:
            print(ln, file=sys.stderr)


def _write_report_file(report: Report, path: str, actor_log: str | None) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(result.to_dict()) for result in report.results]
    lines.append(json.dumps({"summary": report.summary()}))
    common.write_private_text(out, "\n".join(lines) + "\n")
    if not report.actor_raw_lines:
        return
    # Sidecar: the actor's raw event stream, for debugging model behavior.
    sidecar = Path(actor_log) if actor_log else out.with_name(out.stem + ".actor.jsonl")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    common.write_private_text(sidecar, "\n".join(report.actor_raw_lines) + "\n")
    print(f"Actor event stream written to {sidecar}", file=sys.stderr)


def write_skip_report(path: str, reason: str = "NEXTDNS_API_KEY not set") -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    common.write_private_text(out, json.dumps({"verdict": "SKIP", "reason": reason}) + "\n")


def render_text_report(report: Report) -> str:
    lines = ["=" * 72, "NextDNS MCP — MCP-native E2E harness (actor=harness, measure=REST)", "=" * 72]
    lines.extend(_headline_lines(report))
    if report.skipped_sections:
        lines.append(f"skipped sections: {', '.join(report.skipped_sections)}")
    lines.append("-" * 72)
    lines.extend(_result_lines(report))
    lines.append("=" * 72)
    # End-of-run summary: overall + per-section tally + failed steps.
    lines.append("SUMMARY")
    lines.extend(_summary_lines(report))
    lines.append("=" * 72)
    return "\n".join(lines)


def _headline_lines(report: Report) -> list[str]:
    s = report.summary()
    lines = [
        f"VERDICT : {s['verdict']}",
        f"total   : {s['total']}  (passed {s['passed']}, skipped {s['skipped']}, failed {s['failed']})",
        f"profile : {report.profile_id}  ({report.profile_name})",
    ]
    if report.setup:
        lines.append(f"setup   : {report.setup}")
    lines.append(
        f"actor   : {report.actor_kind} — {len(report.actor_tools_used)} tool(s) observed, {s['actor_tool_calls']} event(s)"
    )
    lines.append(f"tools   : {', '.join(report.actor_tools_used) or '(none)'}")
    lines.append(f"cleanup : {s['cleanup']}")
    return lines


def _step_feedback(result: Any) -> list[str] | None:
    fb = (result.mcp_response or {}).get("feedback") if isinstance(result.mcp_response, dict) else None
    return fb or None


def _result_lines(report: Report) -> list[str]:
    lines = []
    for result in report.results:
        lines.append(f"[{_STATUS_ICON[result.status]}] {result.id}  —  {result.description}")
        lines.extend(_step_feedback(result) or _verdict_detail_lines(result))
    return lines


def _verdict_detail_lines(result: Any) -> list[str]:
    if result.status == "failed":
        return [f"       error: {result.error}"]
    if result.status == "skipped" and result.reason:
        return [f"       reason: {result.reason}"]
    return []


_STATUS_ICON = {"passed": "PASS", "skipped": "SKIP", "failed": "FAIL"}


def _section_counts(report: Report) -> dict[str, dict[str, int]]:
    by_section: dict[str, dict[str, int]] = {}
    for result in report.results:
        section = by_section.setdefault(result.section, {"passed": 0, "skipped": 0, "failed": 0})
        section[result.status] += 1
    return by_section


def _summary_lines(report: Report) -> list[str]:
    s = report.summary()
    lines = [
        f"  verdict: {s['verdict']}  ({s['passed']} passed, {s['skipped']} skipped, {s['failed']} failed of {s['total']} steps)"
    ]
    for section, counts in _section_counts(report).items():
        lines.append(
            f"  {section:12s}: {counts['passed']} passed, {counts['skipped']} skipped, {counts['failed']} failed"
        )
    lines.extend(_failed_step_lines(report))
    return lines


def _failed_step_lines(report: Report) -> list[str]:
    failed_ids = [result.id for result in report.results if result.status == "failed"]
    if not failed_ids:
        return []
    return ["  failed steps:"] + [f"    - {fid}" for fid in failed_ids]


# What the server said when it refused a call, as reusable patterns: the schema
# names the parameter it rejected, and the upstream status code is what a caller
# can act on.
_SCHEMA_REJECTION = re.compile(r"validation error for call\[(\w+)\]\s*(\w+)")
_UPSTREAM_STATUS = re.compile(r"'(\d{3}) ")


def _transient_seen(transients: Any, kind: str) -> bool:
    """Whether the harness recorded this transient kind for the step."""
    return any(str(transient).startswith(kind) for transient in transients or [])


def _no_call_modes(response: dict[str, Any], tool: str) -> list[tuple[str, str]]:
    """What the harness knows about a step whose actor never reached the server.

    A step whose actor never got as far as a call is usually the model
    stalling, not the server refusing: the harness already says which.
    Separating the two is what keeps a run's infrastructure noise from
    looking like a contract problem.
    """
    transients = response.get("transients") or []
    for kind, label in _TRANSIENT_NO_CALL_MODES:
        if _transient_seen(transients, kind):
            return [(label, tool)]
    return [("no call reached the server", tool)]


# The transient kinds the retry loop already named, and how the summary calls
# them. Ordered: the first present decides the label.
_TRANSIENT_NO_CALL_MODES = (
    ("stall", "actor stalled without calling"),
    ("wrong-tool-path", "actor called a tool path this client does not expose"),
    ("provider-unavailable", "model provider was not serving the model"),
)


def _record_modes(records: list[dict[str, Any]], tool: str) -> list[tuple[str, str]]:
    """What each failed call the server recorded says about itself."""
    modes: list[tuple[str, str]] = []
    for record in records:
        if record.get("ok"):
            continue
        name = str(record.get("tool") or tool)
        modes.append(_record_mode(str(record.get("error") or "").replace("\n", " "), name))
    return modes


def _record_mode(error: str, name: str) -> tuple[str, str]:
    schema = _SCHEMA_REJECTION.search(error)
    if schema:
        return (f"schema rejected argument '{schema.group(2)}'", name)
    upstream = _UPSTREAM_STATUS.search(error)
    if upstream:
        return (f"upstream returned {upstream.group(1)}", name)
    return ("server error: " + error[:70], name)


def _expected_call_modes(response: dict[str, Any], tool: str) -> tuple[list[tuple[str, str]], str]:
    """The transcript half of the verdict: its label, and the raw reason text."""
    expected = response.get("expected_call") or {}
    expected_why = str(expected.get("why") or "") if expected.get("ok") is False else ""
    if not expected_why:
        return [], ""
    return _expected_call_label(expected, expected_why, tool), expected_why


def _expected_call_label(expected: dict[str, Any], expected_why: str, tool: str) -> list[tuple[str, str]]:
    """The one label the verdict text matches, named by the first marker present."""
    for marker, label in _EXPECTED_CALL_MODES:
        if marker in expected_why:
            expect = expected.get("expect") or {}
            return [(label, str(expect.get("tool") or tool))]
    return []


def _end_state_modes(row: dict[str, Any], expected_why: str, tool: str) -> list[tuple[str, str]]:
    # The REST half of the verdict. When the transcript half already said this
    # — the step failed because the expected call never happened, and that text
    # is what lands in `error` — counting it twice would overstate the mode.
    if row.get("status") != "failed" or not row.get("error"):
        return []
    error_text = " ".join(str(row["error"]).split())
    # Compare before truncating: a verdict naming a long endpoint exceeds
    # the bound, and comparing the cut text would count the same failure
    # twice.
    if not error_text or error_text == " ".join(str(expected_why).split()):
        return []
    return [("end state: " + error_text[:70], tool)]


def _instrument_modes(response: dict[str, Any], tool: str) -> list[tuple[str, str]]:
    if response.get("instrument_read"):
        # Not a capability failure and not infrastructure: the subject read the
        # record the verdict is made from, which makes this step's number mean
        # something else.
        return [("actor read the harness call log", tool)]
    return []


def _failure_modes(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Every failure mode visible in one report row, as (mode, tool).

    A step can fail in several ways at once — the schema rejected the arguments
    *and* the end state is wrong — so this reports all of them, not the first.
    Each mode is reported once per step, however many calls inside that step
    show it: the counts answer "how many steps did this cost", which is the
    question the tuning loop asks.
    """
    response = row.get("mcp_response") or {}
    records = response.get("server_calls") or []
    tool = str(row.get("mcp_tool") or "")

    modes = _instrument_modes(response, tool)
    if not records:
        modes.extend(_no_call_modes(response, tool))
    modes.extend(_record_modes(records, tool))
    expected_modes, expected_why = _expected_call_modes(response, tool)
    modes.extend(expected_modes)
    modes.extend(_end_state_modes(row, expected_why, tool))
    return list(dict.fromkeys(modes))


# Ordered: the first marker present in the verdict text decides the label.
_EXPECTED_CALL_MODES = (
    ("reached the server but every such call failed", "expected call always failed"),
    ("was called without", "expected call missing an argument"),
    ("answered without", "expected call answered without the payload"),
    ("returned no payload", "expected call returned no payload"),
    ("followed by the write", "step undone by a later write"),
    ("no ", "no matching call recorded"),
)


def _report_rows(paths: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Every row of every report, and the run-level notes the summaries carry."""
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(row)
                notes.extend(_summary_notes(row))
    return rows, notes


def _summary_notes(row: dict[str, Any]) -> list[str]:
    summary = row.get("summary")
    if not isinstance(summary, dict):
        return []
    return [str(note) for note in summary.get("notes") or []]


def _skip_reason_for(row: dict[str, Any]) -> str | None:
    return str(row.get("reason") or "skipped") if row["status"] == "skipped" else None


def _add_step_modes(by_mode: dict[tuple[str, str], list[str]], row: dict[str, Any]) -> None:
    for mode, tool in _failure_modes(row):
        by_mode.setdefault((mode, tool), []).append(str(row.get("id") or "?"))


def _group_failures(steps: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], list[str]], Counter]:
    by_mode: dict[tuple[str, str], list[str]] = {}
    skip_reasons: Counter = Counter()
    for row in steps:
        reason = _skip_reason_for(row)
        if reason is not None:
            # A skipped step was never attempted - a budget that ran out, or a run
            # aborted on an outage. Grouping it as a failure mode buries the modes
            # that were actually measured.
            skip_reasons[reason] += 1
        elif row["status"] != "passed":
            _add_step_modes(by_mode, row)
    return by_mode, skip_reasons


def _counts_line(paths: list[str], steps: list[dict[str, Any]]) -> str:
    counts = Counter(row["status"] for row in steps)
    return (
        f"failure modes: {len(paths)} report(s), {len(steps)} step(s) "
        f"({counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed, {counts.get('skipped', 0)} skipped)"
    )


def _note_lines(notes: list[str]) -> list[str]:
    if not notes:
        return []
    lines = ["  RUN NOT A CLEAN MEASUREMENT - what happened outside the model:"]
    lines.extend(f"    ! {note}" for note in notes)
    lines.append("")
    return lines


def _mode_lines(by_mode: dict[tuple[str, str], list[str]]) -> list[str]:
    if not by_mode:
        return ["  no failures"]
    lines = []
    for (mode, tool), ids in sorted(by_mode.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        shown = ", ".join(ids[:6]) + (", ..." if len(ids) > 6 else "")
        lines.append(f"{len(ids):3}  {mode:44} {tool:15} {shown}")
    return lines


def _skip_lines(skip_reasons: Counter) -> list[str]:
    if not skip_reasons:
        return []
    lines = ["  skipped, never attempted:"]
    lines.extend(f"{count:3}  {reason}" for reason, count in skip_reasons.most_common())
    return lines


def summarize_reports(paths: list[str]) -> str:
    """Group a run's failures by what went wrong, most common first.

    The tuning loop this exists for: fix one failure mode, re-run the steps that
    show it, repeat. So the output names each mode, how many steps it cost, and
    which steps to re-run.
    """
    rows, notes = _report_rows(paths)
    steps = [row for row in rows if "status" in row]
    if not steps:
        return "no step results in " + ", ".join(paths)
    by_mode, skip_reasons = _group_failures(steps)
    lines = ["=" * 72, _counts_line(paths, steps), "=" * 72]
    lines.extend(_note_lines(notes))
    lines.extend(_mode_lines(by_mode))
    lines.extend(_skip_lines(skip_reasons))
    return "\n".join(lines)
