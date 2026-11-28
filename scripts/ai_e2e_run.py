"""The per-run orchestration of the AI E2E harness.

The responsibilities of ``_run_harness_scoped`` (``scripts/ai_e2e_harness.py``)
live here as focused functions: the run state shared between the step loop and
the teardown, the harness-owned profile provisioning, the plot-profile setup,
one attempt of one step, the per-step body that assembles the verdict, the
teardown, and the run flow. The harness module keeps the thin delegator; this
module is the one that walks the steps.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ai_e2e_common as common
import ai_e2e_harness as harness


class _RunState:
    """Mutable state of one harness run, shared between the step loop and the teardown."""

    def __init__(
        self,
        config: Any,
        targets: Any,
        report: Any,
        workdir: Path,
        call_log: Path,
        step_cfg: Any,
        selected: list[tuple[int, Any]],
        total: int,
    ) -> None:
        self.config = config
        self.targets = targets
        self.report = report
        self.workdir = workdir
        self.call_log = call_log
        self.step_cfg = step_cfg
        self.selected = selected
        self.total = total
        self.pid = ""
        self.started = time.monotonic()
        self.budget = config.timeout_s
        self.total_events = 0
        self.tools_used: list[str] = []
        self.last_stderr = ""
        # Only the last 500 lines are ever reported; a whole 89-step raw event
        # stream accumulated in a list for nothing.
        self.all_raw: deque[str] = deque(maxlen=500)
        # Profiles this run PROVED it created (successful createProfile receipts in
        # the server's call log, plus harness provisioning and self-heal). The
        # stray sweep at teardown deletes only these — never anything that merely
        # appeared during the run, which a concurrent user or another run may
        # have created.
        self.created_ids: set[str] = set()
        self.aborted_reason = ""
        self.outage_streak = 0
        self.instrument_read = False


@dataclass
class _Attempt:
    """One attempt's record: the actor run, both halves of its verdict, retry flag."""

    run: Any
    status: str
    error: str
    reason: str
    rest_v: dict[str, Any]
    call_ok: bool
    call_why: str
    server_calls: list[dict[str, Any]] = field(default_factory=list)
    transient: bool = False


async def _teardown_profiles(state: _RunState, m: Any) -> None:
    """Delete the profiles this run proved it created; record the cleanup verdict."""
    targets = state.targets
    report = state.report
    # Runs from a finally, so a mid-run crash or Ctrl-C still deletes the
    # profiles this run holds a create receipt for.
    crashing = sys.exc_info()[0] is not None
    try:
        pid = state.pid
        # Safety net: if the model deleted the profile, verify; if not, the
        # harness owns cleanup. For a targeted range that excludes
        # profiles.delete the teardown is harness-owned from the start, so it
        # goes through the same MCP tool the actor would have used.
        # Deletion requires the receipt: every path that set ``pid`` (harness
        # provisioning via createProfile, self-heal from a create response, a
        # receipt-backed adoption) put the id in created_ids; an id that got
        # there any other way is not this run's to delete, and the run says
        # so in its notes instead of destroying it.
        if pid and not harness._deletable_by_this_run(pid, state.created_ids):
            note = f"test profile {pid} has no createProfile receipt from this run: teardown refuses to delete it"
            report.notes.append(note)
            harness._progress(f"  NOTE    : {note}")
            state.pid = ""
            pid = ""
        if pid:
            gone, _verdict = await harness._profile_gone(m, pid, targets)
            if not gone and harness.needs_harness_teardown(state.selected):
                try:
                    err = await harness._harness_delete_profile(state.config, pid)
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"
                gone, _verdict = await harness._profile_gone(m, pid, targets)
                if gone:
                    harness._progress("targeted teardown: test profile deleted via deleteProfile")
                else:
                    harness._progress(f"targeted teardown incomplete via MCP ({err or 'profile still present'})")
            if not gone:
                harness._progress("safety cleanup: deleting the test profile via REST")
                for attempt in range(4):
                    resp = await m.rest_delete(f"/profiles/{pid}")
                    if isinstance(resp, dict) and resp.get("ok"):
                        break
                    await asyncio.sleep(5 * (attempt + 1))
                gone, _verdict = await harness._profile_gone(m, pid, targets)
            report.cleanup = (
                "clean: profile deleted and verified gone via REST" if gone else "FAILED (profile still present)"
            )
            report.profile_id = pid
        else:
            report.cleanup = "skipped (test profile not found on the server)"

        # Remove any extra profiles THIS run created beyond the test profile,
        # so they do not accumulate in the account between runs. Only ids with
        # a createProfile receipt are candidates; if the account listing fails
        # the sweep stands down and says so in the report.
        sweep_note = await harness._sweep_stray_profiles(m, state.created_ids, {pid} if pid else set())
        if sweep_note:
            harness._progress(f"  NOTE    : {sweep_note}")
            report.notes.append(sweep_note)
    except BaseException as e:
        # A teardown site that explodes must not replace the crash already
        # unwinding: the original exception propagates and the teardown failure
        # is recorded instead.
        if not crashing:
            raise
        harness._progress(f"teardown interrupted while a mid-run crash was unwinding: {e!r}")
        report.notes.append(f"teardown interrupted while a mid-run crash was unwinding: {e!r}")


