"""Server-side record of the tool calls this server actually executed.

SPDX-License-Identifier: MIT

Verifying an outcome over REST cannot see a read: the profile still exists
whether or not the ``getProfile`` call ever happened, so a harness that only
reads the API passes a step whose transcript was nothing but errors. The
server - the only component that knows which tools it ran and whether they
succeeded - writes that down: set ``NEXTDNS_CALL_LOG`` to a JSONL path and
every executed tool call appends one line.

One concern per module: ``store`` the log file itself, ``projection`` how
much of a value one row may carry, ``evidence`` the identity facts a verifier
matches on, ``record`` what one row says about one call, and ``middleware``
the FastMCP hook. The e2e harness reuses ``read_call_log_rows`` so no second
parser can drift from the server's own.
"""

from .middleware import CallLogMiddleware
from .projection import _PROJECTION_BOUNDS as _PROJECTION_BOUNDS
from .projection import (
    OMITTED_ITEMS_KEY,
    PROJECT_MAX_DEPTH,
    PROJECT_MAX_ITEMS,
    PROJECT_MAX_KEYS,
    PROJECT_MAX_STRING_CHARS,
)
from .record import result_error
from .store import CALL_LOG_ENV, call_log_path, get_call_log, read_call_log_rows

__all__ = [
    "CALL_LOG_ENV",
    "OMITTED_ITEMS_KEY",
    "PROJECT_MAX_DEPTH",
    "PROJECT_MAX_ITEMS",
    "PROJECT_MAX_KEYS",
    "PROJECT_MAX_STRING_CHARS",
    "CallLogMiddleware",
    "call_log_path",
    "get_call_log",
    "read_call_log_rows",
    "result_error",
]
