"""What one row says about one call.

SPDX-License-Identifier: MIT

The row's columns and their semantics live here and nowhere else: the tool,
the endpoint it executed, whether the call changed state (the endpoint's HTTP
method, or the tool's own readOnlyHint when it has no endpoint), the bounded
arguments and result, the digest of any submitted code, and the evidence
column. Pure apart from ``time.time()``.
"""

import logging
import time
from collections.abc import Mapping
from typing import Any

from ..constants import is_write_operation
from .evidence import EVIDENCE_MAX_ID_CHARS, _result_evidence
from .projection import ERROR_CHARS, _args_text, _code_sha256, _result_text

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
            evidence = _result_evidence(result, write)
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


def _bounded_profile_id(arguments: Mapping[str, Any]) -> str:
    """An unbounded profile_id must not land in the log whole (#766)."""
    raw = arguments.get("profile_id")
    return "" if raw is None else str(raw)[:EVIDENCE_MAX_ID_CHARS]


def build_row(
    tool: str,
    args: Any,
    ok: bool,
    error: str,
    result: Any,
    endpoints: Mapping[str, tuple[str, str]],
    *,
    annotation_write: bool = False,
) -> dict[str, Any]:
    """The one JSON row describing this call (``endpoints`` is the middleware's map)."""
    arguments = args if isinstance(args, dict) else {}
    endpoint = endpoints.get(tool)
    digest = _code_sha256(arguments)
    # An endpoint's HTTP method is the fact of what changed upstream: `POST
    # /profiles/{profile_id}/rewrites` changed state whatever the tool was named.
    # A tool with no endpoint (the composites, the generative UI) is classified by
    # its own readOnlyHint, which the middleware resolves from the live catalog.
    write = is_write_operation(endpoint[0]) if endpoint else annotation_write
    row = {
        "ts": time.time(),
        "tool": tool,
        "profile_id": _bounded_profile_id(arguments),
        # Bounded before serialization and always valid JSON (see _args_text).
        "args": _args_text(arguments, digest),
        "ok": ok,
        "error": error[:ERROR_CHARS],
        # What the call returned. This is what makes a READ verifiable: the
        # API cannot show that a read happened, but it can show that this
        # read answered with data.
        "result": _result_text(result),
        "write": write,
    }
    if digest:
        row["code_sha256"] = digest
    if digest:
        row["code_sha256"] = digest
    _attach_derived_fields(row, endpoint, result, ok, write)
    return row
