"""Regression tests for repository hygiene: #416 (.worktrees/), #594 (artifacts/)."""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_worktrees_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert ".worktrees/" in gitignore.splitlines()


def test_artifacts_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert "artifacts/" in gitignore.splitlines()


def test_artifacts_rule_declares_no_reinclusions():
    """Issue #594: a ``!artifacts/...`` pattern below ``artifacts/`` is inert config.

    Git cannot re-include a file whose parent directory is excluded, so the old
    ``!artifacts/README.md`` never made that file addable — it only implied a
    tracked file the repository does not have. If a future change wants to track
    something in ``artifacts/``, it has to widen the directory rule to
    ``artifacts/*`` first; adding a negation back without that keeps the pair inert.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    inert = [line for line in gitignore.splitlines() if line.startswith("!artifacts/")]
    assert inert == []


def test_worktrees_ruff_excluded():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    excludes = data["tool"]["ruff"].get("extend-exclude", [])
    assert ".worktrees" in excludes


def test_env_example_documents_nextdns_api_base():
    env_example = (REPO_ROOT / ".env.example").read_text()
    assert "# NEXTDNS_API_BASE=https://api.nextdns.io" in env_example
