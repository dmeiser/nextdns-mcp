"""Per-step feedback and transient-failure classification for the E2E harness.

SPDX-License-Identifier: MIT

The verbose feedback block appended to a failed step's retry prompt
(``_feedback_block``) and the detectors that separate transient
provider/client failures (``_transient_actor_failure``,
``_client_reported_errors``) from real refusals, so only the former are
retried. Split out of ``ai_e2e_harness.py``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ai_e2e_actor import ActorRun, ToolCall
from ai_e2e_checklist import Step, _server_calls_text
from ai_e2e_config import _known_tools, allowed_tools


def _parsed_dicts(lines: Any) -> list[dict[str, Any]]:
    """The JSON-object lines of a raw stream, skipping anything unparseable."""
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _error_events(raw_lines: list[str]) -> list[dict[str, Any]]:
    """The ``{"type": "error"}`` events of a raw event stream."""
    return [ev for ev in _parsed_dicts(raw_lines) if ev.get("type") == "error"]


def _error_message(ev: dict[str, Any]) -> Any:
    error = ev.get("error") or {}
    return error.get("message") if isinstance(error, dict) else str(error)


def _extract_error_events(run: ActorRun) -> list[str]:
    """Pull provider/actor error messages out of the raw event stream."""
    errs = [str(message) for message in map(_error_message, _error_events(run.raw_lines)) if message]
    if run.error:
        errs.append(run.error)
    return errs


def _call_blob(call: ToolCall) -> str:
    """The call's arguments as one searchable string, best effort."""
    try:
        return json.dumps(call.args, default=str).lower()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(call.args).lower()


def _instrument_tokens(path: str) -> list[str]:
    """The strings that mean the call log was touched: the path or its file name."""
    return [token for token in (path.lower(), os.path.basename(path).lower()) if token]


def _actor_read_the_instrument(run: ActorRun, call_log: str | os.PathLike[str]) -> bool:
    # True when the actor opened the harness's own call log. The path is in the
    # MCP config the actor can read, so the subject can see the instrument the
    # verdict is made from. Detection is by path or file name in any call's
    # arguments, whichever tool it used to get there.
    tokens = _instrument_tokens(str(call_log or ""))
    if not tokens:
        return False
    return any(any(token in _call_blob(call) for token in tokens) for call in run.tool_calls)


def _summarize_calls(run: ActorRun) -> list[dict[str, Any]]:
    """Compact record of each tool call the actor made."""
    return [{"tool": c.name, "args": c.args, "ok": c.ok, "result": (c.result_text or "")[:300]} for c in run.tool_calls]


def _transient_actor_failure(run: ActorRun, blob: str) -> tuple[str, str]:
    """Classify a failed actor attempt as ``(kind, reason)``; ``("", "")`` if real.

    Retrying these is fair test scheduling, not help for the model: none of
    these causes depends on the model's capability, and in each of them the
    attempt never put a real call in front of the server, so its verdict
    measured infrastructure rather than behaviour.

    * ``unknown-tool``: opencode registers MCP tools lazily, so an attempt can
      fail before registration completes. Its wording varies, and the older
      detector matched only one of them: "Unknown tool: 'x'" and "No tool named
      \"nextdns_search_tools\" is currently available." both mean the same thing,
      and the second was being scored as a model failure.
    * ``wrong-tool-path``: the attempt called the code-mode name
      (``tools.nextdns.search_tools``) as a direct tool. opencode exposes MCP
      servers through Code Mode by default, and the direct name it registers is
      ``<server>_<tool>``, so that call cannot resolve in any attempt. 25 such
      attempts across two runs, none of them ever reaching the server: retrying
      three times measures the same thing each time, so it gets its own kind and
      a smaller budget rather than the registration label.
    * ``stall``: the actor hit its timeout without producing a single tool call
      (observed: ECONNRESET from the model provider after 150s on a trivial
      prompt, which stalled 13 of 89 steps in one run).
    * ``provider-stream``: the model provider ended the response mid tool call,
      and the client records the call with empty arguments and the reason in
      ``state.error`` (observed: "OpenAI Chat stream ended without
      finish_reason", which read as the model calling ``call_tool({})``).
    """
    # These two are the client's own words about a name it could not resolve.
    # They must be read from what the client reported, never from the model's
    # narration: a model that writes "the tool is not available" without making
    # a call has refused to act, which is a verdict, not a retry.
    client = _client_reported_errors(run)
    matched = _match_client_error(client)
    if matched is not None:
        return matched
    if "timed out" in run.error.lower() and not run.tool_calls:
        return "stall", run.error
    return _match_blob_transient(run, blob)


