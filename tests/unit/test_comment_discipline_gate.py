"""Behavioral tests for the comment-only gate in CI.

The `Comment Discipline` workflow (`.github/workflows/comment-discipline.yml`) is
the machine consumption of the `## Comments` contract in `AGENT.md`: a pull
request whose entire diff is comments, blank lines, or docs prose is rejected
unless it carries the `comment-only` label. These tests execute the workflow's
own step, verbatim, against throwaway git repositories, which is what a
developer pushing a pull request actually experiences — and they pin the two
directions the card is judged on: the gate must reject an unlabelled
comment-only diff, and it must never reject a diff that contains code.

The step under test is taken from the workflow unmodified; only the `uv` launcher
is shimmed, so a change to how CI invokes the script fails here instead of
silently stopping the gate.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

import yaml

from scripts.check_comment_only_diff import DELETION_TEST, RULE

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "comment-discipline.yml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "check_comment_only_diff.py"
AGENT_GUIDE = REPO_ROOT / "AGENT.md"
COPILOT_STUB = REPO_ROOT / ".github" / "copilot-instructions.md"

BASE_MODULE = "def payload() -> int:\n    return 1\n"
NARRATION = "def payload() -> int:\n    # return the payload\n    return 1\n"


class Repo(NamedTuple):
    """A throwaway repository and the sha its PR would diff against."""

    path: Path
    base: str


def _gate_step() -> str:
    """The shell script the comment-only job executes in CI."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    run_scripts = [
        step["run"]
        for step in workflow["jobs"]["comment-only-diff"]["steps"]
        if "check_comment_only_diff" in step.get("run", "")
    ]
    assert len(run_scripts) == 1, f"expected one gate step, found {len(run_scripts)}"
    return run_scripts[0]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    # Identity is passed per command: `actions/checkout` configures none, and a
    # test that commits on the developer's own identity is not the test CI runs.
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Gate Test", "-c", "user.email=gate@example.com", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Repo:
    """A git repository holding one committed module and the gate script itself."""
    repo = tmp_path / "repo"
    (repo / "src" / "nextdns_mcp").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copyfile(GATE_SCRIPT, repo / "scripts" / GATE_SCRIPT.name)
    (repo / "src" / "nextdns_mcp" / "mod.py").write_text(BASE_MODULE, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, "base")
    return Repo(path=repo, base=base)


def _advance(repo: Repo, changes: dict[str, str], message: str) -> str:
    """Write `changes` on top of the base commit and return the new head sha."""
    for name, body in changes.items():
        target = repo.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return _commit(repo.path, message)


def _run_gate(repo: Repo, head: str, labels: str = "") -> subprocess.CompletedProcess[str]:
    """Run the real CI step against the throwaway repository."""
    bin_dir = repo.path / ".shim"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        'shift 3  # drop "run --no-project python"; run the script with this interpreter\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(repo.path),
        "PR_BASE_SHA": repo.base,
        "PR_HEAD_SHA": head,
        "PR_LABELS": labels,
    }
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-e", "-c", _gate_step()],
        cwd=repo.path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_comment_only_diff_is_rejected_without_the_label(tmp_path):
    """The oracle case: a bare `# return the payload` above a return, no label."""
    repo = _repo(tmp_path)
    head = _advance(repo, {"src/nextdns_mcp/mod.py": NARRATION}, "narration")

    result = _run_gate(repo, head)

    assert result.returncode != 0, f"gate accepted an unlabelled comment-only diff:\n{result.stdout}"
    assert "REJECTED" in result.stderr
    # The rejection has to carry the rule and the deletion test, not just a red X.
    assert RULE in result.stderr
    assert DELETION_TEST in result.stderr


def test_comment_only_diff_passes_with_the_label(tmp_path):
    """The label is the exception the gate has to allow to be taken."""
    repo = _repo(tmp_path)
    head = _advance(repo, {"src/nextdns_mcp/mod.py": NARRATION}, "narration")

    result = _run_gate(repo, head, labels="comment-only")

    assert result.returncode == 0, f"gate rejected a labelled comment-only diff:\n{result.stdout}{result.stderr}"


