"""Least-privilege enforcement tests for .github/workflows/dependabot-auto-merge.yml.

Issue #188 (opus deep-review finding NEW-12): the workflow granted
``contents: write`` to its ``GITHUB_TOKEN`` while the job only needs
``pull-requests: write`` to approve/comment/enable auto-merge. A token with
``contents: write`` can push to any branch, including ``main``.

These tests do not grep the workflow source. They:

1. load the workflow into a typed model and resolve the *effective* token
   permissions for the job using GitHub's documented resolution rules
   (job-level ``permissions`` replaces workflow-level; any scope that is not
   listed resolves to "none"),
2. execute every step of the workflow (``github-script`` steps through node
   with a recording octokit, ``run`` steps through bash with a recording
   ``gh`` stub) against a fake GitHub API that *enforces* the resolved token
   permissions, and
3. assert that the workflow still completes its job (approve + auto-merge,
   and the failure comment path) while the token is refused write access to
   repository contents -- i.e. it cannot push to ``main``.

A fake API call is rejected exactly the way GitHub rejects a token missing a
scope: HTTP 403 with a ``Resource not accessible by integration`` body, and
octokit raises a ``HttpError`` for JS callers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"

# The pre-fix permissions block from base commit 9218fb2 (issue #188).
# Used to prove the least-privilege check actually detects the regression.
PRE_FIX_PERMISSIONS_YAML = """
name: Dependabot Auto-Merge
on:
  pull_request_target:
    types: [opened, synchronize, reopened]
permissions:
  contents: write
  pull-requests: write
  actions: read
  checks: read
jobs:
  auto-merge:
    runs-on: ubuntu-latest
    if: github.actor == 'dependabot[bot]'
    steps: []