async def _setup_test_profile(state: _RunState, m: Any) -> None:
    """Harness-owned provisioning for targeted runs that skip profiles.create."""
    # Targeted run: when the range does not include profiles.create the
    # harness provisions the test profile itself, through the MCP tool so
    # the server's ACLs apply. Without it every later step has nothing to
    # act on and the report measures nothing.
    if harness.needs_harness_setup(state.selected):
        pid, note = await harness._harness_provision_profile(state.config, state.targets)
        state.pid = pid
        state.report.setup = note
        state.report.profile_id = pid
        if pid:
            state.created_ids.add(pid)
        harness._progress(f"targeted setup: {note}")


async def _resolve_plot_profile(state: _RunState, m: Any) -> None:
    """Resolve the data-bearing profile the read-only steps run against."""
    # Read-only plot/log steps target NEXTDNS_PLOT_PROFILE; when it is unset
    # they would all read the empty test profile and skip as data-starved,
    # so resolve a data-bearing one up front and say so in the report.
    if not any(step.section in {"plots", "dashboard"} or step.id == "logs.download" for _, step in state.selected):
        return
    if not (plot_note := await harness._ensure_plot_profile(m, state.pid)):
        return
    state.report.notes.append(plot_note)
    harness._progress(f"plot profile: {plot_note}")
    # The fallback selection must reach the child server's protected
    # set too: the explicit NEXTDNS_PLOT_PROFILE is protected when the
    # config is built, but a fallback resolved after that point was
    # left writable — the one data-bearing profile the actor must never
    # mutate. Re-protect and re-write the config before any step launches.
    if chosen := os.environ.get("NEXTDNS_PLOT_PROFILE", "").strip():
        harness._protect_server_profile(state.config, chosen)
        harness.write_mcp_config(state.config, state.workdir)


async def _heal_profile(state: _RunState, m: Any) -> None:
    """Recreate the test profile if the actor deleted it between steps."""
    # The actor may delete the test profile mid-run (wrong-tool mistake);
    # recreate it so the report measures each step, not a 404 cascade.
    # Harness scheduling only — no env change.
    healed = await harness._self_heal_profile(m, state.pid, state.targets)
    if healed == state.pid:
        return
    state.pid = healed
    state.report.profile_id = healed
    if healed:
        state.created_ids.add(healed)
    # The profile vanished between steps. This harness deleted
    # it zero times in the run this happened in, so something
    # else is using the account and every step around it is
    # measuring a 404, not the model.
    state.report.notes.append(
        f"self-heal: test profile was deleted mid-run and recreated as {healed}; "
        "another process is using this NextDNS account"
    )


def _render_step_prompt(state: _RunState, step: Any) -> tuple[str, str]:
    """The step's prompt with the profile id it should act on rendered in."""
    prompt_pid = state.pid
    if (step.section in {"plots", "dashboard"} or step.id == "logs.download") and os.environ.get(
        "NEXTDNS_PLOT_PROFILE"
    ):
        prompt_pid = os.environ["NEXTDNS_PLOT_PROFILE"]
    return step.prompt.replace("{profile_id}", prompt_pid), prompt_pid


