"""Standardized structured error payloads for NextDNS MCP tools.

Every tool failure is reported as a plain dict with a stable, machine-readable
``code`` and a human-readable ``error`` message. Callers (including LLMs) can
therefore predict the failure shape regardless of which tool produced it, and can
branch on the typed ``code`` instead of parsing opaque exception text.

SPDX-License-Identifier: MIT
"""

import re
from typing import Any

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


# Typed error codes. These form a stable external contract: consumers branch on
# the code, never on the free-form message. Add new codes here; do not reuse an
# existing code for a different failure class.
class ErrorCode:
    """Typed codes for the standardized error payload contract."""

    INVALID_PROFILE_ID = "invalid_profile_id"
    INVALID_ENTRY_ID = "invalid_entry_id"
    INVALID_RECORD_TYPE = "invalid_record_type"
    MISSING_PROFILE_ID = "missing_profile_id"
    READ_ACCESS_DENIED = "read_access_denied"
    WRITE_ACCESS_DENIED = "write_access_denied"
    ACCESS_DENIED = "access_denied"
    MISSING_REQUIRED_ARGUMENT = "missing_required_argument"
    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED_METRIC = "unsupported_metric"
    HTTP_ERROR = "http_error"
    DOWNLOAD_TOO_LARGE = "download_too_large"
    INTERNAL_ERROR = "internal_error"
    NO_DATA = "no_data"