def test_comment_beside_a_code_change_passes_without_the_label(tmp_path):
    """A diff that is not comment-only is none of this gate's business."""
    repo = _repo(tmp_path)
    head = _advance(
        repo,
        {
            "src/nextdns_mcp/mod.py": "def payload() -> int:\n    # FastMCP applies middleware outside-in, so add order is load order.\n    return 1 + 1\n"
        },
        "why-comment plus code",
    )

    result = _run_gate(repo, head)

    assert result.returncode == 0, f"gate rejected a diff that changes code:\n{result.stdout}{result.stderr}"


def test_diff_with_no_comment_lines_is_never_rejected(tmp_path):
    """The one failure this gate must never cause: rejecting a plain code change."""
    repo = _repo(tmp_path)
    head = _advance(
        repo,
        {
            "src/nextdns_mcp/mod.py": "def payload() -> int:\n    return 2\n",
            "pyproject.toml": '[tool.ruff.lint]\nextend-select = ["I", "ERA", "TD", "FIX"]\n',
            ".github/workflows/build.yml": "name: Build\non: push\n",
            "Dockerfile": "FROM python:3.14-slim\n",
        },
        "code only",
    )

    result = _run_gate(repo, head)

    assert result.returncode == 0, f"gate rejected a diff containing no comments:\n{result.stdout}{result.stderr}"


def test_docs_only_diff_needs_the_label_too(tmp_path):
    """Docs prose counts as a comment: the rule is about why, not about file type."""
    repo = _repo(tmp_path)
    head = _advance(repo, {"AGENT.md": "# Rules\n\nA comment answers why, never what.\n"}, "prose only")

    unlabelled = _run_gate(repo, head)
    labelled = _run_gate(repo, head, labels="comment-only,documentation")

    assert unlabelled.returncode != 0, "gate accepted an unlabelled docs-only diff"
    assert labelled.returncode == 0, f"gate rejected a labelled docs-only diff:\n{labelled.stdout}{labelled.stderr}"


def test_gate_fails_closed_when_the_diff_cannot_be_read(tmp_path):
    """A gate that cannot see the diff must not wave the pull request through."""
    repo = _repo(tmp_path)
    head = _advance(repo, {"src/nextdns_mcp/mod.py": "def payload() -> int:\n    return 3\n"}, "code")

    assert _run_gate(repo, head).returncode == 0  # control: the same diff is green when it can be read

    repo = repo._replace(base="0" * 40)
    broken = _run_gate(repo, head)
    assert broken.returncode != 0, "gate passed a diff it could not read"
    assert "Cannot read the pull request diff" in broken.stderr


def test_agent_md_carries_the_rule_the_gate_quotes():
    """CI prints AGENT.md's words, so the two must not drift apart."""
    agent = AGENT_GUIDE.read_text(encoding="utf-8").replace("**", "")

    assert "## Comments" in agent
    assert RULE in agent
    assert DELETION_TEST in agent
    assert "### Delete" in agent and "### Keep" in agent
    assert "| commented-out code |" in agent
    assert "| a guard whose absence is invisible |" in agent


def test_coverage_rules_point_at_the_comments_section_instead_of_restating_it():
    """`# pragma: no cover` was the one clause with the right shape; it now cites the general rule."""
    agent = AGENT_GUIDE.read_text(encoding="utf-8")
    pragma_lines = [line for line in agent.splitlines() if "pragma: no cover" in line]
    gap_lines = [line for line in agent.splitlines() if "intentional gap" in line.lower()]

    assert pragma_lines and gap_lines
    for line in [*pragma_lines, *gap_lines]:
        assert "**Comments** section" in line, f"coverage rule restates the comment rule instead of citing it: {line}"


def test_copilot_instructions_stay_a_delegation_stub():
    """One home for the rule: Copilot reaches it through `AGENT.md`, never through a copy."""
    stub = COPILOT_STUB.read_text(encoding="utf-8")

    assert "AGENT.md" in stub
    assert RULE not in stub


def test_ruff_selects_the_mechanical_comment_rules():
    """ERA/TD/FIX in `extend-select` is what makes commented-out code a CI failure."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    selected = set(config["tool"]["ruff"]["lint"]["extend-select"])

    assert {"ERA", "TD", "FIX"} <= selected, f"comment gate missing from ruff: {sorted(selected)}"
