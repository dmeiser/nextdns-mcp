"""Regression tests for issue #416: .worktrees/ hygiene."""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_worktrees_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert ".worktrees/" in gitignore.splitlines()


def test_worktrees_ruff_excluded():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    excludes = data["tool"]["ruff"].get("extend-exclude", [])
    assert ".worktrees" in excludes


def test_env_example_documents_nextdns_api_base():
    env_example = (REPO_ROOT / ".env.example").read_text()
    assert "# NEXTDNS_API_BASE=https://api.nextdns.io" in env_example
