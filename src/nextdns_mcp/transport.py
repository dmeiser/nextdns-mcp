"""The API transport: execute a request through the access-controlled client and return a typed payload.

SPDX-License-Identifier: MIT

Every generated tool and every hand-written one that talks to the NextDNS API
reaches it through this module. It owns exactly one thing: sending a request
through ``client.api_client`` - the access-controlled client that enforces the
profile read/write allowlists, read-only mode and the protected-profile
denylist - and turning every way a request can fail into the standardized
payload shape the server documents (``errors.py``):

- an ``AccessDeniedError`` from the ACL layer becomes the typed denial payload
  (never a network request was made at that point);
- an upstream HTTP failure becomes the typed exception matching its status
  (auth / rate-limit / server), and through ``_api_request_payload`` the
  matching error payload;
- a 2xx that carries an error document instead of a payload is normalized into
  the same typed failure, because returning it as data would make an error
  look like a success;
- a 404 under ``/profiles/<id>`` gets the hint naming both ids it could be
  blaming, because upstream does not say which one is wrong.

``utils`` deliberately does not live here: it validates and normalizes values
and imports no network layer at all. The dependency runs one way - transport
imports client - and the call log, the client and the transport share their
few names in common through ``constants``.

Tests replace these functions by attribute on this module (or on the calling
module that imports them), which is why call sites resolve them as module
globals rather than capturing them at construction time.
"""

import logging
import re
from typing import Any

import httpx

from . import client
from .client import AccessDeniedError, _log_safe_error, _redacted
from .errors import (
    ErrorCode,
    NextDNSAuthError,
    NextDNSError,
    NextDNSRateLimitError,
    NextDNSServerError,
    error_payload,
    http_error_payload,
    response_body_text,
    upstream_error_codes,
)

logger = logging.getLogger(__name__)


def access_denied_payload(code: str, message: str, profile_id: str) -> dict[str, Any]:
    """Build the standardized error payload for an access denial (issue #178).

    The one builder for every denial the server reports, from either door: the
    transport's (``access_denied_payload_for`` wraps the raised
    ``AccessDeniedError``) and the DoH tool's gate, which enforces the same
    read rule without an exception in hand. Carries the typed denial ``code``
    (``read_access_denied`` / ``write_access_denied`` / ``access_denied``), the
    full denial reason in ``error``, the denied ``profile_id``, and a nominal
    ``status_code`` of 403 for caller compatibility.
    """
    payload = error_payload(code, message, status_code=403)
    payload["profile_id"] = profile_id
    return payload


def access_denied_payload_for(exc: AccessDeniedError) -> dict[str, Any]:
    """The standardized denial payload for a raised :class:`AccessDeniedError`."""
    return access_denied_payload(exc.code, exc.message, exc.profile_id)


def _handle_api_error(e: NextDNSError) -> dict[str, Any]:
    """The standardized error payload for a raised :class:`NextDNSError`."""
    cause = getattr(e, "__cause__", None)
    if cause is not None and isinstance(cause, httpx.HTTPError):
        return http_error_payload(str(e), cause)
    if getattr(e, "response", None) is not None:
        # Typed error built directly around a response (e.g. an upstream 2xx
        # that carried an error document); keep its structured body instead of
        # flattening to an internal error.
        return http_error_payload(str(e), e)
    return error_payload(ErrorCode.INTERNAL_ERROR, str(e))


async def _api_request(
    method: str, url: str, params: dict[str, Any] | None = None, json_body: Any = None
) -> dict[str, Any]:
    """Make an HTTP request through the access-controlled client and return JSON.

    Access-control denials raised by the ACL layer are reported as standardized
    error payloads (see ``errors.py``) rather than raised exceptions, so callers
    get a consistent, typed failure shape:
    ``{"error": ..., "code": "read_access_denied" | "write_access_denied" | "access_denied", "status_code": 403, ...}``.

    All other failures are raised as typed exceptions with status_code and response_body attributes, so callers can implement retry-with-backoff for 429 or circuit-breaking for 5xx.
    ``NextDNSError`` is the base exception, with subclasses ``NextDNSAuthError`` (401/403), ``NextDNSRateLimitError`` (429), and ``NextDNSServerError`` (5xx).
    For non-HTTP failures, a ``NextDNSError`` is raised with status_code=None.
    Tools that report failures as payloads should await ``_api_request_payload``,
    which applies this conversion.
    """
    try:
        return await _api_request_json(method, url, params=params, json_body=json_body)
    except AccessDeniedError as e:
        # Raised by the ACL layer before any network request. Kept out of the
        # httpx.HTTPError branch: a real upstream 403 (raise_for_status) must
        # keep its existing http_error path.
        logger.warning(f"Access denied in {method} {_redacted(url)}: {_log_safe_error(e)}")
        return access_denied_payload_for(e)
    except NextDNSError:
        # Already typed (including the 2xx-with-error-document case in
        # _api_request_json); re-raised so the generic branch cannot strip its
        # status/response.
        raise
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in {method} {_redacted(url)}: {_log_safe_error(e)}")
        raise _typed_http_error(f"HTTP error in {method} {url}: {e}", *_http_error_parts(e)) from e
    except Exception as e:
        logger.error(f"Unexpected error in {method} {_redacted(url)}: {_log_safe_error(e)}")
        raise NextDNSError(f"Unexpected error in {method} {url}: {e}", status_code=None) from e


