"""Behavioral tests for the CI complexity gate.

The `complexity` job in `.github/workflows/unit-tests.yml` is the machine
consumption of the documented complexity contract: every block must be radon
grade A (cyclomatic complexity <= 5), enforced by xenon with
``--max-absolute A --max-modules A --max-average A``. These tests execute the
gate's own shell script against synthetic source trees and assert whether CI
would pass or fail, which is what a developer pushing a pull request actually
experiences.

The script under test is taken verbatim from the workflow and is not modified
here; only the `uv` launcher is shimmed so it can run against a temporary
source tree with the interpreter running the test suite.
"""

import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "unit-tests.yml"


def _gate_script() -> str:
    """Return the shell script the complexity job executes in CI."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    run_scripts = [step["run"] for step in workflow["jobs"]["complexity"]["steps"] if "xenon" in step.get("run", "")]
    assert len(run_scripts) == 1, f"expected one xenon step, found {len(run_scripts)}"
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


def _run_gate(tmp_path: Path, sources: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real CI gate script against a throwaway source tree."""
    src = tmp_path / "src"
    src.mkdir()
    for filename, body in sources.items():
        (src / filename).write_text(body)

    # Shim `uv run xenon ...` onto the interpreter that has xenon installed, so
    # the gate script runs unmodified against the throwaway tree.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        'shift 2  # drop "run" and the xenon launcher; use the module below\n'
        f'exec "{sys.executable}" -m xenon "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-e", "-c", _gate_script()],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_gate_passes_for_compliant_code(tmp_path):
    """Code entirely within the documented grade A ceiling passes CI."""
    result = _run_gate(tmp_path, {"clean.py": _function("simple", 1)})
    assert result.returncode == 0, f"gate rejected compliant code:\n{result.stdout}{result.stderr}"


def test_gate_passes_at_grade_a_boundary(tmp_path):
    """Complexity 5 is the documented ceiling (grade A) and is accepted."""
    result = _run_gate(tmp_path, {"boundary_a.py": _function("at_limit", 4)})
    assert result.returncode == 0, f"gate rejected complexity 5 (grade A):\n{result.stdout}{result.stderr}"


def test_gate_blocks_function_above_grade_a(tmp_path):
    """Complexity 6 is grade B; CI must fail and name the offending function."""
    result = _run_gate(tmp_path, {"boundary_b.py": _function("over_limit", 5)})
    assert result.returncode != 0, "gate accepted complexity 6 (grade B)"
    output = result.stdout + result.stderr
    assert "over_limit" in output, f"gate did not report the offending function:\n{output}"
    assert "rank of B" in output, f"gate did not grade the offending block:\n{output}"


def test_gate_blocks_method_above_grade_a_inside_class(tmp_path):
    """A grade B method inside a class is a block too; the class shape must not hide it."""
    method = textwrap.indent(_function("method", 5), "    ")
    result = _run_gate(tmp_path, {"inside_class.py": f"class Wrapper:\n{method}"})
    assert result.returncode != 0, "gate accepted a grade B method inside a class"
    output = result.stdout + result.stderr
    assert "inside_class.py:2 method" in output, f"gate did not report the offending method:\n{output}"


def test_gate_command_pins_grade_a():
    """The gate command itself is the contract; loosening it to B must fail here."""
    script = _gate_script()
    assert "--max-absolute A" in script
    assert "--max-modules A" in script
    assert "--max-average A" in script
