"""Lockfile-sync regression tests for the automated version bump (issue #268).

The release version lives in five tracked files: ``pyproject.toml``,
``src/nextdns_mcp/__init__.py``, ``Dockerfile``, ``Dockerfile.alpine`` and
``uv.lock`` (``[[package]] name = "nextdns-mcp"``). Every job in
``unit-tests.yml`` runs ``uv lock --check``, so a release commit that rewrites
the version in the four human-maintained files but leaves the lockfile pinned
to the old version fails all four CI jobs -- on an auto-generated commit whose
failure no maintainer would guess without knowing to run ``uv lock``.

These tests do not grep the workflow YAML for one literal string. They load
each release workflow into a typed step model, resolve the *ordered* steps of
the job that bumps and commits the version, and then execute that job the way a
runner does, against a scratch project:

1. the scratch project is a dependency-free copy of the release-relevant file
   set, so ``uv lock`` resolves with no network access and no cache;
2. the workflow's own version-rewrite step runs there, which reproduces the
   defect: the lockfile still pins the pre-bump version and the CI command
   ``uv lock --check`` fails;
3. the workflow's own lock-refresh step must then run, after the rewrite and
   before the commit, and leave ``uv lock --check`` passing with the new version
   pinned;
4. the commit step then runs against a local ``origin``, and the commit it
   pushes must carry the refreshed ``uv.lock``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
LOCKFILE = "uv.lock"
PROJECT_NAME = "nextdns-mcp"

#: Release workflow file -> the job that bumps the version and commits it.
RELEASE_JOBS: dict[str, str] = {
    "version-bumper.yml": "bump-version",
    "weekly-maintenance.yml": "prepare",
}

#: Scratch-project manifests: dependency-free so ``uv lock`` needs no network.
_SCRATCH_PYPROJECT = '[project]\nname = "{name}"\nversion = "{version}"\nrequires-python = ">=3.12"\n'
_SCRATCH_INIT = '__version__ = "{version}"\n'
_SCRATCH_DOCKERFILE = 'LABEL org.opencontainers.image.version="{version}"\n'

#: A ``uv lock`` invocation and the flags it was given on that line.
_UV_LOCK = re.compile(r"(?:^|[\s;&|(`])uv\s+lock(?P<flags>[^\n]*)")

requires_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="resolving a lockfile requires uv")


# --------------------------------------------------------------------------- #
# Typed workflow model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Step:
    index: int
    name: str
    uses: str | None
    run: str | None


@dataclass(frozen=True)
class ReleaseJob:
    """The ordered steps of the job that bumps the version and commits it."""

    workflow: str
    job_id: str
    steps: tuple[Step, ...]

    def first(self, matches: Callable[[Step], bool]) -> Step | None:
        return next((step for step in self.steps if matches(step)), None)

    def version_edit(self) -> Step | None:
        """The step that rewrites the version in the non-lockfile files."""
        return self.first(lambda s: bool(s.run) and "re.sub(" in s.run and "pyproject.toml" in s.run)

    def lock_refresh(self) -> Step | None:
        """The first step that runs ``uv lock`` in a mode that *writes* it.

        ``uv lock --check`` only validates, so it cannot repair a stale lockfile
        and does not count as a refresh.
        """
        return self.first(
            lambda s: bool(s.run) and any("--check" not in m.group("flags").split() for m in _UV_LOCK.finditer(s.run))
        )

    def commit(self) -> Step | None:
        return self.first(lambda s: bool(s.run) and "git commit" in s.run)


def load_release_job(path: Path, job_id: str) -> ReleaseJob:
    """Parse a workflow file and resolve one job's steps in execution order."""
    raw = yaml.safe_load(path.read_text())
    steps = [
        Step(
            index=index,
            name=str(step.get("name") or step.get("uses") or "<unnamed>"),
            uses=step.get("uses"),
            run=step.get("run"),
        )
        for index, step in enumerate((raw.get("jobs") or {})[job_id].get("steps") or [])
    ]
    return ReleaseJob(workflow=path.name, job_id=job_id, steps=tuple(steps))


# --------------------------------------------------------------------------- #
# Scratch project: a dependency-free stand-in for the released repository
# --------------------------------------------------------------------------- #
def locked_version(root: Path) -> str:
    """The version ``uv.lock`` pins for this project, read as TOML."""
    with (root / LOCKFILE).open("rb") as handle:
        packages = tomllib.load(handle)["package"]
    pinned = [str(pkg["version"]) for pkg in packages if pkg["name"] == PROJECT_NAME]
    assert pinned, f"{LOCKFILE} pins no version for {PROJECT_NAME}"
    return pinned[0]


