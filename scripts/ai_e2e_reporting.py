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
        for r in report.results:
            print(json.dumps(r.to_dict()))
        print(json.dumps({"summary": report.summary()}))
        if report.actor_raw_lines:
            print("--- actor event stream ---", file=sys.stderr)
            for ln in report.actor_raw_lines:
                print(ln, file=sys.stderr)
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r.to_dict()) for r in report.results]
    lines.append(json.dumps({"summary": report.summary()}))
    common.write_private_text(out, "\n".join(lines) + "\n")
    # Sidecar: the actor's raw event stream, for debugging model behavior.
    sidecar = Path(actor_log) if actor_log else out.with_name(out.stem + ".actor.jsonl")
    if report.actor_raw_lines:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        common.write_private_text(sidecar, "\n".join(report.actor_raw_lines) + "\n")
        print(f"Actor event stream written to {sidecar}", file=sys.stderr)


def write_skip_report(path: str, reason: str = "NEXTDNS_API_KEY not set") -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    common.write_private_text(out, json.dumps({"verdict": "SKIP", "reason": reason}) + "\n")


def render_text_report(report: Report) -> str:
    lines = ["=" * 72, "NextDNS MCP — MCP-native E2E harness (actor=harness, measure=REST)", "=" * 72]
    s = report.summary()
    lines.append(f"VERDICT : {s['verdict']}")
    lines.append(f"total   : {s['total']}  (passed {s['passed']}, skipped {s['skipped']}, failed {s['failed']})")
    lines.append(f"profile : {report.profile_id}  ({report.profile_name})")
    if report.setup:
        lines.append(f"setup   : {report.setup}")
    lines.append(
        f"actor   : {report.actor_kind} — {len(report.actor_tools_used)} tool(s) observed, {s['actor_tool_calls']} event(s)"
    )
    lines.append(f"tools   : {', '.join(report.actor_tools_used) or '(none)'}")
    lines.append(f"cleanup : {s['cleanup']}")
    if report.skipped_sections:
        lines.append(f"skipped sections: {', '.join(report.skipped_sections)}")
    lines.append("-" * 72)
    for r in report.results:
        icon = {"passed": "PASS", "skipped": "SKIP", "failed": "FAIL"}[r.status]
        lines.append(f"[{icon}] {r.id}  —  {r.description}")
        fb = (r.mcp_response or {}).get("feedback") if isinstance(r.mcp_response, dict) else None
        if fb:
            lines.extend(fb)
        if r.status == "failed" and not fb:
            lines.append(f"       error: {r.error}")
        elif r.status == "skipped" and r.reason and not fb:
            lines.append(f"       reason: {r.reason}")
    lines.append("=" * 72)
    # End-of-run summary: overall + per-section tally + failed steps.
    lines.append("SUMMARY")
    lines.append(
        f"  verdict: {s['verdict']}  ({s['passed']} passed, {s['skipped']} skipped, {s['failed']} failed of {s['total']} steps)"
    )
    by_section: dict[str, dict[str, int]] = {}
    for r in report.results:
        sec = by_section.setdefault(r.section, {"passed": 0, "skipped": 0, "failed": 0})
        sec[r.status] += 1
    for section, counts in by_section.items():
        lines.append(
            f"  {section:12s}: {counts['passed']} passed, {counts['skipped']} skipped, {counts['failed']} failed"
        )
    failed_ids = [r.id for r in report.results if r.status == "failed"]
    if failed_ids:
        lines.append("  failed steps:")
        lines.extend(f"    - {fid}" for fid in failed_ids)
    lines.append("=" * 72)
    return "\n".join(lines)


# What the server said when it refused a call, as reusable patterns: the schema
# names the parameter it rejected, and the upstream status code is what a caller
# can act on.
_SCHEMA_REJECTION = re.compile(r"validation error for call\[(\w+)\]\s*(\w+)")
_UPSTREAM_STATUS = re.compile(r"'(\d{3}) ")


