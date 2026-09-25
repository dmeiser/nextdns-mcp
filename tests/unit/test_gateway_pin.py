"""Test for the pinned MCP Gateway checkout (GitHub issue #138, second finding).

The E2E gateway workflow historically cloned ``docker/mcp-gateway`` at mutable
``HEAD``, so an upstream push could change what the E2E build tested (a
supply-chain risk of the same class the merge gate is about). The fix pins the
checkout to a release tag.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "e2e-mcp-gateway.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _build_step(workflow: dict) -> dict:
    steps = workflow["jobs"]["e2e"]["steps"]
    for s in steps:
        if s.get("name") == "Build docker-mcp plugin from source":
            return s
    raise AssertionError("expected the 'Build docker-mcp plugin from source' step to exist")


def test_gateway_checkout_is_pinned_not_head(workflow: dict) -> None:
    """The MCP Gateway clone must pin an explicit ref, not follow mutable HEAD."""
    step = _build_step(workflow)
    run = step.get("run", "")
    assert "git clone" in run
    # A pinned clone names a branch/tag. Cloning without --branch follows HEAD.
    assert "--branch" in run, "the gateway clone must pin a ref with --branch (not follow HEAD)"
    # The pinned ref must be declared, not a bare HEAD/lazy default.
    assert "MCP_GATEWAY_REF" in run


def test_gateway_ref_is_a_release_tag(workflow: dict) -> None:
    """The pinned ref must be a concrete v<semver> release tag."""
    step = _build_step(workflow)
    env = step.get("env", {})
    ref = env.get("MCP_GATEWAY_REF", "")
    import re

    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", ref), f"expected a v<semver> tag, got {ref!r}"