def manifest_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _uv(root: Path, cache: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run uv against the scratch project, offline and with a cold cache."""
    return subprocess.run(
        ["uv", *args],
        cwd=root,
        env={**os.environ, "UV_CACHE_DIR": str(cache), "UV_OFFLINE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def write_scratch_project(root: Path, cache: Path, version: str) -> None:
    """Materialise the release-relevant files plus a lockfile pinned to ``version``."""
    (root / "pyproject.toml").write_text(_SCRATCH_PYPROJECT.format(name=PROJECT_NAME, version=version))
    (root / "src" / "nextdns_mcp").mkdir(parents=True)
    (root / "src" / "nextdns_mcp" / "__init__.py").write_text(_SCRATCH_INIT.format(version=version))
    for name in ("Dockerfile", "Dockerfile.alpine"):
        (root / name).write_text(_SCRATCH_DOCKERFILE.format(version=version))
    # The baseline the bump script reads the current version from.
    baseline = root / "main-ref"
    baseline.mkdir()
    (baseline / "pyproject.toml").write_text(_SCRATCH_PYPROJECT.format(name=PROJECT_NAME, version=version))
    locked = _uv(root, cache, "lock")
    assert locked.returncode == 0, f"could not seed a scratch lockfile:\n{locked.stdout}\n{locked.stderr}"


def run_step(step: Step, root: Path, outputs: Path, env: Mapping[str, str] | None = None) -> None:
    """Execute one ``run`` step's shell body the way the runner would."""
    # The runner interpolates ``${{ ... }}`` expressions before bash sees them.
    body = re.sub(r"\$\{\{[^}]*\}\}", "scratch", step.run or "")
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", body],
        cwd=root,
        env={
            **os.environ,
            "BUMP_LABEL": "bump-patch",
            "GITHUB_OUTPUT": str(outputs),
            "UV_CACHE_DIR": str(root / ".uv-cache"),
            "UV_OFFLINE": "1",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"step {step.name!r} failed:\n{proc.stdout}\n{proc.stderr}"


def ci_lockfile_check_passes(root: Path, cache: Path) -> bool:
    """True when the check every CI job runs (``uv lock --check``) succeeds."""
    return _uv(root, cache, "lock", "--check").returncode == 0


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def init_scratch_repo(root: Path, origin: Path) -> None:
    """Give the scratch tree a commit history and an ``origin`` the release step can push to."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "init", "-q", "--bare", str(origin))
    _git(root, "config", "user.name", "scratch")
    _git(root, "config", "user.email", "scratch@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "baseline")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")


def committed_paths(root: Path) -> set[str]:
    """The files the release commit actually carries."""
    shown = _git(root, "show", "--name-only", "--pretty=format:", "HEAD").stdout
    return {line for line in shown.splitlines() if line}


def _scratch_version(tmp_path: Path) -> tuple[Path, Path]:
    """Build the scratch project; returns its root and its uv cache directory."""
    root = tmp_path / "repo"
    root.mkdir()
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    write_scratch_project(root, cache, _REPO_VERSION)
    return root, cache


def _repo_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


_REPO_VERSION = _repo_version()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_lockfile_pins_the_project_version() -> None:
    """Ground the tests: ``uv.lock`` is a fifth copy of the release version."""
    pinned = locked_version(REPO_ROOT)

    assert pinned == manifest_version(REPO_ROOT), (
        f"{LOCKFILE} pins {pinned} but pyproject.toml declares {manifest_version(REPO_ROOT)}; "
        "the bump workflows are the only thing that keeps these in step"
    )


@pytest.mark.parametrize("workflow_name,job_id", sorted(RELEASE_JOBS.items()))
def test_bump_refreshes_lockfile_before_the_release_commit(workflow_name: str, job_id: str) -> None:
    """The lock refresh must sit between the version rewrite and the commit."""
    job = load_release_job(WORKFLOWS_DIR / workflow_name, job_id)
    edit, refresh, commit = job.version_edit(), job.lock_refresh(), job.commit()

    assert edit is not None, f"{workflow_name}:{job_id} has no version-rewrite step"
    assert commit is not None, f"{workflow_name}:{job_id} has no release commit step"
    assert refresh is not None, (
        f"{workflow_name}:{job_id} rewrites the version without running `uv lock`, so {LOCKFILE} keeps "
        f"pinning the previous release and `uv lock --check` fails in every unit-tests.yml job"
    )
    assert edit.index < refresh.index < commit.index, (
        f"{workflow_name}:{job_id} order is wrong: the lock must be refreshed after the rewrite "
        f"(step {edit.index}) and before the commit (step {commit.index}), not at step {refresh.index}"
    )


@requires_uv
@pytest.mark.parametrize("workflow_name,job_id", sorted(RELEASE_JOBS.items()))
def test_bump_leaves_the_ci_lockfile_check_passing(workflow_name: str, job_id: str, tmp_path: Path) -> None:
    """End-to-end: run the job's own steps, and CI's ``uv lock --check`` must pass."""
    job = load_release_job(WORKFLOWS_DIR / workflow_name, job_id)
    edit, refresh, commit = job.version_edit(), job.lock_refresh(), job.commit()
    assert edit is not None and commit is not None, f"{workflow_name}:{job_id} is missing a bump or commit step"
    assert refresh is not None, (
        f"{workflow_name}:{job_id} never runs `uv lock`, so the release commit leaves a stale lockfile"
    )

    root, cache = _scratch_version(tmp_path)
    init_scratch_repo(root, tmp_path / "origin.git")
    outputs = tmp_path / "github-output"

    run_step(edit, root, outputs)

    bumped = manifest_version(root)
    assert bumped != _REPO_VERSION, f"the version-rewrite step did not bump {_REPO_VERSION}"
    # Reproduce the defect: the rewrite alone leaves CI's lockfile check failing.
    assert not ci_lockfile_check_passes(root, cache), (
        f"{workflow_name}:{job_id} did not reproduce the stale-lockfile failure, so the rest of this "
        "test would pass without proving anything"
    )

    run_step(refresh, root, outputs)

    assert ci_lockfile_check_passes(root, cache), (
        f"{workflow_name}:{job_id} ran {refresh.name!r} but `uv lock --check` still fails; the release "
        "commit would fail all four unit-tests.yml jobs"
    )
    assert locked_version(root) == bumped, (
        f"{LOCKFILE} pins {locked_version(root)} but the release commit declares {bumped}"
    )

    run_step(
        commit,
        root,
        outputs,
        env={"HEAD_REF": "main", "NEXT_VERSION": bumped, "NEW_VERSION": bumped, "CHANGES": "scratch"},
    )

    released = committed_paths(root)
    assert LOCKFILE in released, (
        f"{workflow_name}:{job_id} released {sorted(released)} without {LOCKFILE}; "
        "the refreshed lockfile would be left behind and CI would still fail"
    )


def test_check_only_invocation_is_not_a_lock_refresh(tmp_path: Path) -> None:
    """Guard the guard: ``uv lock --check`` validates, it cannot repair a lockfile."""
    spec: dict[str, Any] = {
        "jobs": {
            "bump-version": {
                "steps": [
                    {"name": "Calculate and Update Version", "run": "re.sub(..., pyproject.toml)"},
                    {"name": "Check lockfile", "run": "uv lock --check"},
                ]
            }
        }
    }
    job = load_release_job(_spec_workflow(tmp_path, spec), "bump-version")

    assert job.lock_refresh() is None, "a `uv lock --check` step was mistaken for a lockfile refresh"


def test_pre_fix_bump_workflow_is_detected_as_regression(tmp_path: Path) -> None:
    """Guard the guard: the pre-fix version-bumper must be read as a bump with no lock refresh."""
    pre_fix: dict[str, Any] = {
        "jobs": {
            "bump-version": {
                "steps": [
                    {"name": "Calculate and Update Version", "run": 're.sub(..., "pyproject.toml", ...)'},
                    {"name": "Commit and Push", "run": "git commit -m chore"},
                ]
            }
        }
    }
    job = load_release_job(_spec_workflow(tmp_path, pre_fix), "bump-version")

    assert job.version_edit() is not None and job.commit() is not None
    assert job.lock_refresh() is None, "the pre-fix workflow was not recognised as missing its lock refresh"


def _spec_workflow(tmp_path: Path, spec: dict[str, Any]) -> Path:
    """Write a throwaway workflow file and return its path."""
    path = tmp_path / "version-bumper.yml"
    path.write_text(yaml.safe_dump(spec))
    return path
