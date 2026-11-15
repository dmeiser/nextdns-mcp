"""Regression tests for the Revert Watchdog workflow (WORK-199).

The `revert` job in `.github/workflows/revert-watchdog.yml` opens an
`auto-revert/<sha>` PR when post-merge validation of a `main` push fails. On
2026-11-14 it aborted at its first git command with exit 128 and
"fatal: not in a git directory" (run 43648601948, job 132611826029): the job
had a single `run:` step and no `actions/checkout`, so it executed git in an
empty workspace. The backstop the workflow exists for could never have fired.

These tests pin the invariants that made the difference, the same way
`test_complexity_gate.py` and `test_coverage_branch_gate.py` pin CI contracts:

* the `revert` job checks the repository out with full history before any git
  command — delete that step again and this file goes red;
* the job keeps the `contents: write` + `pull-requests: write` scopes its
  branch push and `gh pr create` need;
* the automated trigger still gates on a failed `push` run on `main`;
* the duplicate-PR guard still short-circuits when a revert PR is open.

Two behavioural tests run the job's shell script verbatim: once inside a real
scratch repository (the fixed flow: branch created, revert committed, PR
opened against `main`) and once in an empty workspace (the reported failure
mode, reproduced at exit 128). Only `gh` is shimmed, so every git command the
CI job runs is the real one.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "revert-watchdog.yml"


# Shim for `gh`: records every invocation (NUL-separated) and answers the
# duplicate-PR guard's `gh pr list` with the given count. `pr create` records
# its arguments and succeeds.
def _gh_shim(pr_count: int) -> str:
    return (
        "#!/bin/sh\n"
        "printf 'call\\0' >> \"$GH_CALLS\"\n"
        'for arg in "$@"; do printf \'%s\\0\' "$arg" >> "$GH_CALLS"; done\n'
        'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then\n'
        f"    echo {pr_count}\n"
        "fi\n"
        "exit 0\n"
    )


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _revert_job() -> dict:
    return _workflow()["jobs"]["revert"]


def _revert_script() -> str:
    """Return the shell script the revert job executes, verbatim."""
    scripts = [step["run"] for step in _revert_job()["steps"] if "git revert" in step.get("run", "")]
    assert len(scripts) == 1, f"expected exactly one revert script step, found {len(scripts)}"
    return scripts[0]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _scratch_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """A bare origin plus a working clone on `main`, with a revertable head commit."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], capture_output=True, check=True)
    (work / "app.py").write_text("print('validated')\n")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "baseline commit that passed validation")
    _git(work, "push", "-u", "origin", "main")
    (work / "app.py").write_text("print('validated')\nraise SystemExit('bad merge')\n")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "bad change that post-merge validation rejects")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "origin", "main")
    return origin, work, sha


