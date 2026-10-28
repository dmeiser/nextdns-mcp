"""Server-side record of the tool calls this server actually executed.

SPDX-License-Identifier: MIT

Verifying an outcome over REST cannot see a read: after a step that asked for
``manageProfiles(operation="get")`` the profile still exists whether or not the
call ever happened, so a harness that only reads the API passes a step whose
transcript was nothing but errors. The server is the only component that knows
which tools it ran and whether they succeeded, so it writes that down.

Set ``NEXTDNS_CALL_LOG`` to a JSONL path and every executed tool call appends
one line::

    {"ts": 1761571200.5, "tool": "manageProfiles", "operation": "get",
     "profile_id": "abc123", "ok": true, "error": ""}

The search transform's synthetic tools are not recorded: the middleware also
sees the real tool that ``call_tool`` dispatches to, and that is the entry
worth having.
"""

import json
import logging
import os
import threading
import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

CALL_LOG_ENV = "NEXTDNS_CALL_LOG"

# Bound on the recorded result text. Enough to tell an answered read from an
# empty one (and to match on a few expected substrings) without letting one
# large analytics payload dominate the log.
RESULT_CHARS = 2000

# Synthetic entries of the progressive-disclosure transform. The inner call is
# logged under its real name, so logging these would double-count discovery.
_SYNTHETIC_TOOLS = frozenset({"search_tools", "call_tool"})

# One server process may serve concurrent calls; keep records whole.
_write_lock = threading.Lock()


def call_log_path() -> str:
    """The configured log path, or "" when call logging is off."""
    return os.environ.get(CALL_LOG_ENV, "").strip()


def get_call_log(path: str | None = None) -> list[dict[str, Any]]:
    """Read back the recorded calls (empty when unset, absent or malformed)."""
    target = call_log_path() if path is None else path
    if not target or not os.path.exists(target):
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as exc:  # pragma: no cover - unreadable file is not fatal
        logger.warning("call log read failed: %s", exc)
    return rows


def result_error(res: Any) -> str:
    """Error text of a tool result: raised, or an error payload in the body.

    The server's error contract returns ``{"error": ...}`` bodies rather than
    raising, so ``is_error`` alone is not enough to tell success from failure.
    """
    if getattr(res, "is_error", False):
        return "tool reported an error"
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, dict) and structured.get("error"):
        return str(structured["error"])
    return ""


# Operations that change server state. Reads (get/list) and the query tools
# (queryAnalytics, plotAnalytics, dohLookup) are the complement: they leave the
# account as they found it, so a caller may run them after doing the work.
WRITE_OPERATIONS = frozenset({"add", "replace", "update", "remove", "create", "delete", "clear"})


def is_write(operation: str) -> bool:
    """Whether an operation mutates state (empty operation means it does not)."""
    return operation in WRITE_OPERATIONS


class CallLogMiddleware(Middleware):
    """Append one record per tool call this server executes."""

    def __init__(self, path: str | None = None) -> None:
        self._path = call_log_path() if path is None else path

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = str(getattr(context.message, "name", ""))
        if name in _SYNTHETIC_TOOLS:
            return await call_next(context)
        args = getattr(context.message, "arguments", None) or {}
        try:
            result = await call_next(context)
        except Exception as exc:  # schema validation, unknown tool, transport faults
            self._record(name, args, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        error = result_error(result)
        self._record(name, args, ok=not error, error=error, result=result)
        return result

    def _record(self, tool: str, args: Any, ok: bool, error: str, result: Any = None) -> None:
        if not self._path:
            return
        arguments = args if isinstance(args, dict) else {}
        operation = str(arguments.get("operation", ""))
        row = {
            "ts": time.time(),
            "tool": tool,
            "operation": operation,
            "write": is_write(operation),
            "profile_id": str(arguments.get("profile_id", "")),
            # Enough of the arguments to match the call the harness asked for
            # (which list, which metric, which entry), bounded so a large
            # settings payload or a long entry list cannot bloat the log.
            "args": json.dumps(_loggable(arguments), sort_keys=True, default=str)[:600],
            "ok": ok,
            "error": error[:300],
            # What the call returned. This is what makes a READ verifiable: the
            # API cannot show that a read happened, but it can show that this
            # read answered with data.
            "result": _result_text(result),
        }
        try:
            with _write_lock, open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError as exc:  # pragma: no cover - logging must not break a call
            logger.warning("call log write failed: %s", exc)


def _loggable(arguments: dict[str, Any]) -> dict[str, Any]:
    """Argument keys worth matching on, with the operation removed (it has its own field)."""
    return {k: v for k, v in arguments.items() if k != "operation"}


def _result_text(result: Any) -> str:
    """Bounded text form of a tool result, for matching read outcomes."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, sort_keys=True, default=str)[:RESULT_CHARS]
    # No structured payload (e.g. plotAnalytics returns image content blocks):
    # record the shape of what came back rather than the bytes.
    blocks = getattr(result, "content", None) or []
    kinds = [getattr(b, "type", type(b).__name__) for b in blocks]
    return json.dumps({"content": kinds}, sort_keys=True)[:RESULT_CHARS]
