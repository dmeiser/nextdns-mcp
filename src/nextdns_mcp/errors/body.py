"""Bounding what a failed upstream response contributes to an error payload.

The tool result is read by an LLM client, so neither a multi-hundred-KB error
page nor one huge field inside a structured error document may be copied
verbatim into the context window for a request that already failed. This module
owns the character cap, the spec that applies it to an upstream document, the
bounded read of a response body, and the cap-and-marker helper the package
publishes for consumers that cut their own strings. It imports no sibling module
in the package - the recursive bounder it delegates to is
:mod:`nextdns_mcp.json_bounds`, the shared bounder the call log uses too, which
neither this package nor the call log owns.

A name that crosses out of this module is public here (``bound_structured_fields``,
``read_response_body_prefix``, ``cap_with_marker``); the ``_``-prefixed helpers are
this module's own, which is what ``tests/unit/test_error_boundaries.py`` enforces.

SPDX-License-Identifier: MIT
"""

from typing import Any

from ..json_bounds import BoundSpec, bound_json, bound_key

# Server-side cap on the number of characters of an unparseable upstream
# response body inlined into an error payload. The tool result is read by an
# LLM client, so a multi-hundred-KB HTML error page must not be copied
# verbatim into the context window for a request that already failed.
MAX_RESPONSE_BODY_CHARS = 2048

# Marker appended to a response body that was cut at the cap.
TRUNCATION_MARKER = "... [truncated]"

# Typed fields callers branch on (issue #148 contract): a *string* value under one
# of these keys is surfaced verbatim when a structured error document is inlined
# into a tool result. A dict or list under such a key is still bounded at every
# depth, so a whole document cannot hide there.
_UNBOUNDED_STRUCTURED_FIELDS = frozenset({"code", "error", "status_code"})

# The caps this module applies to an upstream document: strings are cut at the
# body cap and cut values carry the marker, dict keys are cut by the same cap
# because an upstream document names its own fields and a key is as much a size
# cost as a value (issue #600), and nothing else is trimmed - the structure of
# an upstream error document is the diagnosis, so only its size is not.
# Containers are rebuilt, never edited in place.
_BODY_BOUNDS = BoundSpec(max_string_chars=MAX_RESPONSE_BODY_CHARS, string_marker=TRUNCATION_MARKER, bound_keys=True)

# A UTF-8 codepoint spans at most four bytes; this is the slack kept past the
# character cap so a decode slice ends near a whole codepoint (a trailing
# partial sequence is dropped by the decoder), and the slice is sized so that
# any body longer than it always decodes to more than the cap's worth of
# characters - the truncation flag therefore sees every body it must name.
_UTF8_MAX_CODEPOINT_BYTES = 4
BODY_SLICE_BYTES = MAX_RESPONSE_BODY_CHARS * _UTF8_MAX_CODEPOINT_BYTES + _UTF8_MAX_CODEPOINT_BYTES


def _bound_value(value: Any) -> tuple[Any, bool]:
    """Bound every free-form string in a JSON value, at any depth.

    Strings are capped at ``MAX_RESPONSE_BODY_CHARS`` and cut values carry
    ``TRUNCATION_MARKER``; dicts and lists are rebuilt with their elements
    bounded. Other scalars pass through unchanged.

    Args:
        value: Any JSON-decoded value.

    Returns:
        A ``(bounded_value, truncated)`` pair.
    """
    return bound_json(value, _BODY_BOUNDS)


def bound_structured_fields(payload: dict[str, Any]) -> bool:
    """Bound free-form keys and values in an inlined structured error document.

    Every free-form string is capped at ``MAX_RESPONSE_BODY_CHARS`` (dict keys,
    and strings nested in dicts and lists too) and cut strings are flagged with
    ``TRUNCATION_MARKER``, so a multi-hundred-KB upstream ``details`` field
    cannot crowd the LLM context for a request that already failed (issue
    #297), and neither can a field whose *name* is the blob (issue #600). The
    typed string fields (``code``, ``error``) are exempt: the issue #148
    contract has callers branch on them, so their text is surfaced verbatim.
    A container under one of those keys is *not* exempt - a whole document can
    be hidden there.

    Public by name because ``errors.http`` composes the payload this walk bounds.

    Args:
        payload: The structured error document; replaced with its bounded copy.

    Returns:
        True when at least one key or value was truncated.
    """
    bounded: dict[str, Any] = {}
    cuts: list[bool] = []
    for key, value in payload.items():
        if key in _UNBOUNDED_STRUCTURED_FIELDS and isinstance(value, str):
            bounded[key] = value
            continue
        bounded_key, key_cut = bound_key(bounded, key, _BODY_BOUNDS)
        bounded[bounded_key], value_cut = _bound_value(value)
        cuts.extend((key_cut, value_cut))
    payload.clear()
    payload.update(bounded)
    return any(cuts)


def read_response_body_prefix(response: Any) -> tuple[str | None, bool]:
    """A leading slice of the response body as text, and whether more follows it.

    ``(text, more)``; ``text`` is None when the body cannot be read at all.
    Two failures of the obvious ``response.text`` are handled here:

    - A streaming response whose body was never read raises
      ``httpx.ResponseNotRead`` (a ``RuntimeError``) from ``.text`` and
      ``.json()``. An error payload exists to describe a failed request; it
      must not turn the failure handler into a second crash because the
      failure happened mid-stream (reproduced on a streaming 503). The body is
      then reported as absent.
    - A large unparseable body (a multi-hundred-KB HTML error page) used to be
      decoded whole just so the first 2 KB could be sliced off it. The bytes
      are sliced first, with a multibyte margin, and only the slice is
      decoded; whatever came after it is never paid for.

    Public by name because ``errors.http`` reports the truncation the ``more``
    flag carries; ``response_body_text`` below is the reading that does not
    need it.
    """
    try:
        raw = getattr(response, "content", None)
    except RuntimeError:  # httpx.ResponseNotRead: the stream was never read
        return None, False
    if raw is None:
        # Not an httpx response (a test stub with only .text): treat the text
        # it offers as the whole body.
        try:
            return getattr(response, "text", None), False
        except RuntimeError:
            return None, False
    return raw[:BODY_SLICE_BYTES].decode("utf-8", errors="ignore"), len(raw) > BODY_SLICE_BYTES


def response_body_text(response: Any) -> str | None:
    """The response body as text, or None when it cannot be read.

    Safe on every shape a failure arrives in: a streaming response that was
    never read (``ResponseNotRead`` from ``.text``) and a very large body
    (sliced with a multibyte margin rather than decoded whole). See
    ``read_response_body_prefix``.
    """
    text, _ = read_response_body_prefix(response)
    return text


def cap_with_marker(text: str, limit: int, marker: str) -> tuple[str, bool]:
    """Cut ``text`` past ``limit`` characters, marking the cut and flagging it.

    Shared cap-and-marker implementation: the comparison, prefix slice, marker
    append, and truncation flag live in :func:`json_bounds.bound_json` (via
    :class:`json_bounds.BoundSpec`), so this is a thin adapter keeping the
    ``(text, limit, marker)`` call shape both callers use.

    Public by name because ``nextdns_mcp.utils`` borrows it from the package
    root: a name that crosses a module boundary is that module's API, and
    ``utils.sanitize_label`` must reach the one cap without naming a private.
    """
    return bound_json(text, BoundSpec(max_string_chars=limit, string_marker=marker))