def _match_client_error(client: str) -> tuple[str, str] | None:
    """The lazy-registration or wrong-path classification of what the client said."""
    for markers, kind, reason in _CLIENT_ERROR_MODES:
        if any(marker in client for marker in markers):
            return kind, reason
    return None


def _match_blob_transient(run: ActorRun, blob: str) -> tuple[str, str]:
    """The provider-drop classification visible in the whole attempt's text."""
    if _mode_seen(blob, _PROVIDER_STREAM_MARKERS):
        return "provider-stream", "model provider ended the response mid tool call"
    if not run.tool_calls and _mode_seen(blob, _PROVIDER_OUTAGE_MARKERS):
        return "provider-unavailable", "model provider is not serving this model"
    return "", ""


def _mode_seen(blob: str, markers: tuple[str, ...]) -> bool:
    return any(marker in blob for marker in markers)


def _client_reported_errors(run: ActorRun) -> str:
    """Lower-cased text the *client* produced, as opposed to the model's narration.

    Tool results count even when the call is flagged ok: opencode reports a
    resolution failure as a completed call whose output is the error text
    (observed: "Error calling tool 'call_tool': Unknown tool: 'x'" recorded with
    ok=True). What is excluded is the model's own final text and the harness's
    verdict strings — a model that writes "the tool is not available" without
    making a call has refused to act, which is a verdict, not a retry.
    """
    parts = [run.error or "", run.stderr or ""]
    parts += [call.result_text or "" for call in run.tool_calls]
    parts += _extract_error_events(run)
    return " ".join(parts).lower()


# Signatures of opencode's lazy MCP tool registration, not of a model decision.
_LAZY_REGISTRATION_MARKERS = ("unknown tool", "no tool named", "is currently available")

# The provider refusing to serve the model at all. Observed verbatim:
# "Function id '9fa6fd04-...' version 'null': Specified function in account
# 'X1UCRn4VumVsuKcJJB603mP5Yx09bRnJ67Idchb50ys' is not found" - a bare
# `opencode run` with the same model reproduces it, so no MCP code is involved.
_PROVIDER_OUTAGE_MARKERS = (
    "specified function in account",
    "provider_model_doesnt_exist",
    "the model you selected does not exist",
)

# Three steps in a row of the same outage is the signal; the run is measuring
# nothing and would otherwise spend its whole budget doing it.
PROVIDER_OUTAGE_STEPS = 3


# The code-mode name used as a direct tool name. opencode registers a direct MCP
# tool as `<server>_<tool>`, so `tools.<server>.<tool>` is not a name it has.
_CODE_MODE_TOOL_PATH_MARKERS = ('no tool named "tools.', "no tool named 'tools.")

# What the client says about a name it could not resolve. Ordered: the first
# matching mode wins.
_CLIENT_ERROR_MODES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (_CODE_MODE_TOOL_PATH_MARKERS, "wrong-tool-path", "actor called the code-mode tool name directly"),
    (_LAZY_REGISTRATION_MARKERS, "unknown-tool", "MCP tool not registered yet"),
)


def _provider_outage(run: ActorRun) -> bool:
    # Distinct from a bad answer: the attempt produced no tool call at all, so
    # the step measured the provider rather than the model. The message arrives
    # as an {"type":"error"} event in the actor's event stream, not in stderr.
    if run.tool_calls:
        return False
    blob = " ".join(_extract_error_events(run) + [run.stderr or "", run.final_text or ""]).lower()
    return any(marker in blob for marker in _PROVIDER_OUTAGE_MARKERS)


# Signatures of a provider/transport drop rather than a model decision.
_PROVIDER_STREAM_MARKERS = (
    "stream ended without finish_reason",
    "econnreset",
    "connection reset",
    "socket hang up",
    "network error",
)

# How many attempts each transient kind gets. A stall is pure infrastructure
# and costs the step its entire verdict, so it gets more chances than a lazy
# registration hiccup; all are capped so one flaky step cannot eat the budget.
TRANSIENT_ATTEMPTS = {
    "unknown-tool": 3,
    "stall": 5,
    "provider-stream": 3,
    "wrong-tool-path": 2,
    "provider-unavailable": 2,
}


# The ACTOR lines are a fixed chain: what was called, what was not, and why.
_ACTOR_SAMPLE = 4
_DISCOVERY_TOOLS = ("search_tools", "call_tool")
_STATUS_TAG = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}


