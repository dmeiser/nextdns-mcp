"""Standardized structured error payloads for NextDNS MCP tools.

Every tool failure is reported as a plain dict with a stable, machine-readable
``code`` and a human-readable ``error`` message. Callers (including LLMs) can
therefore predict the failure shape regardless of which tool produced it, and can
branch on the typed ``code`` instead of parsing opaque exception text.

The package is split by responsibility, one concern per module:

- ``codes`` - the typed ``ErrorCode`` names and the ``error_payload`` builder.
- ``body`` - the response-body cap, the bounded read of a failed response, the
  walk that bounds every free-form key and string in an inlined document, and the
  shared cap-and-marker helper.
- ``upstream`` - the NextDNS plural ``errors`` document: its codes, its detail
  sentences, and the clause folded into a caller's message.
- ``rate_limit`` - the ``Retry-After`` and ``X-RateLimit-*`` metadata of a 429.
- ``http`` - ``http_error_payload``, the composer: the only module here that reads a
  response, and the only one that imports ``body``, ``upstream`` and ``rate_limit``.
- ``exceptions`` - ``NextDNSError`` and its four typed subclasses.

Import the surface from here (``from nextdns_mcp.errors import ErrorCode``); names
beginning with ``_`` inside the submodules are package-internal, and a helper that
crosses into another module - here or in another module of the package - does so
under a public name, which is what ``tests/unit/test_error_boundaries.py`` enforces.
The recursive JSON bounder these modules delegate to is not part of this contract:
it lives in ``nextdns_mcp.json_bounds``, shared with the call log.

SPDX-License-Identifier: MIT
"""

from .body import (
    BODY_SLICE_BYTES,
    MAX_RESPONSE_BODY_CHARS,
    TRUNCATION_MARKER,
    cap_with_marker,
    response_body_text,
)
from .codes import ErrorCode, error_payload
from .exceptions import (
    NextDNSAuthError,
    NextDNSError,
    NextDNSRateLimitError,
    NextDNSResponseTooLargeError,
    NextDNSServerError,
    StateOwnershipError,
)
from .http import http_error_payload
from .upstream import upstream_error_codes, upstream_error_details

__all__ = [
    "BODY_SLICE_BYTES",
    "MAX_RESPONSE_BODY_CHARS",
    "TRUNCATION_MARKER",
    "ErrorCode",
    "NextDNSAuthError",
    "NextDNSError",
    "NextDNSRateLimitError",
    "NextDNSResponseTooLargeError",
    "NextDNSServerError",
    "StateOwnershipError",
    "cap_with_marker",
    "error_payload",
    "http_error_payload",
    "response_body_text",
    "upstream_error_codes",
    "upstream_error_details",
]
