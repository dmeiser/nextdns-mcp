"""Regression tests for the docker-publish release gate (WORK-207).

`.github/workflows/docker-publish.yml` publishes after every successful
`Unit Tests` run on `main`. Before the gate landed, the merged PR's
`no-release` label only suppressed the immutable version tags: `latest`,
`alpine` and the short-SHA tag were enabled unconditionally for every
`workflow_run` event. Run 43801875717 (head 06fe963, PR #516, `no-release`)
logged `Is no-release: true` / `Is release merge: false` and still pushed
`alpine` + `06fe963-alpine`; run 43790690497 (head 3ad5664, PR #522) pushed
`latest` + `3ad5664`. A manual `workflow_dispatch` off a non-`main` ref could
also re-push the immutable version tags from branch-head code.

These tests pin the CI contract the way `test_revert_watchdog.py`,
`test_complexity_gate.py` and `test_coverage_branch_gate.py` pin theirs:

* the `gate` job exists, runs only for `workflow_run`, and exports
  `is_release` as a job output;
* `docker-publish` needs the gate and its `if:` requires
  `needs.gate.outputs.is_release == 'true'` for the `workflow_run` path,
  while `schedule` and `workflow_dispatch` keep bypassing the gate;
* every `latest`/`alpine`/short-SHA tag line carries an `enable=` gated on
  that same `is_release` value, and every version-tag line restricts
  `workflow_dispatch` to `github.ref == 'refs/heads/main'`;
* the gate's github-script, run verbatim under node against the recorded
  payloads of the two `no-release` merges (#516, #522) and one `bump-minor`
  merge (#386), reports `is_release` `false`, `false` and `true`.

Drop the gate again and this file goes red on exactly the pre-fix YAML: the
`gate` job lookups abort and the unconditional short-SHA tag line fails the
enable= assertion.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"

RELEASE_GATE = "needs.gate.outputs.is_release == 'true'"
MAIN_REF = "github.ref == 'refs/heads/main'"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _jobs() -> dict:
    return _workflow()["jobs"]


def _gate_job() -> dict:
    jobs = _jobs()
    assert "gate" in jobs, (
        "docker-publish.yml must keep a gate job that decides is_release for workflow_run "
        "events; without it every merge on main republishes the floating tags (WORK-207)"
    )
    return jobs["gate"]


def _publish_job() -> dict:
    return _jobs()["docker-publish"]


def _gate_script_step() -> dict:
    steps = [s for s in _gate_job().get("steps", []) if "github-script" in str(s.get("uses", ""))]
    assert len(steps) == 1, f"the gate job must contain exactly one github-script step, found {len(steps)}"
    return steps[0]


def _gate_script() -> str:
    script = (_gate_script_step().get("with") or {}).get("script", "")
    assert "listPullRequestsAssociatedWithCommit" in script, (
        "the gate script must look up the merged PR for the head SHA; that label check is the gate"
    )
    return script


def _meta_tags(step_id: str) -> list[str]:
    """Every docker/metadata-action raw tag line of the given meta step."""
    step = next((s for s in _publish_job()["steps"] if s.get("id") == step_id), None)
    assert step is not None, f"the docker-publish job lost its {step_id} metadata step"
    tags = (step.get("with") or {}).get("tags", "")
    return [line.strip() for line in tags.splitlines() if line.strip().startswith("type=raw,")]


def test_gate_job_exists_and_exports_is_release():
    gate = _gate_job()
    outputs = gate.get("outputs") or {}
    assert outputs.get("is_release") == "${{ steps.release.outputs.is_release }}", (
        "the gate job must export is_release from its release step; docker-publish consumes it via needs"
    )
    assert "github.event_name == 'workflow_run'" in str(gate.get("if", "")), (
        "the gate must run only for workflow_run events; schedule and workflow_dispatch bypass it"
    )
    assert "no-release" in _gate_script(), "the gate script must key off the merged PR's no-release label"
    assert RELEASE_GATE in str(_publish_job()["if"]), (
        "the docker-publish job condition must require the gate's is_release output for workflow_run"
    )


def test_publish_job_needs_the_gate():
    needs = _publish_job().get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "gate" in needs, "docker-publish must declare needs: gate or its if: can never see is_release"
    assert "quality" in needs, "schedule/workflow_dispatch runs must still pass the quality job first"


def test_workflow_run_path_requires_the_gate_and_bypasses_stay_open():
    condition = str(_publish_job()["if"])
    for clause in (
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.head_branch == 'main'",
        RELEASE_GATE,
    ):
        assert clause in condition, f"the docker-publish job condition lost the guard {clause!r}"
    bypass = "(github.event_name == 'workflow_dispatch' || github.event_name == 'schedule')"
    assert bypass in condition, (
        "schedule and workflow_dispatch must keep publishing without the gate: nightly rebuilds are how "
        "no-release changes reach :latest"
    )


def test_floating_and_short_sha_tags_are_gated_on_is_release():
    """The exact defect: these tag lines were unconditional for every merge."""
    for step_id, markers in (
        ("meta-slim", ("value=latest", "steps.sha.outputs.short ")),
        ("meta-alpine", ("value=alpine", "steps.sha.outputs.short }}-alpine")),
    ):
        seen = 0
        for line in _meta_tags(step_id):
            if not any(marker in line for marker in markers):
                continue
            seen += 1
            assert RELEASE_GATE in line, (
                f"{step_id} tag line publishes for every workflow_run merge without the gate: {line!r}"
            )
        assert seen == 2, f"{step_id}: expected latest/short-SHA and (alpine) tag lines, matched {seen}"


def test_dispatch_version_tags_require_the_main_ref():
    """A manual rebuild off a feature branch must not overwrite immutable version tags."""
    for step_id in ("meta-slim", "meta-alpine"):
        seen = 0
        for line in _meta_tags(step_id):
            if "steps.version.outputs" not in line:
                continue
            seen += 1
            assert "github.event_name == 'workflow_dispatch'" in line, f"version tag lost dispatch enable: {line!r}"
            assert MAIN_REF in line, (
                f"workflow_dispatch version tags must be restricted to main or a branch rebuild overwrites "
                f"an immutable tag: {line!r}"
            )
        assert seen == 3, f"{step_id}: expected three version-tag lines (full/major/major.minor), matched {seen}"


# Recorded listPullRequestsAssociatedWithCommit payloads (gh api
# repos/dmeiser/nextdns-mcp/commits/<sha>/pulls), the inputs of the two buggy
# runs 43801875717 and 43790690497 and of a good release run 42647653783.
def _recorded_pr(number: int, merged_at: str, merge_sha: str, labels: list[str]) -> dict:
    return {
        "number": number,
        "state": "closed",
        "merged_at": merged_at,
        "merge_commit_sha": merge_sha,
        "labels": [{"name": name} for name in labels],
    }


_SCENARIOS = (
    (
        "no-release-516",
        "06fe963f51b1708582ad081bf009830a53d7d4fe",
        [_recorded_pr(516, "2026-11-16T04:00:58Z", "06fe963f51b1708582ad081bf009830a53d7d4fe", ["no-release"])],
        "false",
    ),
    (
        "no-release-522",
        "3ad56646a3ff30e0a4bf1048bd05f860e10cf5ac",
        [_recorded_pr(522, "2026-11-16T02:08:11Z", "3ad56646a3ff30e0a4bf1048bd05f860e10cf5ac", ["no-release"])],
        "false",
    ),
    (
        "release-386",
        "bbe2e5c2ab80d402a5fa093b8eaf6a789ae2b46a",
        [_recorded_pr(386, "2026-11-09T15:41:26Z", "bbe2e5c2ab80d402a5fa093b8eaf6a789ae2b46a", ["bump-minor"])],
        "true",
    ),
)

# Runs the gate job's github-script verbatim under the same shape the
# actions/github-script runner gives it: top-level await inside an async
# function with context, github, core and console in scope.
_GATE_HARNESS = """\
import fs from "node:fs";

