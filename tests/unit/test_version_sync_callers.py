"""Regression tests for how the release workflows locate ``scripts/version_sync.py`` (issue #770).

Two of the five delegated call sites run in a checkout that may not contain the
CLI yet, which QA caught on PR #838:

* ``version-bumper.yml`` checks out the PR branch as the workspace root, and
  branches cut before the CLI lands have no ``scripts/version_sync.py`` —
  running the PR's copy fails every open branch at the bump step. The baseline
  checkout (``main-ref``) always has it post-merge, and using it also keeps
  PR-modifiable code out of a ``contents: write`` job.
* ``version-guard.yml`` runs the baseline's copy so a PR cannot weaken the
  guard, but for the window before the CLI reaches ``main`` that file is
  absent; the step must fall back to the PR's copy.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _run_body(workflow: str, job_id: str, step_name: str) -> str:
    """The ``run`` body of one named step in one job of a workflow."""
    doc = yaml.safe_load((WORKFLOWS_DIR / workflow).read_text())
    for step in doc["jobs"][job_id]["steps"]:
        if step.get("name") == step_name:
            body = step.get("run")
            assert isinstance(body, str), f"{workflow}:{job_id}:{step_name} is not a run step"
            return body
    raise AssertionError(f"{workflow}:{job_id} has no step named {step_name!r}")


def test_bumper_runs_the_baselines_copy_of_the_cli() -> None:
    """Stale PR branches have no scripts/version_sync.py; main-ref always does."""
    body = _run_body("version-bumper.yml", "bump-version", "Calculate and Update Version")

    assert "python3 main-ref/scripts/version_sync.py" in body, (
        "the bump step must run the baseline's copy of the CLI; the PR-branch copy is "
        "missing on branches cut before the CLI landed and is PR-modifiable in a "
        "contents: write job"
    )
    assert "--root ." in body, "the baseline's CLI must still rewrite the PR checkout at the workspace root"
    assert "--baseline main-ref" in body, "the bump must read the current version from main, not the PR head"
    assert "python3 scripts/version_sync.py bump" not in body


def test_guard_falls_back_to_the_pr_copy_until_the_cli_lands_on_main() -> None:
    """The guard prefers the baseline's copy but must work before the CLI is on main."""
    body = _run_body("version-guard.yml", "check-sync", "Validate Versions")

    assert 'SCRIPT="main/scripts/version_sync.py"' in body, (
        "the guard should prefer the baseline's copy so a PR cannot weaken the check"
    )
    assert '[ -f "$SCRIPT" ] || SCRIPT="pr/scripts/version_sync.py"' in body, (
        "without a fallback the guard fails for every PR until the CLI lands on main"
    )
    assert 'python3 "$SCRIPT" check --root pr --baseline main' in body