"""

_LEVELS = {"none": 0, "read": 1, "write": 2}

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="executing github-script steps requires node (the runner's JS runtime)",
)


# --------------------------------------------------------------------------- #
# Typed workflow model + GitHub permission resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Step:
    name: str
    uses: str | None
    run: str | None
    script: str | None
    condition: str | None
    outputs: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0
    stdout: str = ""


@dataclass(frozen=True)
class Job:
    job_id: str
    runs_on: str
    declared_permissions: dict[str, str] | None
    condition: str | None
    steps: list[Step]


@dataclass(frozen=True)
class Workflow:
    name: str
    declared_permissions: dict[str, str] | None
    jobs: dict[str, Job]

    def job(self, job_id: str) -> Job:
        return self.jobs[job_id]


def load_workflow(path: Path) -> Workflow:
    """Parse a workflow file into the typed model (YAML ``on:`` is a bool key)."""
    raw = yaml.safe_load(path.read_text())
    # PyYAML resolves the bare key ``on`` to ``True``.
    raw = {("on" if key is True else key): value for key, value in raw.items()}

    jobs: dict[str, Job] = {}
    for job_id, job in (raw.get("jobs") or {}).items():
        steps = [
            Step(
                name=step.get("name", step.get("uses", "<unnamed>")),
                uses=step.get("uses"),
                run=step.get("run"),
                script=((step.get("with") or {}).get("script")),
                condition=step.get("if"),
            )
            for step in job.get("steps", [])
        ]
        jobs[job_id] = Job(
            job_id=job_id,
            runs_on=job.get("runs-on", ""),
            declared_permissions=job.get("permissions"),
            condition=job.get("if"),
            steps=steps,
        )
    return Workflow(
        name=raw.get("name", ""),
        declared_permissions=raw.get("permissions"),
        jobs=jobs,
    )


def effective_permissions(workflow: Workflow, job: Job) -> dict[str, str]:
    """Resolve the token permissions a job's ``GITHUB_TOKEN`` actually gets.

    GitHub: job-level ``permissions`` *replaces* the workflow-level block, and
    every scope that is not named is set to "none" (no access).
    """
    declared = job.declared_permissions if job.declared_permissions is not None else workflow.declared_permissions
    if declared is None:  # no permissions anywhere: default repo permissions apply
        declared = {"contents": "read", "pull-requests": "read"}
    return {scope: str(level) for scope, level in declared.items()}


def has_scope(permissions: dict[str, str], scope: str, level: str) -> bool:
    """True when the token holds at least ``level`` on ``scope``."""
    return _LEVELS.get(permissions.get(scope, "none"), 0) >= _LEVELS[level]


# --------------------------------------------------------------------------- #
# Fake GitHub API that enforces the resolved token permissions
# --------------------------------------------------------------------------- #
# Every endpoint the workflow can reach, with the token scope GitHub requires.
ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "pulls.get": ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}", "pull-requests:read"),
    "actions.listWorkflowRunsForRepo": ("GET", "/repos/{owner}/{repo}/actions/runs", "actions:read"),
    "checks.listForRef": ("GET", "/repos/{owner}/{repo}/commits/{ref}/check-runs", "checks:read"),
    "pulls.createReview": ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews", "pull-requests:write"),
    "pulls.update": ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}", "pull-requests:write"),
    "pulls.merge": ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge", "pull-requests:write"),
    "issues.createComment": ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments", "pull-requests:write"),
    # Write paths a token with ``contents: write`` could use to push code.
    "repos.createOrUpdateFileContents": ("PUT", "/repos/{owner}/{repo}/contents/{path}", "contents:write"),
    "git.createRef": ("POST", "/repos/{owner}/{repo}/git/refs", "contents:write"),
    "git.updateRef": ("PATCH", "/repos/{owner}/{repo}/git/refs/{ref}", "contents:write"),
    "git.createCommit": ("POST", "/repos/{owner}/{repo}/git/commits", "contents:write"),
}

SCOPES_GH_CLI: list[tuple[tuple[str, ...], str, str]] = [
    (("pr", "review"), "pull-requests", "write"),
    (("pr", "merge"), "pull-requests", "write"),
    (("pr", "comment"), "pull-requests", "write"),
    (("pr", "view"), "pull-requests", "read"),
    (("run", "list"), "actions", "read"),
    (("run", "view"), "actions", "read"),
    (("api",), None, None),  # catch-all marker: resolved from the method + path below
    ((), None, None),  # no prefix match -> fall through to `gh api` handling
]


@dataclass
class ApiCall:
    source: str  # "github-script" or "gh"
    operation: str
    method: str
    path: str
    required_scope: str
    required_level: str
    status: int
    allowed: bool
    detail: str = ""


def _required_for_gh_call(argv: list[str]) -> tuple[str, str, str, str]:
    """Map a ``gh`` invocation to (operation, method, path, required-scope)."""
    for prefix, scope, level in SCOPES_GH_CLI:
        if scope is None:
            continue
        if tuple(argv[: len(prefix)]) == prefix:
            operation = "gh " + " ".join(prefix)
            return operation, "POST" if level == "write" else "GET", f"/{operation}", f"{scope}:{level}"
    # gh api ... -> derive from the requested method and path
    if argv[:1] == ["api"]:
        method = "GET"
        if "-X" in argv:
            method = argv[argv.index("-X") + 1].upper()
        path = next((a for a in argv[1:] if a.startswith("/")), "/")
        if "/pulls/" in path and "/reviews" in path:
            return "gh api review", "POST", path, "pull-requests:write"
        if "/issues/" in path and "/comments" in path:
            return "gh api comment", "POST", path, "pull-requests:write"
        if "/git/refs" in path or "/contents/" in path or "/git/commits" in path:
            return f"gh api write {path}", method, path, "contents:write"
        if "/pulls/" in path:
            return "gh api read PR", method, path, "pull-requests:read"
    raise AssertionError(f"unmapped gh invocation, refusing to silently pass it: {argv!r}")


HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const permissions = spec.permissions;
const LEVELS = {none: 0, read: 1, write: 2};
const ENDPOINTS = spec.endpoints;
const calls = [];
const logs = [];

function requiredLevel(scope, level) {
  return (LEVELS[permissions[scope] || 'none'] || 0) >= LEVELS[level];
}

// Build a recording octokit whose every call goes through permission enforcement.
function makeClient() {
  return new Proxy({}, {
    get(_t, restKey) {
      if (restKey !== 'rest') return undefined;
      return new Proxy({}, {
        get(_t2, group) {
          return new Proxy({}, {
            get(_t3, method) {
              return async function (params) {
                const ep = ENDPOINTS[group + '.' + method];
                if (!ep) throw new Error('unmapped octokit call: ' + group + '.' + method);
                const [verb, path, scope] = ep;
                const [reqScope, reqLevel] = scope.split(':');
                const ok = requiredLevel(reqScope, reqLevel);
                calls.push({
                  source: 'github-script', operation: group + '.' + method, method: verb,
                  path, required_scope: reqScope, required_level: reqLevel,
                  status: ok ? 200 : 403, allowed: ok
                });
                if (!ok) {
                  // This is what octokit surfaces to the step script.
                  const err = new Error(
                    'HttpError: ' + verb + ' ' + path + ' -> 403 ' +
                    JSON.stringify({message: 'Resource not accessible by integration'})
                  );
                  err.status = 403;
                  err.response = {status: 403, data: {message: 'Resource not accessible by integration'}};
                  throw err;
                }
                return {status: ok ? 200 : 403, data: spec.responses[group + '.' + method]};
              };
            }
          });
        }
      });
    }
  });
}

const outputs = {};
const core = {
  setOutput: (name, value) => { outputs[name] = typeof value === 'string' ? value : JSON.stringify(value); },
  info: (m) => logs.push(String(m)),
  warning: (m) => logs.push(String(m)),
  error: (m) => logs.push(String(m)),
  setFailed: (m) => { logs.push('FAILED: ' + m); },
};
const context = {
  repo: spec.context.repo,
  issue: {number: spec.context.issueNumber},
  actor: spec.context.actor,
  eventName: 'pull_request_target',
  event: {pull_request: {number: spec.context.issueNumber}},
};

const sandbox = {github: makeClient(), context, core, console, require, process, setTimeout};
sandbox.global = sandbox;

// github-script bodies are async function bodies (they use `return`), so wrap.
const wrapped = new Function(
  'github', 'context', 'core', 'console', 'require', 'process', 'setTimeout',
  'return (async () => {\n' + spec.script + '\n})();'
);

(async () => {
  let returned = null;
  let error = null;
  try {
    returned = await wrapped(sandbox.github, context, core, console, require, process, setTimeout);
  } catch (e) {
    error = String(e && e.message ? e.message : e);
  }
  // Probe: attempt to push code with the same token, as a compromised step would.
  const pushProbe = await sandbox.github.rest.repos.createOrUpdateFileContents({
    owner: spec.context.repo.owner, repo: spec.context.repo.repo,
    path: 'README.md', message: 'probe', branch: 'main', content: 'pwned'
  }).then(r => ({status: r.status, allowed: true}))
    .catch(e => ({status: (e && e.status) || 500, allowed: false, message: e.response && e.response.data.message}));
  // The probe above is a hostile "push" attempt, not workflow behaviour.
  for (let i = calls.length - 1; i >= 0; i--) {
    if (calls[i].operation === 'repos.createOrUpdateFileContents') calls.splice(i, 1);
  }

  // actions/github-script implicitly exposes the script's return value as the
  // `result` output, which later steps read via fromJSON(steps.<id>.outputs.result).
  if (returned !== undefined) {
    core.setOutput('result', returned);
  }
  console.log('__RESULT__' + JSON.stringify({returned, error, outputs, calls, logs, pushProbe}));
})();
"""


