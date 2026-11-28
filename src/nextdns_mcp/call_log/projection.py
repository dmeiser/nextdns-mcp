"""How much of a value one row may carry: the bounded projection.

SPDX-License-Identifier: MIT

Bounded *before* serialization, not by truncating a full dump afterwards, so
a 2,000-point series is never serialized whole just to be sliced; over-cap
result text is a ``{"truncated": true, "head": ...}`` envelope, not a slice.
The walk itself is the shared bounder (`json_bounds.bound_json`, which also
bounds the documents `errors` inlines — issue #614); this module owns only the
caps and the omission key a call-log row applies.
"""

import json
from typing import Any

from ..json_bounds import BoundSpec, bound_json

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
PROJECT_MAX_DEPTH = 64

# The caps this module applies to a result: strings are cut, lists keep a few
# leading items and name the dropped ones, dicts keep the leading keys, and
# anything deeper than the depth cap is dropped whole. The walk itself is the
# shared bounder; only these numbers and the marker key are call-log's own.
OMITTED_ITEMS_KEY = "_omitted_items"
_PROJECTION_BOUNDS = BoundSpec(
    max_string_chars=PROJECT_MAX_STRING_CHARS,
    max_items=PROJECT_MAX_ITEMS,
    omitted_key=OMITTED_ITEMS_KEY,
    max_keys=PROJECT_MAX_KEYS,
    max_depth=PROJECT_MAX_DEPTH,
)


def _project_value(value: Any, depth: int = 0) -> Any:
    """A size-bounded projection of one result value (see ``PROJECT_*``)."""
    return bound_json(value, _PROJECTION_BOUNDS, depth)[0]


def project_result(structured: Any) -> Any:
    """The bounded, still-structured form of a result for the log row.

    Keys a caller (or a harness) matches on survive - names, ids, statuses -
    because only list length and string length are trimmed. The projection runs
    before ``json.dumps``, so the serializer never sees the unbounded payload
    and the row is bounded by construction, not by slicing a finished dump.
    """
    return _project_value(structured)


def result_text(result: Any) -> str:
    """Bounded text form of a tool result, for matching read outcomes.

    Always valid JSON, even when bounded: a slice cut mid-document is what a
    verifying reader (the harness json.loads-es this column) would choke on,
    so an over-cap result records a ``{"truncated": true, "head": ...}``
    envelope instead of a fragment.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        # No sort_keys here: the row serializer in ``_append_row`` sorts the
        # whole row once, so sorting this intermediate dump would serialize
        # twice for nothing (same reasoning as the ``args`` column).
        raw = json.dumps(project_result(structured), default=str)
        if len(raw) <= RESULT_CHARS:
            return raw
        # json.dumps of a string can double it (quote/backslash escaping), so
        # the head takes at most half the budget minus the envelope.
        return json.dumps({"truncated": True, "head": raw[: (RESULT_CHARS - 64) // 2]}, sort_keys=True)
    # No structured payload (e.g. a call that raised before answering is recorded
    # with no result at all): record the shape of what came back rather than the bytes.
    blocks = getattr(result, "content", None) or []
    kinds = [getattr(b, "type", type(b).__name__) for b in blocks]
    return json.dumps({"content": kinds}, sort_keys=True)[:RESULT_CHARS]