def _mcp_calls(run: ActorRun) -> list[ToolCall]:
    return [call for call in run.tool_calls if call.name in allowed_tools()]


def _discovery_calls(run: ActorRun) -> list[ToolCall]:
    return [call for call in run.tool_calls if call.name in _DISCOVERY_TOOLS]


def _visible_calls(run: ActorRun) -> list[ToolCall]:
    """The calls the ACTOR lines show: real operations first, discovery second."""
    return _mcp_calls(run) or _discovery_calls(run)


def _actor_call_line(call: ToolCall) -> str:
    return f"  ACTOR   : called {call.name}({json.dumps(call.args)[:120]})" + (
        "" if call.ok else f"  -> ERROR: {(call.result_text or '')[:200]}"
    )


def _no_call_lines(run: ActorRun) -> list[str]:
    errs = _extract_error_events(run)
    if errs:
        return [f"  ACTOR   : no tool call — {errs[0][:250]}"]
    if run.exit_code not in (0, None):
        return [
            f"  ACTOR   : no tool call — harness exited code {run.exit_code}"
            + (f", stderr: {run.stderr[:200]}" if run.stderr else "")
        ]
    return [f"  ACTOR   : no tool call — replied: {(run.final_text or '')[:200]!r}"]


def _invisible_call_lines(run: ActorRun) -> list[str]:
    if not run.tool_calls:
        return _no_call_lines(run)
    return [f"  ACTOR   : no MCP operation observed ({len(run.tool_calls)} built-in/discovery call(s))"]


def _actor_lines(run: ActorRun) -> tuple[list[str], set[int]]:
    """The ACTOR lines and the ids of the calls they already display."""
    visible = _visible_calls(run)
    shown = {id(call) for call in visible[:_ACTOR_SAMPLE]}
    if not visible:
        return _invisible_call_lines(run), shown
    lines = [_actor_call_line(call) for call in visible[:_ACTOR_SAMPLE]]
    extra = len(_mcp_calls(run)) - _ACTOR_SAMPLE
    if extra > 0:
        lines.append(f"  ACTOR   : ... {extra} more MCP call(s)")
    return lines, shown


def _is_hidden_attempt(call: ToolCall, shown: set[int]) -> bool:
    return id(call) not in shown and (call.is_error or call.name not in _known_tools())


def _attempt_line(call: ToolCall) -> str:
    return f"  ATTEMPT : {call.name}({json.dumps(call.args)[:100]})" + (
        f"  -> ERROR: {(call.result_text or '')[:150]}" if not call.ok else "  -> not an advertised tool"
    )


def _attempt_lines(run: ActorRun, shown: set[int]) -> list[str]:
    # Failed calls and attempts at tools outside the advertised catalog
    # (hallucinated names, first-class MCP calls outside execute, wrong
    # session ids): invisible to the verdict, but they explain retries.
    attempts = [call for call in run.tool_calls if _is_hidden_attempt(call, shown)]
    lines = [_attempt_line(call) for call in attempts[:4]]
    if len(attempts) > 4:
        lines.append(f"  ATTEMPT : ... {len(attempts) - 4} more attempt(s)")
    return lines


def _verify_detail(rest_v: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rest_v.items() if k != "endpoint" and v not in (None, "", {}, [])}


def _verify_line(rest_v: dict[str, Any]) -> str:
    endpoint = rest_v.get("endpoint", "")
    detail = _verify_detail(rest_v)
    return f"  VERIFY  : {endpoint or '(no REST endpoint)'}" + (f"  {json.dumps(detail)[:200]}" if detail else "")


def _result_line(status: str, error: str, reason: str) -> str:
    why = error or reason
    return f"  RESULT  : {_STATUS_TAG[status]}" + (f"  — {why}" if why else "")


def _feedback_block(
    step: Step,
    run: ActorRun,
    status: str,
    error: str,
    reason: str,
    rest_v: dict[str, Any],
    server_calls: list[dict[str, Any]] | None = None,
    verbose: bool = False,
) -> list[str]:
    """Render the command -> actor -> verify -> result chain for one step."""
    out = [f"  COMMAND : {step.description}"]
    lines, shown = _actor_lines(run)
    out.extend(lines)
    if verbose:
        out.extend(_attempt_lines(run, shown))
    if server_calls is not None:
        # The server's own account of the attempt — the only evidence a read
        # step leaves anywhere.
        out.append(f"  SERVER  : {_server_calls_text(server_calls)}")
    out.append(_verify_line(rest_v))
    out.append(_result_line(status, error, reason))
    return out