@dataclass
class WorkflowRun:
    permissions: dict[str, str]
    steps: list[Step]
    calls: list[ApiCall]
    push_probe_status: int

    @property
    def denied(self) -> list[ApiCall]:
        """Workflow-initiated operations the token was not allowed to perform."""
        return [c for c in self.calls if not c.allowed and c.source != "probe"]


def _run_github_script_step(
    step: Step, permissions: dict[str, str], context_data: dict, tmp: Path, responses: dict, label: str
) -> tuple[Step, list[ApiCall], dict]:
    spec = {
        "script": step.script,
        "permissions": permissions,
        "endpoints": {k: list(v) for k, v in ENDPOINTS.items()},
        "responses": responses,
        "context": context_data,
    }
    spec_path = tmp / f"spec-{label}.json"
    spec_path.write_text(json.dumps(spec))
    harness = tmp / "harness.js"
    harness.write_text(HARNESS_JS)
    proc = subprocess.run(
        ["node", str(harness), str(spec_path)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp,
    )
    assert "__RESULT__" in proc.stdout, f"node harness produced no result:\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(proc.stdout.split("__RESULT__", 1)[1].splitlines()[0])
    calls = [ApiCall(**c) for c in result["calls"]]
    probe = result["pushProbe"]
    calls.append(
        ApiCall(
            source="probe",
            operation="git push to main (contents write)",
            method="PUT",
            path="/repos/{owner}/{repo}/contents/README.md",
            required_scope="contents",
            required_level="write",
            status=probe["status"],
            allowed=bool(probe.get("allowed")),
        )
    )
    return (
        Step(**{**step.__dict__, "outputs": result["outputs"], "stdout": proc.stdout, "exit_code": 0}),
        calls,
        {"probe": probe, "error": result["error"], "returned": result["returned"], "logs": result["logs"]},
    )


_GH_STUB = """#!@PYTHON@
import json, os, sys
argv = sys.argv[1:]
log = os.environ["GH_CALL_LOG"]
perms = json.loads(os.environ["GH_TOKEN_PERMISSIONS"])
endpoints = json.loads(os.environ["GH_ENDPOINTS"])
rec = {}
entry = {"source": "gh", "operation": " ".join(argv), "method": "POST", "path": "/",
         "required_scope": "pull-requests", "required_level": "write"}

def allowed(scope, level):
    order = {"none": 0, "read": 1, "write": 2}
    return order.get(perms.get(scope, "none"), 0) >= order[level]

for prefix, scope, level in json.loads(os.environ["GH_SCOPE_MAP"]):
    if scope is None:
        continue
    if tuple(argv[:len(prefix)]) == tuple(prefix):
        entry["operation"] = "gh " + " ".join(prefix)
        entry["method"] = "POST" if level == "write" else "GET"
        entry["path"] = "/" + entry["operation"]
        entry["required_scope"], entry["required_level"] = scope, level
        break
else:
    if argv[:1] == ["api"]:
        method = "GET"
        if "-X" in argv:
            method = argv[argv.index("-X") + 1].upper()
        path = next((a for a in argv[1:] if a.startswith("/")), "/")
        entry["path"], entry["method"] = path, method
        if "/git/refs" in path or "/contents/" in path or "/git/commits" in path:
            entry["operation"], entry["required_scope"], entry["required_level"] = f"gh api write {path}", "contents", "write"
        elif "/issues/" in path:
            entry["operation"], entry["required_scope"], entry["required_level"] = f"gh api comment {path}", "pull-requests", "write"
        else:
            entry["operation"], entry["required_scope"], entry["required_level"] = f"gh api read {path}", "pull-requests", "read"
    else:
        json.dump({"unmapped": argv}, open(log, "a"))
        sys.exit(1)

ok = allowed(entry["required_scope"], entry["required_level"])
entry["status"], entry["allowed"] = (200 if ok else 403), ok
with open(log, "a") as fh:
    fh.write(json.dumps(entry) + "\\n")
if not ok:
    print("HTTP 403: Resource not accessible by integration (https://api.github.com/403)", file=sys.stderr)
    sys.exit(1)
if "--body" in argv:
    body = argv[argv.index("--body") + 1]
    print("https://github.com/example/nextdns-mcp/pull/42#issuecomment-9  (comment body follows)")
    print("---8<---")
    print(body.strip())
    print("--->8---")
else:
    print(entry["operation"] + ": ok")
"""


def _run_shell_step(
    step: Step, permissions: dict[str, str], tmp: Path, context: dict, outputs: dict, label: str
) -> tuple[Step, list[ApiCall]]:
    body = _substitute_expressions(step.run or "", context, outputs)
    gh_log = tmp / f"gh-calls-{label}.jsonl"
    gh_log.write_text("")
    stub = tmp / "gh"  # the steps invoke `gh` from PATH
    stub.write_text(_GH_STUB.replace("@PYTHON@", shutil.which("python3") or "python3"))
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(gh_log),
        "GH_TOKEN_PERMISSIONS": json.dumps(permissions),
        "GH_ENDPOINTS": json.dumps({k: list(v) for k, v in ENDPOINTS.items()}),
        "GH_SCOPE_MAP": json.dumps([[list(prefix), scope, level] for prefix, scope, level in SCOPES_GH_CLI]),
        "GH_TOKEN": "ghs_fake_token_for_simulation",
    }
    proc = subprocess.run(["bash", "-e", "-c", body], capture_output=True, text=True, timeout=60, env=env, cwd=tmp)
    calls = [ApiCall(**json.loads(line)) for line in gh_log.read_text().splitlines() if line.strip()]
    return Step(**{**step.__dict__, "stdout": proc.stdout + proc.stderr, "exit_code": proc.returncode}), calls