async def _attempt_step(
    state: _RunState,
    m: Any,
    step: Any,
    prompt: str,
    prompt_pid: str,
    transients: list[str],
    attempts_by_kind: dict[str, int],
) -> _Attempt:
    """Run one attempt of one step: the actor, then both halves of its verdict."""
    # Where the server's call log stands before this attempt; the
    # lines after it are this attempt's authoritative record.
    log_offset = state.call_log.stat().st_size if state.call_log.exists() else 0
    # One subprocess per attempt: run_actor's own internal retry is
    # off here because it would silently double the window this
    # step's call-log slice covers. Observed: a step whose first
    # attempt timed out at 300s and whose second produced no parsed
    # events reported "actor=0, server=56" - the server records
    # were real, they just came from both attempts. The step loop
    # already retries transients, with the bookkeeping.
    run = await harness.run_actor(state.step_cfg, prompt, state.workdir, retries=0)
    state.total_events += len(run.tool_calls)
    for t in run.tools_used():
        if t not in state.tools_used:
            state.tools_used.append(t)
    if run.stderr:
        state.last_stderr = run.stderr
    state.all_raw.extend(run.raw_lines)
    server_calls = harness._read_new_calls(state.call_log, log_offset)
    # A successful createProfile row is this run's receipt that a
    # profile is its own to clean up (the model sometimes creates
    # extras beyond the test profile).
    state.created_ids |= harness._created_ids_from_records(server_calls)

    status, error, reason, rest_v = await harness._verify_step(m, state.targets, state.pid, step, run)
    # REST says what the state is, not who produced it, and for a
    # read it cannot see the call at all. So the server's own record
    # decides the transcript half of the verdict: the exact expected
    # call succeeded, on the profile the step targets, and nothing
    # wrote over it afterwards.
    call_ok, call_why = harness._expected_call_ok(server_calls, step.expect, state.pid, alt_pid=prompt_pid)
    if status == "passed" and not call_ok:
        status = "failed"
        error = call_why
    kind, why = harness._transient_actor_failure(
        run,
        " ".join(
            [error, reason]
            + harness._extract_error_events(run)
            + [c.result_text or "" for c in run.tool_calls]
            + [str(r.get("error") or "") for r in server_calls]
            + ([run.final_text] if run.final_text else [])
        ).lower(),
    )
    # Attempt loop: infrastructure transients re-run the step (see
    # _transient_actor_failure), each kind with its own cap.
    transient = False
    if status == "failed" and kind and attempts_by_kind[kind] < harness.TRANSIENT_ATTEMPTS[kind]:
        attempts_by_kind[kind] += 1
        transients.append(f"{kind}: {why}")
        harness._progress(f"  RETRY   : transient actor failure ({why}); retrying")
        transient = True
    return _Attempt(run, status, error, reason, rest_v, call_ok, call_why, server_calls, transient)


def _skipped_result(step: Any, reason: str) -> Any:
    """The report row for a step the run never attempted."""
    return harness.CheckResult(step.id, step.section, step.description, "skipped", mcp_tool=step.tool, reason=reason)


def _note_outage(state: _RunState, run: Any) -> None:
    """Count consecutive provider-outage steps and end the run at the cap."""
    report = state.report
    if harness._provider_outage(run):
        state.outage_streak += 1
        if state.outage_streak >= harness.PROVIDER_OUTAGE_STEPS:
            state.aborted_reason = "model provider is not serving this model (run aborted)"
            events = harness._extract_error_events(run)
            report.notes.append(
                f"provider outage after {len(report.results) + 1} step(s): {(events[0] if events else run.error)[:200]}"
            )
            harness._progress(f"  ABORT   : {state.aborted_reason}; every remaining step is skipped")
    else:
        state.outage_streak = 0


def _note_instrument_read(state: _RunState, run: Any) -> None:
    """Say once per run when the actor opened the log the verdict is made from."""
    if state.instrument_read or not harness._actor_read_the_instrument(run, state.call_log):
        return
    state.instrument_read = True
    state.report.notes.append(
        f"actor read the harness call log ({state.call_log}): the record this verdict is made "
        "from was visible to the subject"
    )
    harness._progress("  NOTE    : actor read the harness call log; this run's ground truth was visible")