async def _api_request_json(
    method: str, url: str, params: dict[str, Any] | None = None, json_body: Any = None
) -> dict[str, Any]:
    """Send the request and return its JSON document, raising typed errors on failure."""
    response = await client.api_client.request(method, url, params=params, json=json_body)
    response.raise_for_status()
    if _is_empty_success(response):
        return {"success": True}
    data = response.json()
    _reject_error_document(method, url, response, data)
    return data


def _http_error_parts(error: Exception) -> tuple[int | None, Any]:
    """The ``(status_code, response_body)`` carried by an httpx error, if any."""
    return _response_error_parts(getattr(error, "response", None))


def _is_empty_success(response: httpx.Response) -> bool:
    """A 204, or a 2xx with no body, is a successful write with nothing to return."""
    return response.status_code == 204 or not response.content


def _reject_error_document(method: str, url: str, response: httpx.Response, data: Any) -> None:
    """Raise when a 2xx answer is actually an error document; return when it is data.

    Observed: POST /allowlist on an existing entry returns 200
    ``{"errors":[{"code":"duplicate"}]}``, while the same condition on
    /privacy/blocklists returns 400. Returning that body as if it were data
    makes an error look like a success; it is normalized into the same typed
    failure every other upstream error produces. The code is named by
    ``http_error_payload`` from the body, so this message only has to say that
    the status lied.
    """
    if not (upstream_error_codes(data) and "data" not in data):
        return
    raise NextDNSError(
        f"HTTP error in {method} {url}: upstream answered {response.status_code} "
        "with an error document instead of a payload",
        status_code=response.status_code,
        response_body=response.text,
        response=response,
    )


def _response_error_parts(response: Any) -> tuple[Any, Any]:
    """The (status code, body text) an httpx exception carried, or (None, None).

    A transport failure (connection refused, timeout) has no response at all;
    the typed exception still wants the two attributes, as None. The body read
    is the safe one: a failure caught on a *streaming* response has no
    buffered body, and reading ``.text`` there raises - the error path must
    not raise while describing a failure.
    """
    if response is None:
        return None, None
    return getattr(response, "status_code", None), response_body_text(response)


def _typed_http_error(message: str, status_code: int | None, response_body: str | None) -> NextDNSError:
    """Build the NextDNSError subclass matching an upstream HTTP status."""
    if status_code in (401, 403):
        return NextDNSAuthError(message, status_code=status_code, response_body=response_body)
    if status_code == 429:
        return NextDNSRateLimitError(message, status_code=status_code, response_body=response_body)
    if status_code is not None and 500 <= status_code < 600:
        return NextDNSServerError(message, status_code=status_code, response_body=response_body)
    return NextDNSError(message, status_code=status_code, response_body=response_body)


async def _api_request_payload(
    method: str, url: str, params: dict[str, Any] | None = None, json_body: Any = None
) -> dict[str, Any]:
    """Perform an API request, reporting typed upstream failures as error payloads.

    Equivalent to awaiting ``_api_request`` and passing any raised
    ``NextDNSError`` through ``_handle_api_error``, so every tool reports the
    same standardized failure shape.
    """
    try:
        return await _api_request(method, url, params=params, json_body=json_body)
    except NextDNSError as e:
        return _note_404_on_a_profile_path(_handle_api_error(e), url)


# The first path segment every profile-scoped endpoint shares.
_PROFILE_PATH_RE = re.compile(r"^/profiles/([^/?#]+)")


def _note_404_on_a_profile_path(payload: dict[str, Any], url: str) -> dict[str, Any]:
    """Say which id a 404 under ``/profiles/<id>`` could be blaming.

    A 404 on that path has two possible causes and the upstream answer does not
    say which: the profile id is wrong, or the entry within the profile is.
    Live runs show the first happening because the tool description shows a
    ``profile_id="abc123"`` example and the caller sends exactly that - a
    six-character hex string, which is the shape of a real NextDNS profile id,
    so nothing about it reads as a placeholder.

    The hint names both possibilities rather than guessing one, and where to get a
    real profile id: ``listProfiles()``, the tool that lists them.
    """
    if not isinstance(payload, dict) or payload.get("upstream_code") != "notFound":
        return payload
    if not _PROFILE_PATH_RE.match(url):
        return payload
    return {
        **payload,
        "hint": (
            "a 404 here means the profile id or the entry id in this request does not exist. "
            "listProfiles() returns the profile ids this API key can access"
        ),
    }