const [scriptPath, fixturePath] = process.argv.slice(2);
const script = fs.readFileSync(scriptPath, "utf8");
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));

const context = {
  payload: { workflow_run: { head_sha: fixture.head_sha } },
  repo: { owner: "dmeiser", repo: "nextdns-mcp" },
};
const requests = [];
const github = {
  rest: {
    repos: {
      listPullRequestsAssociatedWithCommit: async (params) => {
        requests.push(params);
        return { data: fixture.prs };
      },
    },
  },
};

const outputs = {};
const logs = [];
const core = {
  setOutput: (name, value) => {
    outputs[name] = String(value);
  },
};
const runnerConsole = {
  log: (...args) => {
    logs.push(args.join(" "));
  },
};

await new Function("context", "github", "core", "console", "return (async () => {\\n" + script + "\\n})();")(
  context,
  github,
  core,
  runnerConsole,
);

process.stdout.write(JSON.stringify({ outputs, logs, requests }));
"""


def _run_gate_script(tmp_path: Path, head_sha: str, prs: list[dict]) -> dict:
    script_file = tmp_path / "gate-script.js"
    script_file.write_text(_gate_script())
    fixture_file = tmp_path / "fixture.json"
    fixture_file.write_text(json.dumps({"head_sha": head_sha, "prs": prs}))
    harness_file = tmp_path / "gate-harness.mjs"
    harness_file.write_text(_GATE_HARNESS)
    result = subprocess.run(
        ["node", str(harness_file), str(script_file), str(fixture_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"gate script failed under node:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ["scenario", "head_sha", "prs", "expected"],
    _SCENARIOS,
    ids=[scenario for scenario, *_ in _SCENARIOS],
)
def test_gate_script_reports_is_release_for_recorded_merges(
    tmp_path: Path, scenario: str, head_sha: str, prs: list[dict], expected: str
) -> None:
    """The recorded defect inputs must classify exactly as the fixed gate decided."""
    result = _run_gate_script(tmp_path, head_sha, prs)
    assert result["outputs"].get("is_release") == expected, (scenario, result["outputs"], result["logs"])
    assert [r.get("commit_sha") for r in result["requests"]] == [head_sha], (
        f"{scenario}: the gate must look up the merged PR for the workflow_run head SHA"
    )
