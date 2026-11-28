"""Regression guard for SEC-2: GitHub expressions must not become executable source.

Interpolating ``${{ ... }}`` directly into a ``run:`` or ``script:`` block puts
untrusted context values into shell or JavaScript source before it is parsed.
The four workflows in ``WORKFLOWS_UNDER_TEST`` pass those values through step
``env:`` blocks instead. This test fails if the old pattern returns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

WORKFLOWS_UNDER_TEST = (
    "version-guard.yml",
    "weekly-maintenance.yml",
    "auto-release.yml",
    "dependabot-auto-merge.yml",
)

#: The vulnerable shape from each affected workflow, kept as a fixture so the
#: guard is proven to fail when the fix is removed.
PRE_FIX_WORKFLOW = """
name: Pre-fix regression fixture
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Labels in single-quoted bash
        run: >-
          LABELS='${{ toJson(github.event.pull_request.labels.*.name) }}'
      - name: Review reason in double-quoted bash
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: >-
          gh pr comment 1 --body "reason: ${{ steps.inspect.outputs.block_reason }}"
      - name: Release tag in github-script
        uses: actions/github-script@v8
        with:
          script: >-
            const tagName = '${{ steps.version.outputs.tag }}';
      - name: Safe environment-backed step
        env:
          PR_LABELS: ${{ toJson(github.event.pull_request.labels.*.name) }}
        run: >-
          LABELS="$PR_LABELS"
"""

EXPRESSION = "${{"
EXECUTABLE_KEYS = ("run", "script")


def _direct_expressions(document: Any, location: str = "workflow") -> list[str]:
    """Yield locations of expressions embedded in executable step text."""
    found: list[str] = []
    if isinstance(document, dict):
        for key, value in document.items():
            where = f"{location}.{key}"
            if key in EXECUTABLE_KEYS and isinstance(value, str):
                found.extend(f"{where}: {line.strip()}" for line in value.splitlines() if EXPRESSION in line)
            else:
                found.extend(_direct_expressions(value, where))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(_direct_expressions(value, f"{location}[{index}]"))
    return found


@pytest.mark.parametrize("workflow_name", WORKFLOWS_UNDER_TEST)
def test_no_direct_expressions_in_executable_steps(workflow_name: str) -> None:
    """Expressions must reach bash and github-script only through environment variables."""
    path = WORKFLOWS_DIR / workflow_name
    raw = yaml.safe_load(path.read_text())
    offenders = _direct_expressions(raw)
    assert not offenders, f"{workflow_name} interpolates GitHub expressions into executable text: {offenders}"


def test_pre_fix_workflow_is_detected_as_regression() -> None:
    """The vulnerable text must fail the guard, not merely be absent today."""
    raw = yaml.safe_load(PRE_FIX_WORKFLOW)
    offenders = _direct_expressions(raw)
    assert len(offenders) == 3, f"expected the three vulnerable steps, got: {offenders}"
    assert any("jobs.check.steps[0].run" in offender for offender in offenders)
    assert any("jobs.check.steps[1].run" in offender for offender in offenders)
    assert any("jobs.check.steps[2].with.script" in offender for offender in offenders)
