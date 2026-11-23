"""The typed failures a NextDNS request can raise.

The transport raises these and the tools convert them into payloads
(``_api_request_payload``), so the hierarchy is the shape of a failure between
those two points. It is a leaf: it reads no response and imports nothing else
from the package.

SPDX-License-Identifier: MIT
"""

from typing import Any


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


class NextDNSResponseTooLargeError(NextDNSError):
    """Raised when a response body crosses a caller-stated byte ceiling.

    Raised *while streaming*, before the body is buffered whole or JSON-decoded,
    so the ceiling bounds transport memory, not just what is later returned.
    Carries the ceiling that was crossed in ``max_response_bytes``.
    """

    def __init__(self, message: str, max_response_bytes: int):
        super().__init__(message, status_code=None)
        self.max_response_bytes = max_response_bytes


class ConfigurationError(ValueError):
    """Raised when configuration values are invalid or malformed."""


class MissingApiKeyError(ConfigurationError):
    """Raised when a NextDNS API key has not been configured yet.

    Subclasses ``ConfigurationError`` so that a single ``except
    ConfigurationError`` covers both "no key configured yet" and "the value
    you configured is invalid".
    """
