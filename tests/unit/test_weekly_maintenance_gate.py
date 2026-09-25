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


def test_approval_gate_is_fail_safe_on_timeout(workflow: dict) -> None:
    """If no maintainer approves in time, the merge is blocked (not bypassed).

    The approval step must be wired as a real gate (its output feeds the merge
    ``if``), and its timeout path must set ``maintainer_approved=false`` so that
    a missing approval is fail-safe: it blocks the merge rather than defaulting
    to merging.
    """
    wait_checks = _jobs(workflow)["wait-checks"]
    # The approval gate output is wired into the job outputs...
    assert any(
        "maintainer_approved" in str(v) for v in wait_checks.get("outputs", {}).values()
    ), "wait-checks must expose the maintainer_approved gate"
    # ...and that same output feeds the merge condition.
    assert "maintainer_approved" in _merge_if(workflow)

    # The approval step exists and its script fails safe on timeout.
    step = next(s for s in wait_checks["steps"] if s.get("id") == "wait_approval")
    script = step.get("run", "")
    assert "maintainer_approved=false" in script, "timeout must set maintainer_approved=false"
    # The true path and the timeout path must be distinct and explicit.
    assert "maintainer_approved=true" in script
