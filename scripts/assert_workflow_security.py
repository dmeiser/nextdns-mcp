"""Assert E2E workflow security invariants for the MCP Gateway workflow.

Security contract (GitHub issue #137):

1. On ``pull_request`` events the workflow must never run with
   ``ALLOW_LIVE_WRITES=true``: ordinary PRs must not create or delete real
   NextDNS profiles under the repo secret. The ``pull_request`` guard must
   precede any reference to the (PR-controllable) dispatch input so a PR can
   never opt in.
2. The ``workflow_dispatch`` input may only ever resolve to ``true`` via an
   explicit maintainer choice, never as an implicit default (the input
   itself defaults to ``false``).
3. Secret injection must be gated to non-PR events: on ``pull_request`` the
   ``NEXTDNS_API_KEY`` / ``NEXTDNS_PLOT_PROFILE`` secret expressions must
   resolve to empty strings, so PR-controlled checkout contents never see
   the repo secret in the environment.

Usage:
    python scripts/assert_workflow_security.py [path/to/workflow.yml]

Defaults to ``.github/workflows/e2e-mcp-gateway.yml`` relative to the
repository root (parent of this script's directory). Exits non-zero with a
list of violations when any invariant is broken.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment without PyYAML
    sys.stderr.write("ERROR: PyYAML is required to run this assertion\n")
    sys.exit(2)

DEFAULT_WORKFLOW_REL = ".github/workflows/e2e-mcp-gateway.yml"
PR_EVENT = "pull_request"
LIVE_WITES_ENV = "ALLOW_LIVE_WRITES"
SECRET_ENVS = ("NEXTDNS_API_KEY", "NEXTDNS_PLOT_PROFILE")


def workflow_root(workflow: object) -> dict:
    """Return the parsed workflow as a dict or fail with a diagnostic."""
    if not isinstance(workflow, dict):
        raise TypeError(f"workflow root is a {type(workflow).__name__}, expected a mapping")
    return workflow


def live_writes_expr(workflow: dict) -> str | None:
    """Return the job-level ALLOW_LIVE_WRITES env expression, if declared."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return None
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        env = job.get("env")
        if not isinstance(env, dict):
            continue
        expr = env.get(LIVE_WITES_ENV)
        if isinstance(expr, str):
            return expr
    return None


def check_live_writes_gating(expr: str | None) -> list[str]:
    """Assert the ALLOW_LIVE_WRITES expression keeps PRs read-only.

    Required shape: a ``github.event_name == 'pull_request' && 'false'`` guard
    that precedes any ``github.event.inputs.allow_live_writes`` reference, so
    that on pull_request the expression resolves to ``false`` before the
    (PR-controllable) dispatch input is ever consulted. The expression must
    also contain an explicit ``'false'`` fallback so read-only mode is always
    reachable; the only path to ``true`` is an explicit maintainer choice on
    a non-PR event.
    """
    if expr is None:
        return [f"no job-level env expression found for {LIVE_WITES_ENV}; default must be pinned to false"]
    if not expr.lstrip().startswith("${{"):
        return [
            LIVE_WITES_ENV + " env is not a GitHub Actions ${{...}} expression; it must be computed per event: " + expr
        ]
    pr_guard = "github.event_name == 'pull_request' && 'false'"
    if pr_guard not in expr:
        return [f"{LIVE_WITES_ENV} expression is missing the pull_request guard: {expr}"]
    input_ref = expr.find("github.event.inputs.allow_live_writes")
    if input_ref != -1 and input_ref < expr.find(pr_guard):
        return [f"{LIVE_WITES_ENV} expression consults the dispatch input before the pull_request guard: {expr}"]
    # A read-only fallback must exist: the expression carries an explicit
    # "|| 'false'" (or ends in a 'false' literal) so the non-default branch
    # can never silently widen to live writes.
    if "|| 'false'" not in expr and not expr.rstrip().endswith("'false')}"):
        return [f"{LIVE_WITES_ENV} expression does not fall back to 'false': {expr}"]
    return []


def dispatch_default(workflow: dict) -> str | None:
    """Return the workflow_dispatch allow_live_writes input default, if declared."""
    on = workflow.get("on", workflow.get(True))  # PyYAML parses bare `on:` as True
    if not isinstance(on, dict):
        return None
    dispatch = on.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return None
    inputs = dispatch.get("inputs")
    if not isinstance(inputs, dict):
        return None
    allow = inputs.get("allow_live_writes")
    if not isinstance(allow, dict):
        return None
    default = allow.get("default")
    return str(default) if default is not None else None


def secret_expressions(workflow: dict) -> dict[str, list[str]]:
    """Collect every ``${{ ... }}`` expression that references a repo secret, per env name."""
    found: dict[str, list[str]] = {}
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return found

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and isinstance(value, str) and "${{" in value:
                    for secret in SECRET_ENVS:
                        if f"secrets.{secret}" in value:
                            found.setdefault(key, []).append(value)
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(jobs)
    return found


def check_pr_secret_gating(secret_refs: dict[str, list[str]]) -> list[str]:
    """Every secret expression used for a secret env must be empty on pull_request."""
    violations: list[str] = []
    for env_name, exprs in secret_refs.items():
        for expr in exprs:
            # The expression must short-circuit to '' on pull_request. Accept
            # the canonical guarded form: `... && '' || secrets.<NAME>` where
            # the pull_request test precedes the secret reference.
            if PR_EVENT not in expr:
                violations.append(f"{env_name}: secret expression is not gated to non-PR events: {expr}")
                continue
            empty_part, _, secret_part = expr.partition("||")
            if "secre" in empty_part:
                violations.append(
                    f"{env_name}: pull_request branch of the expression still references the secret: {expr}"
                )
            if "''" not in empty_part:
                violations.append(
                    f"{env_name}: pull_request branch of the expression does not resolve to empty: {expr}"
                )
            if "secrets." not in secret_part:
                violations.append(f"{env_name}: non-PR branch of the expression does not inject the secret: {expr}")
    return violations


def assert_security(workflow: dict) -> list[str]:
    """Return the list of violated invariants (empty list = safe)."""
    violations: list[str] = []

    # Invariant 1: pull_request never resolves ALLOW_LIVE_WRITES=true, even
    # when a PR-controlled dispatch input value is present.
    # Invariant 2: live writes stay restricted to an explicit maintainer
    # workflow_dispatch opt-in; no event defaults to true.
    violations.extend(check_live_writes_gating(live_writes_expr(workflow)))
    default = dispatch_default(workflow)
    if default == "true":
        violations.append(
            "workflow_dispatch allow_live_writes input defaults to 'true'; live writes must be an explicit maintainer opt-in"
        )

    # Invariant 3: secret injection gated to non-PR events.
    violations.extend(check_pr_secret_gating(secret_expressions(workflow)))

    return violations


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        workflow_path = Path(argv[1])
    else:
        workflow_path = Path(__file__).resolve().parent.parent / DEFAULT_WORKFLOW_REL
    if not workflow_path.is_file():
        sys.stderr.write(f"ERROR: workflow file not found: {workflow_path}\n")
        return 2

    with workflow_path.open("r", encoding="utf-8") as f:
        workflow = workflow_root(yaml.safe_load(f))

    violations = assert_security(workflow)
    if violations:
        sys.stderr.write(f"FAIL: {workflow_path.name} violates {len(violations)} security invariant(s):\n")
        for violation in violations:
            sys.stderr.write(f"  - {violation}\n")
        return 1

    print(f"OK: {workflow_path.name} satisfies the E2E security invariants (issue #137)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
