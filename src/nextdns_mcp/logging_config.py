"""Logging setup for the NextDNS MCP server.

``configure_logging`` lives in its own module so that importing
``nextdns_mcp.config`` stays free of logging side effects: the function is
called only from the ``__main__`` entrypoint, never at import time.
``nextdns_mcp.config`` re-exports it, so existing
``nextdns_mcp.config`` imports keep resolving.

SPDX-License-Identifier: MIT
"""

import logging

from .constants import escape_control_chars

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class SanitizingFormatter(logging.Formatter):
    """Render every record's message with control characters written out as escapes.

    The text interpolated into a record is often caller-controlled: the URL an
    ACL denial names, the tool name an argument filter was given, the profile id
    a read was refused for. A CR or LF in one of those splits one record into two
    lines that read like real records, and a raw ESC byte reaches the operator's
    terminal as a formatting command (issue #767). Escaping the message at the
    formatter is the one choke point that covers every current and future log
    site, even ones that interpolate a value directly (issue #139's rule still
    asks those sites to go through ``_redacted`` / ``_log_safe_error``).

    Only the composed message is escaped. ``exc_info`` keeps its line breaks:
    a traceback's newlines are the artifact being printed, not caller data, and
    the exception strings this server composes itself already pass through
    ``_log_safe_error`` before they reach a record.
    """

    def format(self, record: logging.LogRecord) -> str:
        # A copy, not a mutation: the record is shared with every other handler
        # on the logger, and sanitizing it here must not hide the original bytes
        # from a handler that adds its own (e.g. structured) rendering.
        safe = logging.makeLogRecord(record.__dict__)
        safe.msg = escape_control_chars(record.getMessage())
        safe.args = None
        return super().format(safe)


def configure_logging() -> None:
    """Configure root logging for the NextDNS MCP server.

    Called only from the ``__main__`` entrypoint so that importing this
    module has no logging side effects.

    The ``httpx`` and ``httpcore`` loggers are pinned to WARNING: httpx logs
    every request at INFO with the fully merged URL, whose query string can
    carry the PII the client redacts from its own request line (issue #139).
    The handler carries :class:`SanitizingFormatter`, so a record that still
    contains a caller-supplied control character cannot forge a second line.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(SanitizingFormatter(_LOG_FORMAT, _DATE_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for library_logger in ("httpx", "httpcore"):
        logging.getLogger(library_logger).setLevel(logging.WARNING)