def error_payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build a standardized error payload with a typed ``code`` and ``error`` message.

    Args:
        code: A stable typed error code (see :class:`ErrorCode`).
        message: A human-readable description of the failure.
        **extra: Optional context fields (e.g. ``status_code``, ``profile_id``).

    Returns:
        A dict of the form ``{"error": message, "code": code, **extra}``.
    """
    payload: dict[str, Any] = {"error": message, "code": code}
    payload.update(extra)
    return payload


def _bound_string(value: str) -> tuple[str, bool]:
    """Cap one free-form string, flagging the cut with ``TRUNCATION_MARKER``."""
    if len(value) > MAX_RESPONSE_BODY_CHARS:
        return value[:MAX_RESPONSE_BODY_CHARS] + TRUNCATION_MARKER, True
    return value, False


def _bound_dict(value: dict[Any, Any]) -> tuple[dict[Any, Any], bool]:
    """Rebuild a dict with every element bounded."""
    bounded: dict[Any, Any] = {}
    truncated = False
    for key, item in value.items():
        bounded[key], cut = _bound_value(item)
        truncated = truncated or cut
    return bounded, truncated


def _bound_list(value: list[Any]) -> tuple[list[Any], bool]:
    """Rebuild a list with every element bounded."""
    bounded_items = [_bound_value(item) for item in value]
    return [item for item, _ in bounded_items], any(cut for _, cut in bounded_items)


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
    if isinstance(value, str):
        return _bound_string(value)
    if isinstance(value, dict):
        return _bound_dict(value)
    if isinstance(value, list):
        return _bound_list(value)
    return value, False


def _bound_structured_fields(payload: dict[str, Any]) -> bool:
    """Bound free-form values in an inlined structured error document.

    Every free-form string is capped at ``MAX_RESPONSE_BODY_CHARS`` (nested in
    dicts and lists too) and cut values are flagged with ``TRUNCATION_MARKER``,
    so a multi-hundred-KB upstream ``details`` field cannot crowd the LLM
    context for a request that already failed (issue #297). The typed string
    fields (``code``, ``error``) are exempt: the issue #148 contract has callers
    branch on them, so their text is surfaced verbatim. A container under one of
    those keys is *not* exempt - a whole document can be hidden there.

    Args:
        payload: The structured error document; bounded in place.

    Returns:
        True when at least one field was truncated.
    """
    truncated = False
    for key, value in payload.items():
        if key in _UNBOUNDED_STRUCTURED_FIELDS and isinstance(value, str):
            continue
        payload[key], cut = _bound_value(value)
        truncated = truncated or cut
    return truncated


def _upstream_error_items(body: Any) -> list[dict[str, Any]]:
    """The ``errors`` documents of an upstream failure body, as dicts.

    The NextDNS API reports failures as ``{"errors": [{"code": "duplicate",
    "detail": ...}]}`` — a *plural* ``errors`` document, distinct from this
    server's own singular ``error`` payload. Anything that is not such a
    document yields no items.
    """
    if not isinstance(body, dict):
        return []
    items = body.get("errors")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def upstream_error_codes(body: Any) -> list[str]:
    """Extract the NextDNS upstream error codes from a decoded response body.

    Callers (tools, and the model reading the tool result) need those upstream
    codes as values they can branch on, instead of matching free-form message
    text.
    """
    return [str(item["code"]) for item in _upstream_error_items(body) if isinstance(item.get("code"), str)]


# httpx's boilerplate on every raise_for_status() message.
_STATUS_DOC_LINK = re.compile(r"\s*For more information check: \S+")

# Upstream codes NextDNS sends with no `detail` text, in the words that say
# what the caller can do about them. Only codes seen from this API are listed;
# an unlisted code is still named, just without a gloss.
_UPSTREAM_CODE_GLOSS = {
    "duplicate": "the entry already exists",
    "notFound": "nothing matches that id",
    "insufficientRole": "the API key cannot write to this profile",
    "authRequired": "no API key was accepted",
    "required": "a required parameter was not sent",
    "type": "a parameter was sent in the wrong type",
    "enum": "a parameter was sent with a value it does not accept",
}


def upstream_error_summary(body: Any) -> str:
    """One clause naming what upstream rejected: details, or the codes alone.

    Some failures carry a `detail` naming the parameter; others carry only a
    code (`{"errors":[{"code":"insufficientRole"}]}`). Both belong in the
    message, because the transport's text names only the URL and the status.
    """
    details = upstream_error_details(body)
    if details:
        return "upstream: " + "; ".join(details[:3])
    codes = upstream_error_codes(body)
    if not codes:
        return ""
    named = [f"{code} ({_UPSTREAM_CODE_GLOSS[code]})" if code in _UPSTREAM_CODE_GLOSS else code for code in codes[:3]]
    return "upstream code: " + ", ".join(named)


def _error_detail_sentence(item: dict[str, Any]) -> str:
    """One upstream error entry's diagnosis sentence, '' when it carries no detail."""
    detail = str(item.get("detail") or "").strip()
    if not detail:
        return ""
    source = item.get("source")
    parameter = source.get("parameter") if isinstance(source, dict) else None
    return f"{detail} (parameter: {parameter})" if parameter else detail


def upstream_error_details(body: Any) -> list[str]:
    """The upstream diagnosis as short sentences, naming the parameter at fault.

    NextDNS says exactly what it rejected — "`partials` must be equal to one of
    the allowed values" with ``source.parameter`` set — but that text only
    reached callers inside ``response_body``, a JSON string, while the ``error``
    message they read first carried the transport's boilerplate.
    """
    details: list[str] = []
    for item in _upstream_error_items(body):
        sentence = _error_detail_sentence(item)
        if sentence:
            details.append(sentence)
    return details


def http_error_payload(message: str, exc: Exception, fallback_code: str = ErrorCode.HTTP_ERROR) -> dict[str, Any]:
    """Build a typed error payload for a failed HTTP request.

    If the failed response body is a structured JSON error, its fields are
    surfaced so they are not lost, with every free-form string (top level or
    nested in dicts and lists) bounded at ``MAX_RESPONSE_BODY_CHARS`` and the
    payload flagged with ``response_body_truncated`` when any value was cut
    (issue #297). Otherwise a generic payload built from ``fallback_code`` is
    returned, always carrying ``status_code`` when the response has one. An
    unparseable body is included only as a bounded prefix, flagged with
    ``response_body_truncated``. (ACL denials do not reach this helper: the
    access-control layer raises the typed ``AccessDeniedError`` instead of
    faking a response.)

    Args:
        message: The human-readable message used when no structured body exists.
        exc: The caught exception; a ``response`` attribute is read when present.
        fallback_code: Typed code used when the body is not a structured error.

    Returns:
        A standardized error payload dict.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) if response is not None else None
    parsed: Any = None

    if response is not None:
        try:
            parsed = response.json()
        except (ValueError, TypeError, RuntimeError):
            # RuntimeError covers httpx.ResponseNotRead: a streaming failure
            # (mid-download 503) reaches here with the body unread, and the
            # payload builder must describe the failure, not raise on it. An
            # unread body parses as no body at all; the bounded text read below
            # answers the same way.
            parsed = None
        structured = _structured_error_payload(parsed, fallback_code, status_code)
        if structured is not None:
            return structured

    payload = error_payload(fallback_code, message, status_code=status_code)
    _attach_error_body(payload, response)
    _add_upstream_diagnosis(payload, parsed)
    # httpx appends a pointer to the MDN page for the status code to every
    # raise_for_status() message. It describes HTTP, not this request, and it
    # eats the room the diagnosis needs - the call log keeps 300 characters of
    # the message, and the link was what survived.
    payload["error"] = _STATUS_DOC_LINK.sub("", str(payload["error"]))
    return payload


def _structured_error_payload(parsed: Any, fallback_code: str, status_code: Any) -> dict[str, Any] | None:
    """The payload when the failed body is already a structured error document.

    Its own fields are kept - they are the diagnosis - with every free-form
    string bounded, so nothing upstream said is lost. None means the body is
    not one, and the generic payload path applies instead.
    """
    if not (isinstance(parsed, dict) and parsed.get("error")):
        return None
    payload = dict(parsed)
    payload.setdefault("code", fallback_code)
    payload.setdefault("status_code", status_code)
    if _bound_structured_fields(payload):
        payload["response_body_truncated"] = True
    return payload


# A UTF-8 codepoint spans at most four bytes; this is the slack kept past the
# character cap so a decode slice ends near a whole codepoint (a trailing
# partial sequence is dropped by the decoder), and the slice is sized so that
# any body longer than it always decodes to more than the cap's worth of
# characters - the truncation flag therefore sees every body it must name.
_UTF8_MAX_CODEPOINT_BYTES = 4
BODY_SLICE_BYTES = MAX_RESPONSE_BODY_CHARS * _UTF8_MAX_CODEPOINT_BYTES + _UTF8_MAX_CODEPOINT_BYTES


def _read_response_body_prefix(response: Any) -> tuple[str | None, bool]:
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
    ``_read_response_body_prefix``.
    """
    text, _ = _read_response_body_prefix(response)
    return text


def _attach_error_body(payload: dict[str, Any], response: Any) -> None:
    """Include the raw body as a bounded prefix, flagged when it had to be cut.

    An unparseable body is the only case where the text stands in for a
    diagnosis, so it is included - but bounded, because the payload goes into
    an LLM context and a 2 MB HTML error page does not (issue #297). The
    ``response_body_truncated`` flag covers both ways content was dropped: a
    character cut at the cap, and a byte slice that stopped short of the body.
    """
    text, more = _read_response_body_prefix(response)
    if not text:
        return
    truncated = more or len(text) > MAX_RESPONSE_BODY_CHARS
    payload["response_body"] = text[:MAX_RESPONSE_BODY_CHARS] + (TRUNCATION_MARKER if truncated else "")
    payload["response_body_truncated"] = truncated


def _add_upstream_diagnosis(payload: dict[str, Any], parsed: Any) -> None:
    """Fold the upstream error document into a generic payload.

    NextDNS reports failures as ``{"errors": [{"code": ...}]}``: the codes go in
    as typed values so callers branch on them instead of parsing the inlined
    body string, and the detail text goes into the message. The transport's
    message names the URL and the status; the upstream document names the
    parameter. A caller acts on the second, so it goes in the message, not only
    in a side field.
    """
    codes = upstream_error_codes(parsed)
    if codes:
        payload["upstream_code"] = codes[0]
        payload["upstream_codes"] = codes
    summary = upstream_error_summary(parsed)
    if summary:
        payload["error"] = f"{payload['error']} | {summary}"


class NextDNSError(RuntimeError):
    """Base exception for NextDNS API errors.

    ``response`` carries the originating :class:`httpx.Response` when there is
    one - including the upstream 2xx that answered with an error document,
    where the status line says success and only the body says otherwise. It is
    a declared constructor argument rather than an attribute attached after
    construction so the error's contract (and a reader of this class) states
    everything a typed failure can carry.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
        response: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.response = response


class NextDNSAuthError(NextDNSError):
    """Raised for 401 and 403 errors."""


class NextDNSRateLimitError(NextDNSError):
    """Raised for 429 errors."""


class NextDNSServerError(NextDNSError):
    """Raised for 5xx errors."""
