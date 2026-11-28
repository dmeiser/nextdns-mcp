"""Actor half of the NextDNS MCP E2E harness: driving the model harness.

SPDX-License-Identifier: MIT

The actor is an MCP-native LLM harness (default ``opencode``) that mounts the
in-tree MCP server over stdio and does the tool calling itself. This module
holds the actor result model (``ToolCall`` / ``ActorRun``), the
harness-agnostic event-stream parser (``parse_actor_events``), and the
subprocess driver (``run_actor``). Split out of ``ai_e2e_harness.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_e2e_common import _content_texts
from ai_e2e_config import (
    PLACEHOLDER_CONFIG_FILE,
    PLACEHOLDER_MODEL,
    PLACEHOLDER_PROMPT,
    PLACEHOLDER_SERVER_COMMAND,
    PLACEHOLDER_SERVER_ENV,
    PLACEHOLDER_WORKDIR,
    HarnessConfig,
    _redacted_server_env,
)


@dataclass
class ToolCall:
    """A tool the actor used, as observed in the harness's event stream."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    result_text: str = ""

    @property
    def ok(self) -> bool:
        return not self.is_error


@dataclass
class ActorRun:
    """Outcome of driving the actor harness once."""

    command: list[str] = field(default_factory=list)
    ok: bool = False
    exit_code: int | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    stderr: str = ""
    raw_lines: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def tools_used(self) -> list[str]:
        seen: list[str] = []
        for c in self.tool_calls:
            if c.name not in seen:
                seen.append(c.name)
        return seen


def _flatten_text(res: Any) -> str:
    """Flatten a tool result into readable text (bounded).

    Reading the text field of a block, in either shape it arrives in, is the
    shared reader's answer (``ai_e2e_common``); what is the harness's own is
    how the answer is used: an actor event joins every block that carries text,
    because the failure it reports can be split across them, and anything that
    is not a tool result is rendered as its string form.
    """
    if isinstance(res, dict) and isinstance(res.get("content"), list):
        return "".join(_content_texts(res))[:5000]
    if isinstance(res, str):
        return res[:5000]
    if res is None:
        return ""
    return str(res)[:5000]


# A generic wrapper tool: the real MCP tool name is only inside its ``input.code``.
_GENERIC_WRAPPER_TOOLS = ("execute", "bash", "run", "")

# opencode invokes MCP tools as tools.nextdns["<tool>"](...), while other
# harnesses use a nextdns_<tool> prefix or a generic
# tools.mcp.call_tool({name: "<tool>", ...}) wrapper. The wrapper is tried FIRST
# so a progressive-disclosure call (tools.nextdns.call_tool({name:
# "addToDenylist", ...})) resolves to the proxied tool rather than to the
# synthetic "call_tool" itself.
_WRAPPER_CODE_PATTERNS = (
    re.compile(r'call_tool\(\s*\{[^}]*?name["\']?\s*:\s*["\']([A-Za-z0-9_]+)'),
    re.compile(r'nextdns["\']?\s*\[\s*["\']([A-Za-z0-9_]+)'),
    re.compile(r"nextdns[._]([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r'["\']nextdns_([A-Za-z][A-Za-z0-9_]*)["\']'),
)


def _event_code(part: dict[str, Any]) -> str:
    """The code a wrapper-tool event ran, or "" when the event carries none."""
    state = part.get("state")
    if not isinstance(state, dict):
        return ""
    return str(_or_dict(state.get("input"), {}).get("code", ""))


def _tool_name_in_code(code: str) -> str:
    """The MCP tool a wrapper's code names, by the first spelling that matches."""
    for pattern in _WRAPPER_CODE_PATTERNS:
        match = pattern.search(code)
        if match:
            return match.group(1)
    return ""


def _normalize_tool_name(name: str) -> str:
    return name.removeprefix("nextdns_").removeprefix("tools.nextdns.")


