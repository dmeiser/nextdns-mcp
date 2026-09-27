"""Accuracy tests for the agent-facing rules in ``AGENT.md`` (issue #274).

``.github/copilot-instructions.md`` tells Copilot to follow ``AGENT.md``
literally, so any rule pointing at a file, tool, or mechanism that does not
exist makes agents write code against a phantom API. These tests keep the
document tied to the repository it describes.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_MD = REPO_ROOT / "AGENT.md"


@pytest.fixture(scope="module")
def agent_md() -> str:
    """Return the text of AGENT.md."""
    assert AGENT_MD.exists(), "AGENT.md must exist in the repository"
    return AGENT_MD.read_text(encoding="utf-8")


def test_title_matches_file_name(agent_md: str) -> None:
    """The H1 title must name the file agents actually have to open."""
    assert agent_md.splitlines()[0] == "# AGENT.md — Rules and behaviors for AI agents"


def test_no_reference_to_missing_todo_file(agent_md: str) -> None:
    """`TODO.md` is not in the repository, so no rule may require editing it."""
    assert "TODO.md" not in agent_md


def test_replacement_documented_against_manage_lists(agent_md: str) -> None:
    """List replacement is `manageLists(operation="replace", entries=...)`.

    The removed `replaceDenylist`/`replaceAllowlist` tools and the
    `body=[...]` argument must not come back as instructions.
    """
    assert "replaceDenylist" not in agent_md
    assert "replaceAllowlist" not in agent_md
    assert "Array-Body" not in agent_md
    assert 'manageLists(operation="replace", entries=' in agent_md


def test_integration_section_matches_reality(agent_md: str) -> None:
    """The testing section must not promise OpenAPI loading or integration tests."""
    assert "OpenAPI loading" not in agent_md
    assert "tests/integration/" in agent_md
    assert "run_container_e2e.py" in agent_md


def test_write_safety_names_the_real_enforcement(agent_md: str) -> None:
    """Write scoping is enforced by env vars, not by a phantom test-profile registry."""
    assert "designated test profiles" not in agent_md
    assert "NEXTDNS_WRITABLE_PROFILES" in agent_md
    assert "NEXTDNS_READ_ONLY" in agent_md