def _step_result(
    state: _RunState,
    step: Any,
    att: _Attempt,
    prompt: str,
    attempt: int,
    transients: list[str],
    fb: list[str],
) -> Any:
    """The report row for one finished step."""
    run = att.run
    return harness.CheckResult(
        id=step.id,
        section=step.section,
        description=step.description,
        status=att.status,
        mcp_tool=step.tool,
        mcp_request={
            "prompt": prompt[:500],
            "exit_code": run.exit_code,
            "tool_events": len(run.tool_calls),
            "attempts": attempt,
        },
        mcp_response={
            "calls": harness._summarize_calls(run),
            "final_text": run.final_text[:1000],
            "error_events": harness._extract_error_events(run)[:3],
            "feedback": fb,
            # Recorded, not just printed: a report that cannot say how
            # many attempts a step took hides infrastructure noise.
            "transients": transients,
            # The server's own record of the attempt: what it executed,
            # with which operation, and whether it succeeded.
            "server_calls": att.server_calls,
            # How the transcript half of the verdict was decided, so a
            # failure says which condition it failed.
            "expected_call": {"ok": att.call_ok, "why": att.call_why, "expect": step.expect},
            # Set on the step where the actor opened the call log; the
            # run-level note says when, this says which step.
            "instrument_read": state.instrument_read and harness._actor_read_the_instrument(run, state.call_log),
        },
        rest_verification=att.rest_v,
        reason=att.reason,
        error=att.error,
        duration_ms=run.duration_ms,
    )


async def _adopt_profile_id(state: _RunState, m: Any) -> None:
    """Adopt the test profile's id once this run can prove it created it."""
    # Capture the profile id as soon as the profile exists — even when
    # this step's own verdict failed. Otherwise every later prompt
    # renders a literal "{profile_id}" and the report measures a
    # cascade of argument errors instead of the model's behaviour.
    # A name lookup may only supply an id this run holds a create
    # receipt for (_proven_created_id): when the actor created the
    # profile, the server's call log carries the createProfile receipt
    # and its id is a created_ids member; when nobody in this run
    # created it, the lookup is ambiguity - it is not adopted, not
    # written to, and never deleted.
    prof = await m.find_profile(state.targets.profile_name) or await m.find_profile(state.targets.renamed_name)
    pid = harness._proven_created_id(prof, state.created_ids)
    state.pid = pid
    if prof is not None and not pid:
        note = (
            f"a profile answering to {state.targets.profile_name!r} (id {prof.get('id')}) has no "
            "createProfile receipt from this run: not adopted, not written to, not deleted"
        )
        state.report.notes.append(note)
        harness._progress(f"  NOTE    : {note}")
    if pid:
        state.report.profile_id = pid


async def _run_one_step(state: _RunState, m: Any, i: int, step: Any) -> None:
    """The per-step body: skip checks, heal, attempts, verdict assembly."""
    if state.aborted_reason:
        state.report.results.append(_skipped_result(step, state.aborted_reason))
        return
    if time.monotonic() - state.started > state.budget:
        state.report.results.append(_skipped_result(step, f"overall time budget ({state.budget:.0f}s) exhausted"))
        return
    if step.section != "profiles" and not state.pid:
        state.report.results.append(_skipped_result(step, "test profile was not provisioned"))
        return

    if step.section != "profiles" and state.pid:
        await _heal_profile(state, m)

    prompt, prompt_pid = _render_step_prompt(state, step)

    attempts_by_kind = {kind: 0 for kind in harness.TRANSIENT_ATTEMPTS}
    transients: list[str] = []
    attempt = 0
    att: _Attempt
    while True:
        attempt += 1
        harness._progress(f"[{i}/{state.total}] {step.id}" + (f" (retry {attempt - 1})" if attempt > 1 else ""))
        att = await _attempt_step(state, m, step, prompt, prompt_pid, transients, attempts_by_kind)
        if not att.transient:
            break

    _note_outage(state, att.run)
    _note_instrument_read(state, att.run)

    # Verdict comes from the task outcome (REST verification), not from
    # how many tool calls the model needed to get there.
    fb = harness._feedback_block(
        step,
        att.run,
        att.status,
        att.error,
        att.reason,
        att.rest_v,
        server_calls=att.server_calls,
        verbose=state.config.verbose,
    )
    for ln in fb:
        harness._progress(ln)

    state.report.results.append(_step_result(state, step, att, prompt, attempt, transients, fb))

    if not state.pid and step.verify in ("profile_exists", "profile_renamed"):
        await _adopt_profile_id(state, m)


