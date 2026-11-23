"""Logging setup for the NextDNS MCP server.

``configure_logging`` lives in its own module so that importing
``nextdns_mcp.config`` stays free of logging side effects: the function is
called only from the ``__main__`` entrypoint, never at import time.
``nextdns_mcp.config`` re-exports it, so existing
``nextdns_mcp.config`` imports keep resolving.

SPDX-License-Identifier: MIT
"""

import logging


def configure_logging() -> None:
    """Configure root logging for the NextDNS MCP server.

    Called only from the ``__main__`` entrypoint so that importing this
    module has no logging side effects.

    The ``httpx`` and ``httpcore`` loggers are pinned to WARNING: httpx logs
    every request at INFO with the fully merged URL, whose query string can
    carry the PII the client redacts from its own request line (issue #139).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for library_logger in ("httpx", "httpcore"):
        logging.getLogger(library_logger).setLevel(logging.WARNING)
