"""Tests for the weekly-maintenance auto-merge gate (GitHub issue #138).

The Sunday workflow historically merged release PRs when a ~13-keyword regex
over Copilot review comments found nothing scary. That made the keyword scan
the *sole* merge gate, so attacks phrased differently (TOCTOU, ReDoS, auth
bypass, leak) all passed. The fix makes the keyword scan a **block signal only**
and adds a **required maintainer-approval review** as a real required-check
gate. Merging now requires three independent gates:

  (a) full CI green            -> wait-checks.checks_passed
  (b) maintainer approval      -> wait-checks.maintainer_approved   (the real gate)
  (c) keyword scan clear       -> review.safe_to_merge             (block signal only)

These tests pin that structure so a regression that lets the regex stand in for
a human review is caught at CI time.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "weekly-maintenance.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _jobs(workflow: dict) -> dict:
    return workflow["jobs"]


def _merge(workflow: dict) -> dict:
    return _jobs(workflow)["merge"]


def _merge_if(workflow: dict) -> str:
    return " ".join(str(_merge(workflow).get("if", "")).split())


def test_merge_requires_green_checks_transitively(workflow: dict) -> None:
    """The merge gate only opens when CI is green.

    ``review`` (the keyword block signal) is gated on
    ``wait-checks.checks_passed == 'true'``, and ``merge`` needs ``review``.
    So a red CI run prevents the merge from ever reaching the approval check.
    """
    jobs = _jobs(workflow)
    # review only runs when CI is green
    assert "checks_passed == 'true'" in jobs["review"]["if"]
    # merge depends on review, so the green-CI condition is transitive to merge
    assert "review" in jobs["merge"]["needs"]


def test_merge_requires_maintainer_approval_not_just_keyword_scan(workflow: dict) -> None:
    """A maintainer approval is a required gate the keyword regex can never satisfy.

    The merge ``if`` must reference BOTH the keyword-clear signal and the
    maintainer-approval output, AND-ed. The keyword scan alone (safe_to_merge)
    must not be the only condition.
    """
    merge_if = _merge_if(workflow)
    # Both gates are referenced.
    assert "safe_to_merge" in merge_if, "keyword block signal must still gate the merge"
    assert "maintainer_approved" in merge_if, "maintainer approval must gate the merge"
    # They are combined with AND, so neither alone is sufficient.
    assert "&&" in merge_if, "the gates must be AND-ed, not OR-ed"


def test_keyword_scan_is_block_signal_never_sole_gate(workflow: dict) -> None:
    """The keyword scan can block a merge but can never be its sole permission.

    Even with a clean scan (safe_to_merge == true), the merge still requires the
    independent maintainer approval. We prove the regex is not a sufficient
    condition by confirming the approval output is a distinct required term in
    the same boolean expression.
    """
    merge_if = _merge_if(workflow)
    terms = [t.strip() for t in merge_if.split("&&")]
    assert len(terms) >= 2, "the merge gate must require more than one independent condition"
    scan_terms = [t for t in terms if "safe_to_merge" in t]
    approval_terms = [t for t in terms if "maintainer_approved" in t]
    assert scan_terms, "the keyword scan must be one of the gates"
    assert approval_terms, "the approval must be an independent gate"
    assert scan_terms != approval_terms


def test_approval_gate_is_wired_as_required_output(workflow: dict) -> None:
    """The approval gate output is exposed by wait-checks and feeds the merge ``if``.

    If the approval step never emits ``maintainer_approved=true`` (timeout,
    missing variable, no approval), the output stays empty and the merge
    condition ``== 'true'`` is false — so the merge is blocked, not bypassed.
    """
    wait_checks = _jobs(workflow)["wait-checks"]
    assert any(
        "maintainer_approved" in str(v) for v in wait_checks.get("outputs", {}).values()
    ), "wait-checks must expose the maintainer_approved gate"
    assert "maintainer_approved" in _merge_if(workflow)


def _wait_approval_script(workflow: dict) -> str:
    step = next(
        s for s in _jobs(workflow)["wait-checks"]["steps"] if s.get("id") == "wait_approval"
    )
    # Normalize GitHub Actions template expressions, which are unresolvable here.
    return re.sub(r"\$\{\{[^{}]*\}\}", "", step["run"])


def _run_wait_approval(
    workflow: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maintainers: str,
    reviews: list,
    timeout: float = 120,
) -> str:
    """Execute the extracted wait_approval script with a mocked ``gh`` binary.

    Returns the accumulated GITHUB_OUTPUT content. If ``timeout`` expires the
    script is killed and the output collected so far is returned — a gate that
    never opens keeps polling, which is itself the observable behavior.
    """
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("behavioral gate test requires bash and jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_text(json.dumps(reviews))
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\ncat "$FAKE_REVIEWS_JSON"\n')
    gh.chmod(0o755)
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("FAKE_REVIEWS_JSON", str(reviews_file))
    monkeypatch.setenv("MAINTAINERS", maintainers)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    try:
        subprocess.run(
            ["bash", "-c", _wait_approval_script(workflow)],
            check=True,
            cwd=tmp_path,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pass
    return output_file.read_text() if output_file.exists() else ""


def test_wait_approval_accepts_approval_from_named_maintainer(
    workflow: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An APPROVED review from a listed maintainer sets maintainer_approved=true."""
    output = _run_wait_approval(
        workflow,
        tmp_path,
        monkeypatch,
        maintainers="someone,alice",
        reviews=[{"state": "APPROVED", "user": {"login": "Alice"}}],
    )
    assert "maintainer_approved=true" in output


def test_wait_approval_fails_closed_without_maintainers(
    workflow: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With WEEKLY_MAINTAINERS unset, the gate fails closed even if a review approves."""
    output = _run_wait_approval(
        workflow,
        tmp_path,
        monkeypatch,
        maintainers="",
        reviews=[{"state": "APPROVED", "user": {"login": "Alice"}}],
    )
    assert "maintainer_approved=false" in output
    assert "maintainer_approved=true" not in output


def test_wait_approval_accepts_latest_approval_after_changes(
    workflow: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A maintainer whose NEWEST review is APPROVED opens the gate.

    Positive control for the latest-review-only semantics: an older
    CHANGES_REQUESTED followed by a newer APPROVED must still merge.
    """
    output = _run_wait_approval(
        workflow,
        tmp_path,
        monkeypatch,
        maintainers="alice",
        reviews=[
            {"state": "APPROVED", "user": {"login": "alice"}},
            {"state": "CHANGES_REQUESTED", "user": {"login": "alice"}},
        ],
    )
    assert "maintainer_approved=true" in output


def test_wait_approval_ignores_superseded_approval(
    workflow: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A maintainer's stale APPROVED must not open the gate after later CHANGES_REQUESTED.

    The reviews API returns history newest-first; here the maintainer's newest
    review is CHANGES_REQUESTED, so no maintainer_approved=true may be emitted.
    """
    output = _run_wait_approval(
        workflow,
        tmp_path,
        monkeypatch,
        maintainers="alice",
        reviews=[
            {"state": "CHANGES_REQUESTED", "user": {"login": "alice"}},
            {"state": "APPROVED", "user": {"login": "alice"}},
        ],
        timeout=5,
    )
    assert "maintainer_approved=true" not in output
