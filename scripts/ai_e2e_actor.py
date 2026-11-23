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


def _tool_event_name(part: dict[str, Any]) -> str:
    """Best-effort tool name from a harness tool event.

    Harnesses differ: opencode exposes each MCP tool as ``nextdns_<tool>`` and
    may additionally route calls through a generic ``execute`` tool, whose
    ``input`` code references the underlying ``nextdns_<tool>``. We normalise
    both so coverage (an *observational*, non-verdicting signal) is useful.
    """
    tool = str(part.get("tool") or "")
    name = tool
    if tool in ("execute", "bash", "run", ""):
        code = (
            str((part.get("state") or {}).get("input", {}).get("code", ""))
            if isinstance(part.get("state"), dict)
            else ""
        )
        # opencode invokes MCP tools as tools.nextdns["<tool>"](...), while
        # other harnesses use a nextdns_<tool> prefix or a generic
        # tools.mcp.call_tool({name: "<tool>", ...}) wrapper. Try each form.
        # The wrapper is tried FIRST so a progressive-disclosure call
        # (tools.nextdns.call_tool({name: "addToDenylist", ...})) resolves to the
        # proxied tool rather than to the synthetic "call_tool" itself.
        m = re.search(r'call_tool\(\s*\{[^}]*?name["\']?\s*:\s*["\']([A-Za-z0-9_]+)', code)
        if not m:
            m = re.search(r'nextdns["\']?\s*\[\s*["\']([A-Za-z0-9_]+)', code)
        if not m:
            m = re.search(r"nextdns[._]([A-Za-z_][A-Za-z0-9_]*)", code)
        if not m:
            m = re.search(r'["\']nextdns_([A-Za-z][A-Za-z0-9_]*)["\']', code)
        if m:
            name = m.group(1)
        else:
            name = tool
    return name.removeprefix("nextdns_").removeprefix("tools.nextdns.")


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

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        if etype == "tool_use":
            part = ev.get("part") or {}
            state = part.get("state") or {}
            inp = state.get("input") or {}
            output = _flatten_text(state.get("output"))
            if not output and state.get("error"):
                # opencode reports a failed call's reason in state.error. Without
                # it, a provider-side drop ("OpenAI Chat stream ended without
                # finish_reason") is invisible to the verdict, and the step gets
                # judged as though the model chose to emit an empty call.
                output = _flatten_text(state["error"])
            calls.append(
                ToolCall(
                    name=_tool_event_name(part),
                    args=inp if isinstance(inp, dict) else {},
                    is_error=str(state.get("status", "completed"))
                    not in (
                        "completed",
                        "success",
                        "succeeded",
                    ),
                    result_text=output,
                )
            )

        elif etype in ("message", "assistant"):
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        inp = block.get("input")
                        calls.append(
                            ToolCall(
                                name=str(block.get("name") or ""),
                                args=inp if isinstance(inp, dict) else {},
                                is_error=False,
                            )
                        )
                    elif btype == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                        final_text = block["text"]

        elif etype == "result":
            text = ev.get("result")
            if isinstance(text, str) and text.strip():
                final_text = text

        elif etype == "text":
            part = ev.get("part") or {}
            if isinstance(part.get("text"), str) and part["text"].strip():
                final_text = part["text"]

    return calls, final_text


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


async def run_actor(config: HarnessConfig, prompt: str, workdir: Path, retries: int = 1) -> ActorRun:
    """Launch the actor harness in ``workdir`` and capture its event stream.

    Retries on transient provider/transport failures (e.g. ``ECONNRESET`` from a
    flaky model endpoint) up to ``retries`` times, so a single dropped
    connection does not doom the whole run.
    """
    values = {
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
    argv = config.render_argv(values)
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    # asyncio's cwd= sets the child's working directory but NOT the $PWD env
    # var; opencode resolves its project config from $PWD. Without this it
    # reads the harness's directory, finds no opencode.json, and never mounts
    # the MCP server. OPENCODE_CONFIG pins the generated config explicitly.
    env["PWD"] = str(workdir)
    env["OPENCODE_CONFIG"] = str(Path(workdir) / "opencode.json")

    def _is_transient(err: str) -> bool:
        low = (err or "").lower()
        return any(
            tok in low for tok in ("econnreset", "transport:", "timed out", "connection reset", "socket hang up")
        )

    last_run: ActorRun | None = None
    for attempt in range(retries + 1):
        run = ActorRun(command=argv)
        if not argv or shutil.which(argv[0]) is None:
            run.error = f"actor harness command not found on PATH: {argv[0] if argv else '(empty)'}"
            return run

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            run.error = f"failed to spawn actor harness: {type(e).__name__}: {e}"
            run.duration_ms = int((time.monotonic() - started) * 1000)
            last_run = run
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            return run

        # Read the event stream as it arrives rather than buffering to the end.
        # A timed-out attempt used to discard every event the actor had already
        # produced, so the one question that matters most on a timeout - where
        # did the model stop? - was the one the report could not answer.
        out_lines, err_chunks, timed_out = await _collect_streams(proc, config.timeout_s)
        # Nothing the actor spawned outlives the attempt - on EVERY exit shape,
        # not only abnormal ones. Reproduced live: an actor that exits 0 can
        # leave the MCP server it spawned running, and the nonzero-exit-only
        # cleanup below used to let that whole process group leak; a
        # cancellation arriving mid-drain was swallowed by _suppress the same
        # way. killpg on an already-gone group is a no-op (ProcessLookupError),
        # so unconditional teardown costs nothing on the clean path.
        _kill_process_group(proc)

        err_text = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
        run.stderr = err_text
        raw = [line.decode("utf-8", errors="replace").rstrip("\r\n") for line in out_lines]
        lines = raw
        run.raw_lines = raw
        calls, final_text = parse_actor_events(lines)
        run.tool_calls = calls
        run.final_text = final_text
        run.duration_ms = int((time.monotonic() - started) * 1000)
        last_run = run

        if timed_out:
            run.error = f"actor harness timed out after {config.timeout_s:.0f}s"
            if attempt < retries and _is_transient(run.error):
                await asyncio.sleep(2)
                continue
            return run

        run.exit_code = proc.returncode
        run.ok = proc.returncode == 0
        if not run.ok and not run.error:
            run.error = err_text[-1500:] if err_text else f"actor harness exited with code {proc.returncode}"

        if run.ok:
            return run
        # Retry on transient provider/transport failures.
        if attempt < retries and _is_transient(run.error + " " + run.stderr):
            await asyncio.sleep(2)
            continue
        return run

    return last_run if last_run is not None else ActorRun(command=argv)


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