def _or_dict(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    """A value read out of an event, kept only when it is really a mapping."""
    return value if isinstance(value, dict) else default


def _nonempty_str(value: Any) -> str:
    """A string field of an event, or "" when absent, non-string, or blank."""
    return value if isinstance(value, str) and value.strip() else ""


def _tool_event_name(part: dict[str, Any]) -> str:
    """Best-effort tool name from a harness tool event.

    Harnesses differ: opencode exposes each MCP tool as ``nextdns_<tool>`` and
    may additionally route calls through a generic ``execute`` tool, whose
    ``input`` code references the underlying ``nextdns_<tool>``. We normalise
    both so coverage (an *observational*, non-verdicting signal) is useful.
    """
    tool = str(part.get("tool") or "")
    if tool not in _GENERIC_WRAPPER_TOOLS:
        return _normalize_tool_name(tool)
    return _normalize_tool_name(_tool_name_in_code(_event_code(part)) or tool)


# A tool call the client itself reported as finished: opencode spells the
# success status "completed", other harnesses spell it the way they print it.
_OK_STATUSES = ("completed", "success", "succeeded")


def _iter_events(lines: list[str]) -> Any:
    """The JSON-object events of a raw stream, skipping anything unparseable."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict):
            yield ev


def _tool_call(name: str, raw_args: Any, result_text: str, status: str) -> ToolCall:
    return ToolCall(
        name=name,
        args=_or_dict(raw_args, {}),
        is_error=status not in _OK_STATUSES,
        result_text=result_text,
    )


def _tool_use_event(ev: dict[str, Any]) -> tuple[list[ToolCall], str]:
    part = _or_dict(ev.get("part"), {})
    state = _or_dict(part.get("state"), {})
    call = _tool_call(
        _tool_event_name(part),
        state.get("input"),
        _state_output(state),
        str(state.get("status", "completed")),
    )
    return [call], ""


def _state_output(state: dict[str, Any]) -> str:
    # opencode reports a failed call's reason in state.error. Without it, a
    # provider-side drop ("OpenAI Chat stream ended without finish_reason") is
    # invisible to the verdict, and the step gets judged as though the model
    # chose to emit an empty call.
    output = _flatten_text(state.get("output"))
    if output or not state.get("error"):
        return output
    return _flatten_text(state["error"])


def _block_text(block: dict[str, Any]) -> str:
    """The text a content block carries, or "" for any other block kind."""
    if block.get("type") != "text":
        return ""
    return _nonempty_str(block.get("text"))


def _content_blocks(content: Any) -> Any:
    """The mapping content blocks of a message, in order, nothing else."""
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict):
            yield block


def _block_event(block: dict[str, Any]) -> tuple[list[ToolCall], str]:
    if block.get("type") != "tool_use":
        return [], _block_text(block)
    return [_tool_call(str(block.get("name") or ""), block.get("input"), "", "completed")], ""


def _message_event(ev: dict[str, Any]) -> tuple[list[ToolCall], str]:
    # Claude Code ``stream-json``: assistant ``message`` events carry the
    # ``tool_use`` content blocks and the answer text.
    calls: list[ToolCall] = []
    final_text = ""
    for block in _content_blocks(_or_dict(ev.get("message"), {}).get("content")):
        new_calls, text = _block_event(block)
        calls.extend(new_calls)
        final_text = text or final_text
    return calls, final_text


def _result_event(ev: dict[str, Any]) -> tuple[list[ToolCall], str]:
    return [], _nonempty_str(ev.get("result"))


def _part_text_event(ev: dict[str, Any]) -> tuple[list[ToolCall], str]:
    return [], _nonempty_str(_or_dict(ev.get("part"), {}).get("text"))


def _no_event(_ev: dict[str, Any]) -> tuple[list[ToolCall], str]:
    return [], ""


# One handler per event shape; an event type with no handler is ignored, exactly
# as the chain of ``elif``s it replaces did.
_EVENT_HANDLERS: dict[str, Any] = {
    "tool_use": _tool_use_event,
    "message": _message_event,
    "assistant": _message_event,
    "result": _result_event,
    "text": _part_text_event,
}


def parse_actor_events(lines: list[str]) -> tuple[list[ToolCall], str]:
    """Parse a harness ``--format json``-style event stream.

    Recognises the two shapes we support out of the box:

    * opencode: ``tool_use`` events with ``part.tool`` (+ ``part.state``),
      ``text`` events, and an ``error`` event.
    * Claude Code ``stream-json``: assistant ``message`` events with
      ``tool_use`` content blocks, and a final ``result`` event.

    This is the *measuring* side reading what the *actor* did. It is best
    effort: the verdict never depends on it.
    """
    calls: list[ToolCall] = []
    final_text = ""
    for ev in _iter_events(lines):
        new_calls, text = _handler_for(ev.get("type"))(ev)
        calls.extend(new_calls)
        final_text = text or final_text
    return calls, final_text


def _handler_for(event_type: Any) -> Any:
    if not isinstance(event_type, str):
        return _no_event
    return _EVENT_HANDLERS.get(event_type, _no_event)


# =========================================================================== #
# Driving the actor (subprocess)
# =========================================================================== #


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the actor's whole process group, falling back to the direct child.

    The actor is its own session (``start_new_session=True``), so the group is
    the actor plus everything it spawned — the MCP server included. Without
    this, a timed-out or crashed actor left its MCP server alive, and that
    server kept writing state and call records that later steps attributed to
    themselves. Best effort throughout: an already-dead group is success.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    with _suppress():
        proc.kill()


async def _collect_streams(proc: asyncio.subprocess.Process, timeout_s: float) -> tuple[list[bytes], list[bytes], bool]:
    """Drain the actor's stdout and stderr while it runs, up to ``timeout_s``.

    Returns (stdout lines, stderr chunks, timed_out). On a timeout whatever the
    actor had already written is returned with it: the event stream is what
    shows whether the model stalled before its first tool call or in the middle
    Every abnormal exit — timeout, task cancellation (Ctrl-C arrives as
    :class:`asyncio.CancelledError` inside the loop), or a
    :class:`KeyboardInterrupt` — kills the actor's whole process group before
    propagating, so neither the actor nor the MCP server it spawned survives
    teardown to keep writing state or call records.
    """
    out: list[bytes] = []
    err: list[bytes] = []

    async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                return
            sink.append(chunk)

    drains = [
        asyncio.create_task(_drain(proc.stdout, out)),
        asyncio.create_task(_drain(proc.stderr, err)),
    ]
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        _kill_process_group(proc)
    except (asyncio.CancelledError, KeyboardInterrupt):
        _kill_process_group(proc)
        raise
    # The pipes close when the process does, so the drains finish on their own;
    # bounded so a child that leaves a grandchild holding a pipe cannot hang the
    # harness here. Cancellation arriving DURING this drain used to be swallowed
    # by ``_suppress`` and vanish: the run kept going while the actor's process
    # group stayed alive. It now propagates - after the same group teardown as
    # any other abnormal exit.
    try:
        with _suppress():
            await asyncio.wait_for(asyncio.gather(*drains, return_exceptions=True), timeout=10)
    except (asyncio.CancelledError, KeyboardInterrupt):
        _kill_process_group(proc)
        raise
    return out, err, timed_out


def _is_transient(err: str) -> bool:
    """Whether an actor error names a provider/transport drop rather than a verdict."""
    low = (err or "").lower()
    return any(tok in low for tok in _TRANSIENT_TOKENS)


_TRANSIENT_TOKENS = ("econnreset", "transport:", "timed out", "connection reset", "socket hang up")


def _argv_values(config: HarnessConfig, prompt: str, workdir: Path) -> dict[str, str]:
    return {
        PLACEHOLDER_PROMPT: prompt,
        PLACEHOLDER_MODEL: config.model,
        PLACEHOLDER_WORKDIR: str(workdir),
        PLACEHOLDER_CONFIG_FILE: str(workdir / "opencode.json"),
        PLACEHOLDER_SERVER_COMMAND: config.server["command"] + " " + " ".join(config.server["args"]),
        # Redacted, like the config file: a template that places {server_env}
        # into the actor's argv must never render the real key into it (SEC-4) —
        # the reference expands inside the child, from the child's environment.
        PLACEHOLDER_SERVER_ENV: json.dumps(_redacted_server_env(config.server)),
    }


def _child_env(workdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    # asyncio's cwd= sets the child's working directory but NOT the $PWD env
    # var; opencode resolves its project config from $PWD. Without this it
    # reads the harness's directory, finds no opencode.json, and never mounts
    # the MCP server. OPENCODE_CONFIG pins the generated config explicitly.
    env["PWD"] = str(workdir)
    env["OPENCODE_CONFIG"] = str(Path(workdir) / "opencode.json")
    return env


async def run_actor(config: HarnessConfig, prompt: str, workdir: Path, retries: int = 1) -> ActorRun:
    """Launch the actor harness in ``workdir`` and capture its event stream.

    Retries on transient provider/transport failures (e.g. ``ECONNRESET`` from a
    flaky model endpoint) up to ``retries`` times, so a single dropped
    connection does not doom the whole run.
    """
    argv = config.render_argv(_argv_values(config, prompt, workdir))
    env = _child_env(workdir)
    last_run = ActorRun(command=argv)
    for attempt in range(retries + 1):
        run, retryable = await _actor_attempt(argv, env, workdir, config.timeout_s)
        last_run = run
        if not retryable or attempt >= retries:
            return run
        await asyncio.sleep(2)
    return last_run


def _missing_actor_error(argv: list[str]) -> str:
    return f"actor harness command not found on PATH: {argv[0] if argv else '(empty)'}"


async def _actor_attempt(
    argv: list[str], env: dict[str, str], workdir: Path, timeout_s: float
) -> tuple[ActorRun, bool]:
    """One actor attempt: returns ``(run, would_a_retry_help)``."""
    run = ActorRun(command=argv)
    if not argv or shutil.which(argv[0]) is None:
        run.error = _missing_actor_error(argv)
        return run, False

    started = time.monotonic()
    proc = await _spawn_actor(argv, env, workdir, run, started)
    if proc is None:
        # The spawn failed; only the caller knows whether an attempt is left.
        return run, True

    timed_out = await _record_event_stream(run, proc, timeout_s, started)
    if not timed_out:
        _settle_exit(run, proc)
    return run, _retry_after_attempt(run)


async def _spawn_actor(
    argv: list[str], env: dict[str, str], workdir: Path, run: ActorRun, started: float
) -> asyncio.subprocess.Process | None:
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            env=env,
            start_new_session=True,
            # The default 64 KiB StreamReader limit aborts readline() on a
            # single long JSONL event (LimitOverrunError), and the drain
            # gather swallows the dead task: the run then reported zero
            # captured events with an empty error. 16 MiB covers any event
            # the actor can plausibly emit on one line.
            limit=16 * 1024 * 1024,
        )
    except Exception as e:  # noqa: BLE001
        run.error = f"failed to spawn actor harness: {type(e).__name__}: {e}"
        run.duration_ms = int((time.monotonic() - started) * 1000)
        return None


async def _record_event_stream(
    run: ActorRun, proc: asyncio.subprocess.Process, timeout_s: float, started: float
) -> bool:
    # Read the event stream as it arrives rather than buffering to the end.
    # A timed-out attempt used to discard every event the actor had already
    # produced, so the one question that matters most on a timeout - where
    # did the model stop? - was the one the report could not answer.
    out_lines, err_chunks, timed_out = await _collect_streams(proc, timeout_s)
    # Nothing the actor spawned outlives the attempt - on EVERY exit shape,
    # not only abnormal ones. Reproduced live: an actor that exits 0 can
    # leave the MCP server it spawned running, and the nonzero-exit-only
    # cleanup below used to let that whole process group leak; a
    # cancellation arriving mid-drain was swallowed by _suppress the same
    # way. killpg on an already-gone group is a no-op (ProcessLookupError),
    # so unconditional teardown costs nothing on the clean path.
    _kill_process_group(proc)

    run.stderr = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
    run.raw_lines = [line.decode("utf-8", errors="replace").rstrip("\r\n") for line in out_lines]
    run.tool_calls, run.final_text = parse_actor_events(run.raw_lines)
    run.duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        run.error = f"actor harness timed out after {timeout_s:.0f}s"
    return timed_out


def _settle_exit(run: ActorRun, proc: asyncio.subprocess.Process) -> None:
    run.exit_code = proc.returncode
    run.ok = proc.returncode == 0
    if run.ok or run.error:
        return
    run.error = run.stderr[-1500:] if run.stderr else f"actor harness exited with code {proc.returncode}"


def _retry_after_attempt(run: ActorRun) -> bool:
    # Retry on transient provider/transport failures.
    return not run.ok and _is_transient(run.error + " " + run.stderr)


class _suppress:
    """Best-effort cleanup guard: swallow ordinary errors, never cancellation.

    Regular exceptions from a best-effort step are swallowed so cleanup cannot
    mask the original error. ``BaseException`` subclasses propagate:
    ``CancelledError`` arriving during the drain gather used to be swallowed
    here, which cancelled the run's cancellation and left the actor's process
    group alive.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *exc: object) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)
