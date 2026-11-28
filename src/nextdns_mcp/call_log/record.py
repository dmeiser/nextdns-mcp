"""What one row says about one call.

SPDX-License-Identifier: MIT

The row's columns and their semantics live here and nowhere else: the tool,
the endpoint it executed, whether the call changed state (from the endpoint's
HTTP method), the bounded arguments and result, and the evidence column.
Pure apart from ``time.time()``.
"""

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from ..constants import is_write_operation
from .evidence import EVIDENCE_MAX_ID_CHARS, result_evidence
from .projection import ARGS_CHARS, ERROR_CHARS, project_result, result_text

logger = logging.getLogger(__name__)


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


def _attach_derived_fields(
    row: dict[str, Any], endpoint: tuple[str, str] | None, result: Any, ok: bool, write: bool
) -> None:
    """The endpoint line and the evidence column, both derived from the call."""
    if endpoint:
        row["endpoint"] = f"{endpoint[0]} {endpoint[1]}"
    evidence = {}
    if ok:
        try:
            evidence = result_evidence(result, write)
        except (
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            RecursionError,
            AttributeError,
        ) as exc:  # never let evidence extraction fail the call
            logger.warning("call log evidence extraction failed: %s", exc)
            evidence = {"degraded": True}
    if evidence:
        row["evidence"] = evidence


def build_row(
    tool: str, args: Any, ok: bool, error: str, result: Any, endpoints: Mapping[str, tuple[str, str]]
) -> dict[str, Any]:
    """The one JSON row describing this call (``endpoints`` is the middleware's map)."""
    arguments = args if isinstance(args, dict) else {}
    endpoint = endpoints.get(tool)
    # Whether the call changed state. A generated tool is bound to an
    # endpoint, so the HTTP method decides it: `POST
    # /profiles/{profile_id}/rewrites` changed state whatever the tool
    # was named. The local composite operations (plotAnalytics, dashboard tools, dohLookup)
    # have no endpoint and answer questions, so they are reads.
    write = is_write_operation(endpoint[0]) if endpoint else False
    # An unbounded profile_id must not land in the log whole (#766); None
    # still answers empty (#833).
    raw_pid = arguments.get("profile_id")
    row = {
        "ts": time.time(),
        "tool": tool,
        "profile_id": ("" if raw_pid is None else str(raw_pid))[:EVIDENCE_MAX_ID_CHARS],
        # Enough of the arguments to match the call the harness asked for
        # (which endpoint, which entry), bounded before serialization by
        # the same projection the result uses. Serialized once, without
        # sort_keys: key order is fixed by the final row serialization in
        # ``_append_row``, so sorting here would serialize twice for
        # nothing (the harness matches substrings, never key order).
        "args": json.dumps(project_result(arguments), default=str)[:ARGS_CHARS],
        "ok": ok,
        "error": error[:ERROR_CHARS],
        # What the call returned. This is what makes a READ verifiable: the
        # API cannot show that a read happened, but it can show that this
        # read answered with data.
        "result": result_text(result),
        "write": write,
    }
    _attach_derived_fields(row, endpoint, result, ok, write)
    return row