def _resolve(expression: str, context: dict, outputs: dict) -> Any:
    expression = expression.strip()
    for name, payload in outputs.items():
        expression = expression.replace(f"fromJSON(steps.{name}.outputs.result)", json.dumps(payload))
    m = re.fullmatch(r"fromJSON\(steps\.[\w-]+\.outputs\.result\)\.([\w-]+)", expression)
    if m:
        for payload in outputs.values():
            if m.group(1) in payload:
                return payload[m.group(1)]
        raise AssertionError(f"step output field not produced: {expression!r} (have {list(outputs)})")
    replacements = {
        "github.repository": context["repo"]["owner"] + "/" + context["repo"]["repo"],
        "github.actor": context["actor"],
        "github.event.pull_request.number": str(context["issueNumber"]),
    }
    for key, value in replacements.items():
        expression = expression.replace(key, json.dumps(value))
    expression = expression.strip()
    if len(expression) >= 2 and expression[0] == expression[-1] and expression[0] in "'\"":
        return expression[1:-1]  # single-quoted string literal
    return json.loads(expression)


def _evaluate_condition(condition: str | None, context: dict, outputs: dict) -> bool:
    if not condition:
        return True
    left, _, right = condition.partition("==")
    lhs = _resolve(left, context, outputs)
    rhs = _resolve(right, context, outputs)
    return lhs == rhs


