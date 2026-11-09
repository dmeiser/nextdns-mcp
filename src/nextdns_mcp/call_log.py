"""Server-side record of the tool calls this server actually executed.

SPDX-License-Identifier: MIT

Verifying an outcome over REST cannot see a read: after a step that asked for
``getProfile``, the profile still exists whether or not the call ever happened,
so a harness that only reads the API passes a step whose transcript was nothing
but errors. The server is the only component that knows which tools it ran and
whether they succeeded, so it writes that down.

Set ``NEXTDNS_CALL_LOG`` to a JSONL path and every executed tool call appends
one line::

    {"ts": 1761571200.5, "tool": "getProfile", "endpoint": "GET /profiles/{profile_id}",
     "profile_id": "abc123", "ok": true, "error": "", "write": false}

Every tool here is bound to an endpoint, so the record names it: the tool name
says what the caller asked for, the endpoint says what the server sent upstream,
and the HTTP method is what makes a call a write. The composite visual and lookup
operations this server performs itself (`overviewDashboard`, `plotAnalytics`, `dohLookup`, …)
have no endpoint and record as reads.

The search transform's synthetic tools are not recorded: the wrapper re-enters
the middleware chain with the real tool name, so recording ``call_tool`` as well
would double-count the same call.

The file is created ``0600``: the rows carry tool arguments and a bounded
projection of results - profile ids, entry names, query text - which is
sensitive data, and the log exists for one run's verification, not for sharing.
Serialization and disk I/O run on a worker thread so a call never blocks the
event loop on ``open()``, and both the arguments row and the result projection
are bounded *before* serialization, not by truncating a full dump afterwards.
The ``evidence`` column lifts the identity facts a verifier matches on (a
write's created id, a list answer's id/name pairs) out of the full payload
before any bounding, so a lossy result projection cannot lose them.
"""

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .constants import SYNTHETIC_TOOL_NAMES, is_write_operation

logger = logging.getLogger(__name__)

CALL_LOG_ENV = "NEXTDNS_CALL_LOG"

# Bound on the recorded result text. Enough to tell an answered read from an
# empty one (and to match on a few expected substrings) without letting one
# large analytics payload dominate the log.
RESULT_CHARS = 2000

# Bound on the recorded arguments row and the error text. Named because the
# harness matches against them and a bare number in the code says nothing.
ARGS_CHARS = 600
ERROR_CHARS = 300

# Bounds of the result projection, applied *before* serialization: lists keep
# this many leading entries (then an omission marker) and strings are capped,
# so a 2,000-point series or a 10,000-entry allowlist never has to be
# serialized whole just to be sliced to RESULT_CHARS afterwards.
PROJECT_MAX_ITEMS = 5
PROJECT_MAX_KEYS = 50
PROJECT_MAX_STRING_CHARS = 200

# Dedicated bounded evidence (see ``_result_evidence``): how many id/name pairs
# one row may carry, and how long each field may be. An id or name over the cap
# is an anomaly; 50 pairs covers every list this server publishes a budget for.
EVIDENCE_MAX_ITEMS = 50
EVIDENCE_MAX_ID_CHARS = 120

# One server process may serve concurrent calls; keep records whole.
_write_lock = threading.Lock()


def call_log_path() -> str:
    """The configured log path, or "" when call logging is off."""
    return os.environ.get(CALL_LOG_ENV, "").strip()


