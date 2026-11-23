"""What the upstream error document says about a failed request.

NextDNS reports failures as a *plural* ``{"errors": [{"code": ..., "detail":
...}]}`` document, distinct from this server's own singular ``error`` payload,
and returns it behind a 4xx for most endpoints but behind a 2xx for some. This
module reads that document from an already-decoded body: it never touches a
response object and imports nothing else from the package, so the diagnosis and
the response-bounding rules cannot reach each other.

SPDX-License-Identifier: MIT
"""

from typing import Any


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


def _upstream_error_summary(body: Any) -> str:
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


def add_upstream_diagnosis(payload: dict[str, Any], parsed: Any) -> None:
    """Fold the upstream error document into a generic payload.

    NextDNS reports failures as ``{"errors": [{"code": ...}]}``: the codes go in
    as typed values so callers branch on them instead of parsing the inlined
    body string, and the detail text goes into the message. The transport's
    message names the URL and the status; the upstream document names the
    parameter. A caller acts on the second, so it goes in the message, not only
    in a side field.

    Public by name because ``errors.http`` is the composer that calls it.
    """
    codes = upstream_error_codes(parsed)
    if codes:
        payload["upstream_code"] = codes[0]
        payload["upstream_codes"] = codes
    summary = _upstream_error_summary(parsed)
    if summary:
        payload["error"] = f"{payload['error']} | {summary}"
