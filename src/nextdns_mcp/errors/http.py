"""The one composer: turning a failed HTTP request into a typed error payload.

``http_error_payload`` is where the package's three readers meet - the bounded
response body, the upstream error document, and the rate-limit headers - so it
is the only module here that imports them, and the only one that touches a
response. Each reader on its own answers a different question and none of them
needs another; keeping the composition in one place is what stops a body-bounding
helper from being called from a header parser.

SPDX-License-Identifier: MIT
"""

import re
from typing import Any

from .body import (
    MAX_RESPONSE_BODY_CHARS,
    TRUNCATION_MARKER,
    bound_structured_fields,
    read_response_body_prefix,
)
from .codes import ErrorCode, error_payload
from .rate_limit import attach_rate_limit_headers
from .upstream import add_upstream_diagnosis

# httpx's boilerplate on every raise_for_status() message.
_STATUS_DOC_LINK = re.compile(r"\s*For more information check: \S+")


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
            attach_rate_limit_headers(structured, response)
            return structured

    payload = error_payload(fallback_code, message, status_code=status_code)
    _attach_error_body(payload, response)
    add_upstream_diagnosis(payload, parsed)
    attach_rate_limit_headers(payload, response)
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
    if bound_structured_fields(payload):
        payload["response_body_truncated"] = True
    return payload


def _attach_error_body(payload: dict[str, Any], response: Any) -> None:
    """Include the raw body as a bounded prefix, flagged when it had to be cut.

    An unparseable body is the only case where the text stands in for a
    diagnosis, so it is included - but bounded, because the payload goes into
    an LLM context and a 2 MB HTML error page does not (issue #297). The
    ``response_body_truncated`` flag covers both ways content was dropped: a
    character cut at the cap, and a byte slice that stopped short of the body.
    """
    text, more = read_response_body_prefix(response)
    if not text:
        return
    truncated = more or len(text) > MAX_RESPONSE_BODY_CHARS
    payload["response_body"] = text[:MAX_RESPONSE_BODY_CHARS] + (TRUNCATION_MARKER if truncated else "")
    payload["response_body_truncated"] = truncated