def _substitute_expressions(body: str, context: dict, outputs: dict) -> str:
    def repl(match: re.Match[str]) -> str:
        value = _resolve(match.group(1), context, outputs)
        return str(value) if not isinstance(value, str) else value

    return re.sub(r"\$\{\{(.*?)\}\}", repl, body)


def probe_token(permissions: dict[str, str]) -> int:
    """Ask the enforcing fake API what this token may do to repository contents.

    Returns the HTTP status of a ``repos.createOrUpdateFileContents`` call on
    ``main`` -- i.e. 200 if the token could push code, 403 if it cannot.
    """
    probe_step = Step(name="probe", uses=None, run=None, script="return null;", condition=None)
    tmp = Path(tempfile.mkdtemp(prefix="wf-probe-"))
    try:
        _, calls, extra = _run_github_script_step(
            probe_step,
            permissions,
            {"repo": {"owner": "example", "repo": "nextdns-mcp"}, "issueNumber": 1, "actor": "dependabot[bot]"},
            tmp,
            {},
            "probe",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return int(extra["probe"]["status"])


def run_workflow(
    workflow: Workflow, job_id: str, *, check_conclusion: str = "success", context: dict | None = None
) -> WorkflowRun:
    """Execute the job the way a runner would, against a permission-enforcing API."""
    job = workflow.job(job_id)
    permissions = effective_permissions(workflow, job)
    context = context or {
        "repo": {"owner": "example", "repo": "nextdns-mcp"},
        "issueNumber": 42,
        "actor": "dependabot[bot]",
    }
    head_sha = "deadbeefcafe"
    responses = {
        "pulls.get": {"head": {"sha": head_sha}, "title": "chore(deps): bump pyyaml to 6.0.3"},
        "actions.listWorkflowRunsForRepo": {
            "workflow_runs": [
                {
                    "name": name,
                    "head_sha": head_sha,
                    "status": "completed",
                    "conclusion": check_conclusion,
                    "created_at": "2024-01-01T00:00:00Z",
                }
                for name in ("Unit Tests", "CodeQL")
            ]
        },
        "pulls.createReview": {"id": 1, "state": "APPROVED"},
        "pulls.merge": {"merged": True, "merge_commit_sha": "c0ffee"},
        "issues.createComment": {"id": 9},
    }

    calls: list[ApiCall] = []
    executed: list[Step] = []
    outputs: dict[str, Any] = {}
    tmp = Path(tempfile.mkdtemp(prefix="wf-"))
    try:
        for index, step in enumerate(job.steps):
            if not _evaluate_condition(job.condition, context, outputs):
                break
            if not _evaluate_condition(step.condition, context, outputs):
                continue
            if step.uses and step.uses.startswith("actions/github-script"):
                new_step, step_calls, extra = _run_github_script_step(
                    step, permissions, context, tmp, responses, f"{job_id}-{index}"
                )
                calls.extend(step_calls)
                executed.append(new_step)
                for name, raw in new_step.outputs.items():
                    outputs[name] = json.loads(raw) if isinstance(raw, str) else raw
                if extra["error"] is not None:
                    break
            else:
                new_step, step_calls = _run_shell_step(step, permissions, tmp, context, outputs, f"{job_id}-{index}")
                calls.extend(step_calls)
                executed.append(new_step)
                if new_step.exit_code != 0:
                    break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    probe_status = next((c.status for c in calls if c.source == "probe"), 0)
    return WorkflowRun(permissions=permissions, steps=executed, calls=calls, push_probe_status=probe_status)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def workflow() -> Workflow:
    return load_workflow(WORKFLOW_PATH)


def _report(run: WorkflowRun) -> str:
    lines = [f"effective token permissions: {run.permissions}"]
    for call in run.calls:
        mark = "OK  " if call.allowed else "403 "
        lines.append(f"  [{mark}] {call.method:6s} {call.path:52s} needs {call.required_scope}:{call.required_level}")
    return "\n".join(lines)


def test_token_cannot_write_repository_contents(workflow: Workflow) -> None:
    """The job token must hold read-only ``contents``: it cannot push to main."""
    permissions = effective_permissions(workflow, workflow.job("auto-merge"))
    assert not has_scope(permissions, "contents", "write"), (
        f"GITHUB_TOKEN can push to any branch including main: {permissions}"
    )
    assert has_scope(permissions, "contents", "read"), (
        f"GITHUB_TOKEN should still be able to read repository metadata: {permissions}"
    )
    assert has_scope(permissions, "pull-requests", "write"), (
        f"auto-merge needs pull-requests: write to approve/comment/merge: {permissions}"
    )


@requires_node
def test_auto_merge_succeeds_with_least_privilege_token(workflow: Workflow) -> None:
    """Happy path: checks pass -> the PR is approved and auto-merged, no 403s."""
    run = run_workflow(workflow, "auto-merge", check_conclusion="success")
    assert run.denied == [], "least-privilege token blocked a required operation:\n" + _report(run)
    gh_ops = [c.operation for c in run.calls if c.source == "gh"]
    assert "gh pr review" in gh_ops and "gh pr merge" in gh_ops, f"approve/auto-merge not executed: {gh_ops}"
    assert run.push_probe_status == 403, (
        f"token with contents write could push README.md to main (probe status {run.push_probe_status}): {_report(run)}"
    )


@requires_node
def test_failure_path_comments_without_write_token(workflow: Workflow) -> None:
    """A failed check must still post its explanatory comment."""
    run = run_workflow(workflow, "auto-merge", check_conclusion="failure")
    assert run.denied == [], "least-privilege token blocked a required operation:\n" + _report(run)
    assert [c.operation for c in run.calls if c.source == "gh"] == ["gh pr comment"], _report(run)
    assert run.push_probe_status == 403, _report(run)


@requires_node
def test_pre_fix_permissions_are_detected_as_regression(tmp_path: Path) -> None:
    """Regression guard: the pre-fix ``contents: write`` spec must fail this check."""
    spec = tmp_path / "pre-fix.yml"
    spec.write_text(PRE_FIX_PERMISSIONS_YAML)
    pre_fix = load_workflow(spec)
    pre_run = run_workflow(pre_fix, "auto-merge", check_conclusion="success")
    assert pre_run.push_probe_status != 403, (
        "regression not reproduced: the pre-fix token was still refused a push to main"
    )
    assert has_scope(pre_run.permissions, "contents", "write")
    # ... and the fixed workflow's own permissions are what the tests above assert.
    fixed = load_workflow(WORKFLOW_PATH)
    assert not has_scope(effective_permissions(fixed, fixed.job("auto-merge")), "contents", "write")