def _parse_log_line(line: str) -> Any:
    """The JSON object on one log line, or None when the line is blank or malformed."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def read_call_log_rows(path: str, offset: int = 0) -> list[dict[str, Any]]:
    """The dict rows of a call-log file from ``offset`` on, skipping bad lines.

    The shared reader for anything verifying against the log (the server's own
    ``get_call_log`` and any harness reading it back): one place decides what a
    row is - a JSON object on its own line - so a malformed or partial trailing
    line is skipped identically everywhere. ``offset`` supports reading only
    what a run appended after capturing the file's previous end.
    """
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        fh.seek(offset)
        for line in fh:
            row = _parse_log_line(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def get_call_log(path: str | None = None) -> list[dict[str, Any]]:
    """Read back the recorded calls (empty when unset, absent or malformed)."""
    target = call_log_path() if path is None else path
    if not target or not os.path.exists(target):
        return []
    try:
        return read_call_log_rows(target)
    except OSError as exc:
        logger.warning("call log read failed: %s", exc)
        return []


def result_error(res: Any) -> str:
    """Error text of a tool result: raised, or an error payload in the body.

    The server's error contract returns ``{"error": ...}`` bodies rather than
    raising, so ``is_error`` alone is not enough to tell success from failure.
    """
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, dict) and structured.get("error"):
        return str(structured["error"])
    if getattr(res, "is_error", False):
        return "tool reported an error"
    return ""


def _project_list(value: list[Any]) -> list[Any]:
    """Keep the leading items; when items were dropped, say how many.

    The omission marker exists so a reader can tell a small result from a
    truncated one; a list that fits whole is returned untouched.
    """
    kept = [_project_value(item) for item in value[:PROJECT_MAX_ITEMS]]
    if len(value) > PROJECT_MAX_ITEMS:
        kept.append({"_omitted_items": len(value) - PROJECT_MAX_ITEMS})
    return kept


def _project_value(value: Any) -> Any:
    """A size-bounded projection of one result value (see ``PROJECT_*``)."""
    if isinstance(value, str):
        return value[:PROJECT_MAX_STRING_CHARS]
    if isinstance(value, dict):
        return {key: _project_value(item) for key, item in list(value.items())[:PROJECT_MAX_KEYS]}
    if isinstance(value, list):
        return _project_list(value)
    return value


def _project_result(structured: Any) -> Any:
    """The bounded, still-structured form of a result for the log row.

    Keys a caller (or a harness) matches on survive - names, ids, statuses -
    because only list length and string length are trimmed. The projection runs
    before ``json.dumps``, so the serializer never sees the unbounded payload
    and the row is bounded by construction, not by slicing a finished dump.
    """
    return _project_value(structured)


class CallLogMiddleware(Middleware):
    """Append one record per tool call this server executes.

    ``endpoints`` maps a generated tool name to the ``(method, path template)``
    it executes; the server builds it once from the parsed routes. It is
    middleware state, not a module registry, so importing this module never
    depends on which other modules have been loaded or in what order.
    """

    def __init__(
        self,
        path: str | None = None,
        endpoints: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        self._path = call_log_path() if path is None else path
        self._endpoints: dict[str, tuple[str, str]] = {
            name: (method.upper(), template) for name, (method, template) in (endpoints or {}).items()
        }

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = str(getattr(context.message, "name", ""))
        if name in SYNTHETIC_TOOL_NAMES:
            return await call_next(context)
        args = getattr(context.message, "arguments", None) or {}
        try:
            result = await call_next(context)
        except Exception as exc:  # schema validation, unknown tool, transport faults
            await self._record(name, args, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        error = result_error(result)
        await self._record(name, args, ok=not error, error=error, result=result)
        return result

    def _record_row(self, tool: str, args: Any, ok: bool, error: str, result: Any) -> dict[str, Any]:
        """The one JSON row describing this call."""
        arguments = args if isinstance(args, dict) else {}
        endpoint = self._endpoints.get(tool)
        # Whether the call changed state. A generated tool is bound to an
        # endpoint, so the HTTP method decides it: `POST
        # /profiles/{profile_id}/rewrites` changed state whatever the tool
        # was named. The local composite operations (plotAnalytics, dashboard tools, dohLookup)
        # have no endpoint and answer questions, so they are reads.
        write = is_write_operation(endpoint[0]) if endpoint else False
        row = {
            "ts": time.time(),
            "tool": tool,
            "profile_id": str(arguments.get("profile_id", "")),
            # Enough of the arguments to match the call the harness asked for
            # (which endpoint, which entry), bounded before serialization by
            # the same projection the result uses.
            "args": json.dumps(_project_result(arguments), sort_keys=True, default=str)[:ARGS_CHARS],
            "ok": ok,
            "error": error[:ERROR_CHARS],
            # What the call returned. This is what makes a READ verifiable: the
            # API cannot show that a read happened, but it can show that this
            # read answered with data.
            "result": _result_text(result),
            "write": write,
        }
        self._attach_derived_fields(row, endpoint, result, ok, write)
        return row

    def _attach_derived_fields(
        self, row: dict[str, Any], endpoint: tuple[str, str] | None, result: Any, ok: bool, write: bool
    ) -> None:
        """The endpoint line and the evidence column, both derived from the call."""
        if endpoint:
            row["endpoint"] = f"{endpoint[0]} {endpoint[1]}"
        evidence = _result_evidence(result, write) if ok else {}
        if evidence:
            row["evidence"] = evidence

    def _record_row_into(self, path: str, tool: str, args: Any, ok: bool, error: str, result: Any) -> None:
        """Build the row and append it; both off the event loop (``_record`` offloads here)."""
        _append_row(path, self._record_row(tool, args, ok, error, result))

    async def _record(self, tool: str, args: Any, ok: bool, error: str, result: Any = None) -> None:
        """Append one row, off the event loop; a failed write never fails the call.

        The whole row - projection and JSON of the arguments and the result,
        not just the disk write - is built on the worker thread: projecting a
        2,000-point series or a 10,000-entry allowlist is the expensive part,
        and doing it inline made the log slow the call it merely observes.
        """
        if not self._path:
            return
        await asyncio.to_thread(self._record_row_into, self._path, tool, args, ok, error, result)


def _append_row(path: str, row: dict[str, Any]) -> None:
    """Write one row to the log. Runs on a worker thread (``_record`` offloads here)."""
    line = json.dumps(row, sort_keys=True) + "\n"
    try:
        with _write_lock, _open_log_file(path) as fh:
            fh.write(line)
    except OSError as exc:
        logger.warning("call log write failed: %s", exc)


def _open_log_file(path: str) -> Any:
    """Open the log for append, creating it ``0600`` and refusing less.

    The rows carry profile ids, entry names and bounded result content -
    account data - so the file must not be group- or world-readable from the
    moment it exists. ``open(..., "a")`` applies the process umask (often
    ``0022``) at creation; ``os.open`` with an explicit mode does not.

    Fail closed: the tightening ``fchmod`` covers a log that already exists
    (a reused path, or an older run's file made under a permissive umask).
    If it fails - an alien-owned file, a filesystem without chmod - this code
    cannot know who can read the file, so it does not append to it at all;
    ``_append_row``'s except path drops the row and warns, which is the
    correct cost for a log that cannot be made private.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8")


def _identity_entry(node: Any) -> tuple[str, dict[str, str]] | None:
    """(full id, bounded entry) when this object carries an id; None otherwise."""
    if not isinstance(node, dict):
        return None
    identifier = node.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    entry = {"id": identifier[:EVIDENCE_MAX_ID_CHARS]}
    if isinstance(node.get("name"), str):
        entry["name"] = node["name"][:EVIDENCE_MAX_ID_CHARS]
    return identifier, entry


def _walk_children(node: Any, out: list[dict[str, str]], seen: set[str]) -> None:
    """Descend one container level looking for id-carrying objects."""
    if isinstance(node, dict):
        for value in node.values():
            _walk_identities(value, out, seen)
    elif isinstance(node, list):
        for value in node:
            _walk_identities(value, out, seen)


def _walk_identities(node: Any, out: list[dict[str, str]], seen: set[str]) -> None:
    """Collect (id, name) objects from a payload, bounded, without replacement."""
    if len(out) >= EVIDENCE_MAX_ITEMS:
        return
    entry = _identity_entry(node)
    if entry is not None and entry[0] not in seen:
        seen.add(entry[0])
        out.append(entry[1])
    _walk_children(node, out, seen)


def _walk_component_dict(node: dict[str, Any], types: set[str]) -> None:
    """Extract component type from dict node and recurse."""
    component_type = node.get("type")
    if isinstance(component_type, str):
        types.add(component_type)
    for value in node.values():
        _walk_component_types(value, types)


def _walk_component_types(node: Any, types: set[str]) -> None:
    """Collect all component `type` values in a Prefab UI view tree."""
    if isinstance(node, dict):
        _walk_component_dict(node, types)
    elif isinstance(node, list):
        for item in node:
            _walk_component_types(item, types)


def _created_id_of(data: Any) -> str:
    """The id a create answer carries, bounded; empty when it does not."""
    if isinstance(data, dict) and isinstance(data.get("id"), str):
        return data["id"][:EVIDENCE_MAX_ID_CHARS]
    return ""


def _prefab_evidence(structured: dict[str, Any]) -> dict[str, Any]:
    """Component facts lifted out of a Prefab UI document."""
    if "$prefab" not in structured:
        return {}
    types: set[str] = set()
    _walk_component_types(structured.get("view"), types)
    return {"components": sorted(types)} if types else {}


def _result_evidence(result: Any, write: bool) -> dict[str, Any]:
    """Identity facts lifted out of the FULL result, before any bounding.

    The ``result`` column is a deliberately lossy projection - a 10,000-entry
    allowlist keeps five items - and the truncated envelope can cut anywhere;
    a harness that matches created-ids or id/name pairs through that column
    loses facts to where the cut landed, not to the truth of the call. This
    field is the fix: the ids a write created (``created_id``, only for
    state-changing calls, so a GET's data.id is never mislabelled as one) and
    up to ``EVIDENCE_MAX_ITEMS`` (id, name) pairs a list answer carried,
    bounded by construction and therefore never itself truncated.
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        return {}
    evidence = _prefab_evidence(structured)
    data = structured.get("data")
    if write:
        created = _created_id_of(data)
        if created:
            evidence["created_id"] = created
    ids: list[dict[str, str]] = []
    _walk_identities(data, ids, set())
    if ids:
        evidence["ids"] = ids
    return evidence


def _result_text(result: Any) -> str:
    """Bounded text form of a tool result, for matching read outcomes.

    Always valid JSON, even when bounded: a slice cut mid-document is what a
    verifying reader (the harness json.loads-es this column) would choke on,
    so an over-cap result records a ``{"truncated": true, "head": ...}``
    envelope instead of a fragment.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        raw = json.dumps(_project_result(structured), sort_keys=True, default=str)
        if len(raw) <= RESULT_CHARS:
            return raw
        # json.dumps of a string can double it (quote/backslash escaping), so
        # the head takes at most half the budget minus the envelope.
        return json.dumps({"truncated": True, "head": raw[: (RESULT_CHARS - 64) // 2]}, sort_keys=True)
    # No structured payload (e.g. plotAnalytics returns image content blocks):
    # record the shape of what came back rather than the bytes.
    blocks = getattr(result, "content", None) or []
    kinds = [getattr(b, "type", type(b).__name__) for b in blocks]
    return json.dumps({"content": kinds}, sort_keys=True)[:RESULT_CHARS]