def _failure_modes(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Every failure mode visible in one report row, as (mode, tool).

    A step can fail in several ways at once — the schema rejected the arguments
    *and* the end state is wrong — so this reports all of them, not the first.
    Each mode is reported once per step, however many calls inside that step
    show it: the counts answer "how many steps did this cost", which is the
    question the tuning loop asks.
    """
    modes: list[tuple[str, str]] = []
    response = row.get("mcp_response") or {}
    records = response.get("server_calls") or []
    tool = str(row.get("mcp_tool") or "")

    if response.get("instrument_read"):
        # Not a capability failure and not infrastructure: the subject read the
        # record the verdict is made from, which makes this step's number mean
        # something else.
        modes.append(("actor read the harness call log", tool))

    if not records:
        # A step whose actor never got as far as a call is usually the model
        # stalling, not the server refusing: the harness already says which.
        # Separating the two is what keeps a run's infrastructure noise from
        # looking like a contract problem.
        if any(str(t).startswith("stall") for t in response.get("transients") or []):
            modes.append(("actor stalled without calling", tool))
        elif any(str(t).startswith("wrong-tool-path") for t in response.get("transients") or []):
            modes.append(("actor called a tool path this client does not expose", tool))
        elif any(str(t).startswith("provider-unavailable") for t in response.get("transients") or []):
            modes.append(("model provider was not serving the model", tool))
        else:
            modes.append(("no call reached the server", tool))

    for record in records:
        if record.get("ok"):
            continue
        name = str(record.get("tool") or tool)
        error = str(record.get("error") or "").replace("\n", " ")
        match = _SCHEMA_REJECTION.search(error)
        if match:
            modes.append((f"schema rejected argument '{match.group(2)}'", name))
            continue
        match = _UPSTREAM_STATUS.search(error)
        if match:
            modes.append((f"upstream returned {match.group(1)}", name))
            continue
        modes.append(("server error: " + error[:70], name))

    expected = response.get("expected_call") or {}
    expected_why = str(expected.get("why") or "") if expected.get("ok") is False else ""
    if expected_why:
        for marker, label in _EXPECTED_CALL_MODES:
            if marker in expected_why:
                modes.append((label, str((expected.get("expect") or {}).get("tool") or tool)))
                break

    # The REST half of the verdict. When the transcript half already said this
    # — the step failed because the expected call never happened, and that text
    # is what lands in `error` — counting it twice would overstate the mode.
    if row.get("status") == "failed" and row.get("error"):
        error_text = " ".join(str(row["error"]).split())
        # Compare before truncating: a verdict naming a long endpoint exceeds
        # the bound, and comparing the cut text would count the same failure
        # twice.
        if error_text and error_text != " ".join(str(expected_why).split()):
            modes.append(("end state: " + error_text[:70], tool))
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


def summarize_reports(paths: list[str]) -> str:
    """Group a run's failures by what went wrong, most common first.

    The tuning loop this exists for: fix one failure mode, re-run the steps that
    show it, repeat. So the output names each mode, how many steps it cost, and
    which steps to re-run.
    """
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(row)
                if "summary" in row:
                    notes.extend(str(n) for n in (row["summary"].get("notes") or []))
    steps = [row for row in rows if "status" in row]
    if not steps:
        return "no step results in " + ", ".join(paths)

    by_mode: dict[tuple[str, str], list[str]] = {}
    skip_reasons: Counter = Counter()
    for row in steps:
        if row["status"] == "passed":
            continue
        # A skipped step was never attempted - a budget that ran out, or a run
        # aborted on an outage. Grouping it as a failure mode buries the modes
        # that were actually measured.
        if row["status"] == "skipped":
            skip_reasons[str(row.get("reason") or "skipped")] += 1
            continue
        for mode, tool in _failure_modes(row):
            by_mode.setdefault((mode, tool), []).append(str(row.get("id") or "?"))

    counts = Counter(row["status"] for row in steps)
    lines = [
        "=" * 72,
        (
            f"failure modes: {len(paths)} report(s), {len(steps)} step(s) "
            f"({counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed, {counts.get('skipped', 0)} skipped)"
        ),
        "=" * 72,
    ]
    if notes:
        lines.append("  RUN NOT A CLEAN MEASUREMENT - what happened outside the model:")
        for note in notes:
            lines.append(f"    ! {note}")
        lines.append("")
    if not by_mode:
        lines.append("  no failures")
    else:
        for (mode, tool), ids in sorted(by_mode.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            shown = ", ".join(ids[:6]) + (", ..." if len(ids) > 6 else "")
            lines.append(f"{len(ids):3}  {mode:44} {tool:15} {shown}")
    if skip_reasons:
        lines.append("  skipped, never attempted:")
        for reason, count in skip_reasons.most_common():
            lines.append(f"{count:3}  {reason}")
    return "\n".join(lines)