def _run_revert_script(
    cwd: Path, tmp_path: Path, head_sha: str, pr_count: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Execute the job's script with the real git and a shimmed gh."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(_gh_shim(pr_count))
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    calls_file = tmp_path / "gh_calls"
    result = subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-e", "-c", _revert_script()],
        cwd=cwd,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "GH_CALLS": str(calls_file),
            "GH_TOKEN": "shimmed",
            "REPO": "example/nextdns-mcp",
            "HEAD_SHA": head_sha,
            "RUN_URL": "https://example.invalid/actions/run/1",
            "RUN_NAME": "E2E Container",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls: list[list[str]] = []
    if calls_file.exists():
        current: list[str] = []
        for part in calls_file.read_text().split("\x00"):
            if part == "call":
                if current:
                    calls.append(current)
                current = []
            elif part:
                current.append(part)
        if current:
            calls.append(current)
    return result, calls


def test_revert_job_checks_out_the_repository_with_full_history():
    """The git commands need a real repository; their absence is the WORK-199 failure."""
    steps = _revert_job()["steps"]
    assert steps, "the revert job has no steps"
    first = steps[0]
    assert str(first.get("uses", "")).startswith("actions/checkout@"), (
        "the revert job's first step must be actions/checkout; without a checkout the "
        "workspace is empty and git aborts with 'fatal: not in a git directory' (exit 128)"
    )
    assert (first.get("with") or {}).get("fetch-depth") == 0, (
        "the checkout must fetch full history so git revert can see the failing commit and its parent"
    )


def test_job_keeps_write_scopes_for_the_branch_push_and_revert_pr():
    """Pushing auto-revert/* and opening the PR require both write scopes."""
    permissions = _workflow().get("permissions") or {}
    assert permissions.get("contents") == "write", "the job needs contents: write to push the auto-revert branch"
    assert permissions.get("pull-requests") == "write", "the job needs pull-requests: write to open the revert PR"


def test_automated_trigger_still_gates_on_a_failed_push_run_on_main():
    """The backstop must fire only for a red post-merge run on main; green runs still skip."""
    triggers = _workflow().get(True) or _workflow().get("on") or {}
    run_trigger = triggers["workflow_run"]
    assert {"E2E Container", "Unit Tests"} <= set(run_trigger["workflows"]), (
        "the watchdog must watch the post-merge validation workflows"
    )
    assert run_trigger["types"] == ["completed"], "the watchdog must fire on completion, not start"
    assert run_trigger["branches"] == ["main"], "the watchdog must watch main only"
    condition = str(_revert_job()["if"])
    for clause in (
        "github.event.workflow_run.conclusion == 'failure'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
    ):
        assert clause in condition, f"the job condition lost the guard {clause!r}; green or non-push runs would fire it"


def test_duplicate_pr_guard_is_part_of_the_job_script():
    """The guard text the job relies on to stay idempotent is present."""
    script = _revert_script()
    assert 'gh pr list --repo "$REPO" --state open --search "$HEAD_SHA"' in script
    assert "already open" in script, "the guard must short-circuit when a revert PR for the SHA is already open"


def test_guard_short_circuits_when_a_revert_pr_is_already_open(tmp_path):
    """The skip branch of the guard: an open revert PR means no branch, no second PR."""
    origin, work, sha = _scratch_repo(tmp_path)
    result, calls = _run_revert_script(work, tmp_path, sha, pr_count=1)
    assert result.returncode == 0, f"guard path aborted: {result.stdout}{result.stderr}"
    assert "already open" in result.stdout, result.stdout
    assert not any(call[:2] == ["pr", "create"] for call in calls), "a second revert PR must not be opened"
    branches = subprocess.run(
        ["git", "--git-dir", str(origin), "for-each-ref", "--format=%(refname)", "refs/heads/"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "auto-revert" not in branches.stdout, f"the guard must run before any push:\n{branches.stdout}"


def test_revert_flow_creates_branch_and_revert_pr_from_a_real_repository(tmp_path):
    """The fixed flow, end to end: revert commit, auto-revert branch pushed, PR opened on main."""
    origin, work, sha = _scratch_repo(tmp_path)
    result, calls = _run_revert_script(work, tmp_path, sha)
    assert result.returncode == 0, f"the revert flow failed:\n{result.stdout}{result.stderr}"
    branch = f"auto-revert/{sha[:8]}"
    _git(origin, "rev-parse", "--verify", branch)
    create = next((call for call in calls if call[:2] == ["pr", "create"]), None)
    assert create is not None, f"gh pr create never ran; gh calls: {calls}"
    assert create[create.index("--base") + 1] == "main", "the revert PR must target main"
    assert create[create.index("--head") + 1] == branch, "the revert PR must carry the pushed auto-revert branch"
    reverted = _git(work, "show", f"{branch}:app.py").stdout
    assert reverted == "print('validated')\n", "git revert must cancel the bad commit's change"


def test_script_in_an_empty_workspace_reproduces_the_original_failure(tmp_path):
    """The reported incident, replayed: git outside a repository is fatal at exit 128.

    Paired with the checkout-step test, this is what keeps the defect from
    silently returning: the failure mode here is exactly why that step exists.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    result, _ = _run_revert_script(empty, tmp_path, "0" * 40)
    assert result.returncode == 128, f"expected exit 128 from git outside a repository, got {result.returncode}"
    assert "not in a git directory" in result.stdout + result.stderr, (
        "the empty-workspace failure must match the reported run 43648601948 signature"
    )