def _select_reported(selection: list[tuple[int, Any]]) -> str:
    """How a targeted selection is named on the progress line."""
    return (
        ", ".join(s.id for _, s in selection)
        if len(selection) <= 12
        else f"{selection[0][1].id} .. {selection[-1][1].id}"
    )


async def run_harness_scoped(api_key: str, config: Any) -> Any:
    """The run flow: checklist selection, scratch setup, step loop, teardown, report."""
    tag = common._random_tag()
    resolved_profile_name = common._resolve_profile_name(config.profile_name or None)
    targets = harness.TaskTargets(profile_name=resolved_profile_name, tag=tag)
    report = harness.Report(started_at=time.time(), api_key_present=True, profile_name=targets.profile_name)
    report.actor_kind = "mcp-native-harness (per-step)"
    report.actor_command = config.command

    steps = harness.build_steps(targets)
    selected = harness.select_steps(steps, config.step_selection(len(steps)))
    total = len(steps)
    if len(selected) < total:
        harness._progress(f"targeted run: {len(selected)} of {total} steps ({_select_reported(selected)})")
    harness._progress(f"profile name: {targets.profile_name}  ({len(selected)} of {total} steps)")
    # Before any temp file is created (the workdir's mkdtemp and every
    # per-step server's tempfile use TMPDIR), move scratch off the quota'd
    # tmpfs. The scratch dir outlives the whole run; the finally below
    # reclaims it - atexit alone left a 500 MB log CSV behind on any path
    # that unwound without process exit (cancellation, Ctrl-C, a mid-run
    # exception).
    had_tmpdir = "TMPDIR" in os.environ
    old_tmpdir = os.environ.get("TMPDIR")
    # Save tempfile's own resolved-dir cache too: restoring it to None on
    # cleanup lost a pre-existing explicit tempdir an earlier caller had set.
    old_tempfile_tempdir = tempfile.tempdir
    scratch = harness._prepare_scratch_dir()
    try:
        workdir = Path(config.workdir) if config.workdir else Path(tempfile.mkdtemp(prefix="nextdns_e2e_"))
        workdir.mkdir(parents=True, exist_ok=True)
        harness.write_mcp_config(config, workdir)
        call_log = workdir / harness.CALL_LOG_NAME

        step_cfg = harness.HarnessConfig()
        step_cfg.command = list(config.command)
        step_cfg.model = config.model
        step_cfg.server = config.server
        step_cfg.timeout_s = config.step_timeout_s

        state = _RunState(config, targets, report, workdir, call_log, step_cfg, selected, total)

        async with harness.MeasureClient(api_key) as m:
            try:
                # No startup deletion of same-name profiles: a name this run did not
                # create has no proven link to it, and deleting it would destroy a
                # concurrent user's or another run's profile. The default profile name
                # is now run-unique (common._resolve_profile_name), so an interrupted
                # earlier run cannot leave a name collision here either; everything
                # this run deletes it first proved it created (created_ids below).
                await _setup_test_profile(state, m)
                await _resolve_plot_profile(state, m)
                for i, step in selected:
                    await _run_one_step(state, m, i, step)
            finally:
                await _teardown_profiles(state, m)

    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        # The scratch dir is gone; a TMPDIR still pointing at it breaks every
        # later mktemp in this process (run-scoped tests drive run_harness
        # in-process). Restore the ambient value - but only if nothing else
        # moved TMPDIR after we set it.
        if os.environ.get("TMPDIR") == str(scratch):
            if had_tmpdir:
                os.environ["TMPDIR"] = old_tmpdir  # type: ignore[index]
            else:
                os.environ.pop("TMPDIR", None)
        # Also restore the resolved-cache tempfile pinned at the scratch: it
        # takes precedence over TMPDIR, so leaving it dangling would keep
        # routing later mktemp calls at the deleted directory. Restore the
        # value that was in force before this run - not unconditionally None,
        # which silently dropped a pre-existing explicit tempdir.
        if tempfile.tempdir == str(scratch):
            tempfile.tempdir = old_tempfile_tempdir

    report.actor_tool_calls = state.total_events
    report.actor_tools_used = state.tools_used
    report.actor_stderr = state.last_stderr[:4000]
    report.actor_raw_lines = list(state.all_raw)
    report.actor_duration_ms = int((time.monotonic() - state.started) * 1000)
    report.finished_at = time.time()
    return report
