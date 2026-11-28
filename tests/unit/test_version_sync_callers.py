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
* ``docker-publish.yml`` and ``auto-release.yml`` run as ``workflow_run``
  workflows: the workflow comes from the default branch while the checkout is
  the triggering commit, which may predate the CLI. They load the helper from
  the workflow-supplying revision (``github.sha``) and read the version from
  the release checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
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


PUBLISH_CALLERS = (
    (
        "docker-publish.yml",
        "docker-publish",
        "Extract project version",
        {"version": "4.2.1", "major": "4", "major_minor": "4.2"},
    ),
    ("auto-release.yml", "release", "Extract Project Version", {"tag": "v4.2.1"}),
)


@pytest.mark.parametrize(("workflow", "job_id", "step_name", "expected"), PUBLISH_CALLERS)
def test_publish_callers_checkout_the_workflow_revision(
    workflow: str, job_id: str, step_name: str, expected: dict[str, str]
) -> None:
    """workflow_run uses the default branch's workflow; get the CLI from that revision."""
    doc = yaml.safe_load((WORKFLOWS_DIR / workflow).read_text())
    checkouts = [
        step for step in doc["jobs"][job_id]["steps"] if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert any(
        step.get("with", {}).get("path") == "workflow-ref" and step.get("with", {}).get("ref") == "${{ github.sha }}"
        for step in checkouts
    ), f"{workflow}:{job_id} must check out the workflow-supplying revision into workflow-ref"


@pytest.mark.parametrize(("workflow", "job_id", "step_name", "expected"), PUBLISH_CALLERS)
def test_publish_callers_read_a_pre_cli_release_tree(
    workflow: str, job_id: str, step_name: str, expected: dict[str, str], tmp_path: Path
) -> None:
    """A delayed/retried release of a pre-CLI commit still gets its version and tag outputs."""
    release = tmp_path / "release"
    release.mkdir()
    # The triggering release tree predates scripts/version_sync.py.
    (release / "pyproject.toml").write_text('[project]\nname = "nextdns-mcp"\nversion = "4.2.1"\n')
    helper = release / "workflow-ref" / "scripts"
    helper.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "version_sync.py", helper / "version_sync.py")
    assert not (release / "scripts" / "version_sync.py").exists()

    outputs = tmp_path / "github-output"
    outputs.touch()
    body = _run_body(workflow, job_id, step_name)
    proc = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", body],
        cwd=release,
        env={**os.environ, "GITHUB_OUTPUT": str(outputs)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"{workflow}:{step_name} failed on a pre-CLI tree:\n{proc.stderr}"
    written = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    assert written == expected, f"{workflow}:{step_name} lost its outputs: {written}"
