"""Tests for the E2E workflow security assertion (issue #137).

Pins the security contract of ``.github/workflows/e2e-mcp-gateway.yml``:

- ``pull_request`` runs must never resolve ``ALLOW_LIVE_WRITES=true``.
- Live writes are an explicit maintainer ``workflow_dispatch`` opt-in
  (the dispatch input must default to ``false``).
- ``NEXTDNS_API_KEY`` / ``NEXTDNS_PLOT_PROFILE`` secret injection must be
  gated to non-PR events.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
E2E_WORKFLOW = REPO_ROOT / ".github/workflows" / "e2e-mcp-gateway.yml"
ASSERT_SCRIPT = REPO_ROOT / "scripts" / "assert_workflow_security.py"


@pytest.fixture(scope="module")
def security() -> types.ModuleType:
    """Load scripts/assert_workflow_security.py as a module."""
    spec = importlib.util.spec_from_file_location("assert_workflow_security", ASSERT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def e2e_workflow() -> dict:
    with E2E_WORKFLOW.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_e2e_workflow_satisfies_security_invariants(security, e2e_workflow) -> None:
    assert security.assert_security(e2e_workflow) == []


def test_dispatch_input_defaults_to_false(security, e2e_workflow) -> None:
    assert security.dispatch_default(e2e_workflow) == "false"


def test_live_writes_expr_has_pull_request_guard(security, e2e_workflow) -> None:
    expr = security.live_writes_expr(e2e_workflow)
    assert expr is not None
    assert "github.event_name == 'pull_request' && 'false'" in expr
    # The guard must precede any consult of the PR-controllable dispatch input.
    guard = expr.find("github.event_name == 'pull_request' && 'false'")
    input_ref = expr.find("github.event.inputs.allow_live_writes")
    assert guard != -1
    assert input_ref == -1 or input_ref > guard


def test_vulnerable_live_writes_expr_is_rejected(security) -> None:
    # The pre-fix expression: PR-controllable input with a true default.
    vulnerable = "${{ github.event.inputs.allow_live_writes || 'true' }}"
    violations = security.check_live_writes_gating(vulnerable)
    assert any("missing the pull_request guard" in v for v in violations)


def test_input_consulted_before_guard_is_rejected(security) -> None:
    # A guard that is placed after the dispatch input would still let a
    # PR-controlled value leak through.
    misordered = "${{ github.event.inputs.allow_live_writes || (github.event_name == 'pull_request' && 'false') }}"
    violations = security.check_live_writes_gating(misordered)
    assert any("before the pull_request guard" in v for v in violations)


def test_true_fallback_is_rejected(security) -> None:
    # Even with a PR guard, a trailing 'true' fallback would make
    # push/schedule/workflow_call runs write by default.
    true_fallback = (
        "${{ github.event_name == 'pull_request' && 'false' || "
        "github.event.inputs.allow_live_writes == 'true' && 'true' || 'true' }}"
    )
    violations = security.check_live_writes_gating(true_fallback)
    assert any("does not fall back to 'false'" in v for v in violations)


def test_missing_expr_is_rejected(security) -> None:
    assert security.check_live_writes_gating(None) != []
    assert security.check_live_writes_gating("true") != []


def test_ungated_secret_reference_is_rejected(security) -> None:
    violations = security.check_pr_secret_gating({"NEXTDNS_API_KEY": ["${{ secrets.NEXTDNS_API_KEY }}"]})
    assert any("not gated to non-PR events" in v for v in violations)


def test_gated_secret_reference_is_accepted(security) -> None:
    guarded = "${{ github.event_name == 'pull_request' && '' || secrets.NEXTDNS_API_KEY }}"
    assert security.check_pr_secret_gating({"NEXTDNS_API_KEY": [guarded]}) == []


def test_secret_expressions_in_workflow_are_gated(security, e2e_workflow) -> None:
    refs = security.secret_expressions(e2e_workflow)
    assert set(refs) >= {"NEXTDNS_API_KEY", "NEXTDNS_PLOT_PROFILE"}
    assert security.check_pr_secret_gating(refs) == []
