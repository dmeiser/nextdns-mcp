"""Behavioral tests for the CI coverage gate's branch-coverage contract (issue #278).

The `unit-tests` job in `.github/workflows/unit-tests.yml` is the machine
consumption of the documented "100% coverage" contract in `AGENT.md`. Before
issue #278 it measured **statements only**: `pyproject.toml` had no
`[tool.coverage]` section, so a 100% statement report passed the gate while
untested branches stayed invisible. That is how the
`AccessControlledClient.stream()` ACL bypass (issue #262) survived a green
build: the three `stream()` tests only used well-formed relative URLs, so the
allow and deny paths were covered and the bypass branch was never taken.

These tests assert two things a developer actually experiences when pushing:

1. `pyproject.toml` enables branch measurement, so the `term-missing` report
   the gate parses actually contains branch data.
2. The gate script itself rejects a statement-only report and rejects a
   branch report with a partial branch, and accepts a complete one.

The script under test is taken verbatim from the workflow and is not modified
here; only the `uv` launcher is shimmed so it can run against a throwaway
coverage report.
"""

import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "unit-tests.yml"

# A report produced with branch measurement off: no Branch/BrPart columns, and
# the header row of a real `term-missing` run in that mode.
STATEMENT_ONLY_REPORT = """\
Name                    Stmts   Miss  Cover   Missing
-------------------------------------------------------
src/nextdns_mcp/client.py     10      0   100%
-------------------------------------------------------
TOTAL                        10      0   100%
"""

# The same report with branch measurement on and every branch taken.
BRANCH_COMPLETE_REPORT = """\
Name                    Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------------------------------
src/nextdns_mcp/client.py     10      0     48      0   100%
--------------------------------------------------------------------------------
TOTAL                        10      0     48      0   100%
"""

# Branch measurement on, but the `stream()` bypass branch was never taken.
BRANCH_PARTIAL_REPORT = """\
Name                    Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------------------------------
src/nextdns_mcp/client.py     10      0     48      1    98%   249->252
--------------------------------------------------------------------------------
TOTAL                        10      0     48      1    98%
"""


def _coverage_gate_script() -> str:
    """Return the shell script the unit-tests job executes for its coverage gate."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["unit-tests"]["steps"]
    run_scripts = [step["run"] for step in steps if "MIN_FILE_COVERAGE" in step.get("run", "")]
    assert len(run_scripts) == 1, f"expected one coverage gate step, found {len(run_scripts)}"
    return run_scripts[0]


def _run_gate(tmp_path: Path, report: str) -> subprocess.CompletedProcess[str]:
    """Run the real CI coverage gate against a throwaway coverage report."""
    (tmp_path / "report.txt").write_text(report)

    # Shim `uv run pytest ...` so the gate script runs unmodified: it prints the
    # throwaway report where the real step prints its pytest output.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text('#!/bin/sh\nshift 2  # drop "run" and the pytest launcher\nexec cat report.txt\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-e", "-c", _coverage_gate_script()],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "github_output.txt"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_pyproject_enables_branch_coverage():
    """Branch measurement is configured, not left to the pytest-cov default."""
    config = tomllib.loads(PYPROJECT.read_text())
    assert config["tool"]["coverage"]["run"]["branch"] is True, (
        "pyproject.toml must set [tool.coverage.run] branch = true so the coverage gate "
        "measures branches and not statements alone (issue #278)"
    )


def test_gate_rejects_a_statement_only_report(tmp_path):
    """A 100% statement report with no branch data must not pass the gate."""
    result = _run_gate(tmp_path, STATEMENT_ONLY_REPORT)
    assert result.returncode != 0, (
        f"gate accepted a statement-only coverage report; branch coverage is off (issue #278)\n{result.stdout}"
    )
    assert "branch" in result.stdout.lower(), result.stdout


def test_gate_rejects_a_partial_branch(tmp_path):
    """An untested branch must fail the gate even at 100% statement coverage."""
    result = _run_gate(tmp_path, BRANCH_PARTIAL_REPORT)
    assert result.returncode != 0, f"gate accepted a partial branch:\n{result.stdout}"
    assert "98%" in result.stdout, result.stdout


def test_gate_accepts_complete_branch_coverage(tmp_path):
    """A fully covered statement *and* branch report still passes the gate."""
    result = _run_gate(tmp_path, BRANCH_COMPLETE_REPORT)
    assert result.returncode == 0, f"gate rejected complete branch coverage:\n{result.stdout}{result.stderr}"
