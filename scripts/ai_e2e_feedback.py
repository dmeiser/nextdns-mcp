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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.ai_e2e_actor import ActorRun
    from scripts.ai_e2e_checklist import Step, _server_calls_text
    from scripts.ai_e2e_config import _known_tools, allowed_tools
else:
    from ai_e2e_actor import ActorRun
    from ai_e2e_checklist import Step, _server_calls_text
    from ai_e2e_config import _known_tools, allowed_tools


def _extract_error_events(run: ActorRun) -> list[str]:
    """Pull provider/actor error messages out of the raw event stream."""
    errs: list[str] = []
    for line in run.raw_lines:
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and ev.get("type") == "error":
            e = ev.get("error") or {}
            msg = e.get("message") if isinstance(e, dict) else str(e)
            if msg:
                errs.append(str(msg))
    if run.error:
        errs.append(run.error)
    return errs


def _actor_read_the_instrument(run: ActorRun, call_log: str | os.PathLike[str]) -> bool:
    # True when the actor opened the harness's own call log. The path is in the
    # MCP config the actor can read, so the subject can see the instrument the
    # verdict is made from. Detection is by path or file name in any call's
    # arguments, whichever tool it used to get there.
    path = str(call_log or "")
    if not path:
        return False
    sent = path.lower()
    name = os.path.basename(path).lower()
    for call in run.tool_calls:
        blob = json.dumps(call.args, default=str).lower()
        if sent in blob or (name and name in blob):
            return True
    return False


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
    if any(marker in client for marker in _CODE_MODE_TOOL_PATH_MARKERS):
        return "wrong-tool-path", "actor called the code-mode tool name directly"
    if any(marker in client for marker in _LAZY_REGISTRATION_MARKERS):
        return "unknown-tool", "MCP tool not registered yet"
    if "timed out" in run.error.lower() and not run.tool_calls:
        return "stall", run.error
    if any(marker in blob for marker in _PROVIDER_STREAM_MARKERS):
        return "provider-stream", "model provider ended the response mid tool call"
    if not run.tool_calls and any(marker in blob for marker in _PROVIDER_OUTAGE_MARKERS):
        return "provider-unavailable", "model provider is not serving this model"
    return "", ""


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
    mcp_calls = [c for c in run.tool_calls if c.name in allowed_tools()]
    discovery = [c for c in run.tool_calls if c.name in ("search_tools", "call_tool")]
    if mcp_calls:
        for c in mcp_calls[:4]:
            out.append(
                f"  ACTOR   : called {c.name}({json.dumps(c.args)[:120]})"
                + ("" if c.ok else f"  -> ERROR: {(c.result_text or '')[:200]}")
            )
        if len(mcp_calls) > 4:
            out.append(f"  ACTOR   : ... {len(mcp_calls) - 4} more MCP call(s)")
    elif discovery:
        for c in discovery[:4]:
            out.append(
                f"  ACTOR   : called {c.name}({json.dumps(c.args)[:120]})"
                + ("" if c.ok else f"  -> ERROR: {(c.result_text or '')[:200]}")
            )
    elif run.tool_calls:
        out.append(f"  ACTOR   : no MCP operation observed ({len(run.tool_calls)} built-in/discovery call(s))")
    else:
        errs = _extract_error_events(run)
        if errs:
            out.append(f"  ACTOR   : no tool call — {errs[0][:250]}")
        elif run.exit_code not in (0, None):
            out.append(
                f"  ACTOR   : no tool call — harness exited code {run.exit_code}"
                + (f", stderr: {run.stderr[:200]}" if run.stderr else "")
            )
        else:
            out.append(f"  ACTOR   : no tool call — replied: {(run.final_text or '')[:200]!r}")
    if verbose:
        # Failed calls and attempts at tools outside the advertised catalog
        # (hallucinated names, first-class MCP calls outside execute, wrong
        # session ids): invisible to the verdict, but they explain retries.
        shown = {id(c) for c in (mcp_calls or discovery)[:4]}
        attempts = [c for c in run.tool_calls if id(c) not in shown and (c.is_error or c.name not in _known_tools())]
        for c in attempts[:4]:
            out.append(
                f"  ATTEMPT : {c.name}({json.dumps(c.args)[:100]})"
                + (f"  -> ERROR: {(c.result_text or '')[:150]}" if not c.ok else "  -> not an advertised tool")
            )
        if len(attempts) > 4:
            out.append(f"  ATTEMPT : ... {len(attempts) - 4} more attempt(s)")
    if server_calls is not None:
        # The server's own account of the attempt — the only evidence a read
        # step leaves anywhere.
        out.append(f"  SERVER  : {_server_calls_text(server_calls)}")
    endpoint = rest_v.get("endpoint", "")
    detail = {k: v for k, v in rest_v.items() if k not in ("endpoint",) and v not in (None, "", {}, [])}
    out.append(f"  VERIFY  : {endpoint or '(no REST endpoint)'}" + (f"  {json.dumps(detail)[:200]}" if detail else ""))
    tag = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[status]
    why = error or reason
    out.append(f"  RESULT  : {tag}" + (f"  — {why}" if why else ""))
    return out
