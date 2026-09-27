"""Behavioral tests for the CI complexity gate.

The `complexity` job in `.github/workflows/unit-tests.yml` is the machine
consumption of the documented complexity contract (radon grade B ceiling per
function, grade A project average). These tests execute the gate's own shell
script against synthetic source trees and assert whether CI would pass or fail,
which is what a developer pushing a pull request actually experiences.

The script under test is taken verbatim from the workflow and is not modified
here; only the `uv` launcher is shimmed so it can run against a temporary
source tree with the interpreter running the test suite.
"""

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "unit-tests.yml"


def _gate_script() -> str:
    """Return the shell script the complexity job executes in CI."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    run_scripts = [
        step["run"]
        for step in workflow["jobs"]["complexity"]["steps"]
        if "radon" in step.get("run", "")
    ]
    assert len(run_scripts) == 1, f"expected one radon step, found {len(run_scripts)}"
    return run_scripts[0]


def _function(name: str, branches: int) -> str:
    """A module-level function with cyclomatic complexity `branches + 1`."""
    lines = [f"def {name}(x):"]
    for index in range(branches):
        keyword = "if" if index == 0 else "elif"
        lines.append(f"    {keyword} x == {index + 1}:")
        lines.append(f"        return {index}")
    lines.append("    return 0")
    return "\n".join(lines) + "\n"


def _padding(count: int = 10) -> str:
    """Trivial grade A functions that keep the project average at grade A."""
    return "".join(f"def helper_{i}(x):\n    return x\n\n" for i in range(count))


def _run_gate(tmp_path: Path, sources: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real CI gate script against a throwaway source tree."""
    src = tmp_path / "src"
    src.mkdir()
    for filename, body in sources.items():
        (src / filename).write_text(body)

    # Shim `uv run radon ...` onto the interpreter that has radon installed, so
    # the gate script runs unmodified against the throwaway tree.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        'shift 2  # drop "run" and the radon launcher; use the module below\n'
        f'exec "{sys.executable}" -m radon "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-e", "-c", _gate_script()],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )


def test_gate_passes_for_compliant_code(tmp_path):
    """Code entirely within the documented grade B ceiling passes CI."""
    result = _run_gate(tmp_path, {"clean.py": _function("simple", 1)})
    assert result.returncode == 0, f"gate rejected compliant code:\n{result.stdout}{result.stderr}"


def test_gate_passes_at_grade_b_boundary(tmp_path):
    """Complexity 10 is the documented ceiling (grade B) and is accepted."""
    sources = {"boundary_b.py": _function("at_limit", 9) + _padding()}
    result = _run_gate(tmp_path, sources)
    assert result.returncode == 0, f"gate rejected complexity 10 (grade B):\n{result.stdout}{result.stderr}"


def test_gate_blocks_function_above_grade_b(tmp_path):
    """Complexity 11 is grade C; CI must fail and name the offending function."""
    sources = {"boundary_c.py": _function("over_limit", 10) + _padding()}
    result = _run_gate(tmp_path, sources)
    assert result.returncode != 0, "gate accepted complexity 11 (grade C)"
    assert "over_limit" in result.stdout, f"gate did not report the offending function:\n{result.stdout}"
    assert "exceeding Grade B" in result.stdout


def test_gate_blocks_when_project_average_is_not_grade_a(tmp_path):
    """No function above grade B, but a grade B project average still fails CI."""
    sources = {"avg_only.py": _function("one", 7) + _function("two", 7) + _function("three", 7)}
    result = _run_gate(tmp_path, sources)
    assert result.returncode != 0, "gate accepted a grade B project average"
    assert "grade B, Grade A required" in result.stdout, result.stdout
