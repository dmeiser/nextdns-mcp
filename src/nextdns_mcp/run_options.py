"""MCP server run options: transport resolution and host/port parsing.

SPDX-License-Identifier: MIT

Everything the entrypoint needs to turn ``MCP_TRANSPORT`` / ``MCP_HOST`` /
``MCP_PORT`` into the keyword arguments ``mcp.run()`` expects. The decisions
live here, outside ``server.py``, so the entry point composes them without
owning them:

- :func:`_resolve_transport` fails loudly on an unrecognized transport
  instead of silently downgrading to stdio (issue #294);
- :func:`_parse_port` fails fast with the project's typed error naming the
  variable the operator actually set, rather than a bare ``ValueError`` from
  ``int()`` at startup (issue #283);
- :func:`get_mcp_run_options` assembles the run kwargs and warns when an
  operator opts into a non-loopback HTTP bind, which exposes the
  unauthenticated MCP endpoint on every reachable interface.
"""

import logging
import os
from typing import Any

from .config import ConfigurationError

logger = logging.getLogger(__name__)


def _is_loopback_host(host: str) -> bool:
    """Return True if host binds to loopback interfaces only.

    Args:
        host: Hostname or IP address to check.

    Returns:
        True when the bind is restricted to loopback (127.0.0.1, ::1, localhost).
    """
    return host in ("127.0.0.1", "::1", "localhost")


# The only transports the server supports. Anything else is a configuration
# error (issue #294) rather than a silent downgrade to stdio.
_VALID_TRANSPORTS = frozenset({"stdio", "http"})


def _resolve_transport() -> str:
    """Resolve ``MCP_TRANSPORT`` to a supported transport name.

    The value is stripped and lowercased so an env-file trailing space or a
    case variant (``HTTP``) still resolves. A value that does not match the
    recognized set fails loudly instead of silently downgrading to stdio, which
    is the single most common way an operator ends up with a server that speaks
    no HTTP and no warning.

    Returns:
        str: The resolved transport name (``stdio`` or ``http``).

    Raises:
        ConfigurationError: If MCP_TRANSPORT is not a recognized transport.
    """
    raw = os.getenv("MCP_TRANSPORT", "stdio")
    mode = raw.strip().lower()
    if mode not in _VALID_TRANSPORTS:
        raise ConfigurationError(f"Invalid MCP_TRANSPORT: {raw!r}. Expected one of: {sorted(_VALID_TRANSPORTS)}.")
    return mode


def _parse_port() -> int:
    """Resolve ``MCP_PORT`` to a valid TCP port number.

    Mirrors :func:`get_http_timeout` so a malformed value fails fast with
    the project's typed error naming the variable the operator actually set,
    rather than a bare ``ValueError`` from ``int()`` at startup.

    Returns:
        int: The configured port (default 8000).

    Raises:
        ConfigurationError: If MCP_PORT is not an integer in 1-65535.
    """
    raw = os.getenv("MCP_PORT", "8000")
    try:
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigurationError(f"Invalid MCP_PORT: {raw!r}. Expected an integer in 1-65535.") from None

    return port


def get_mcp_run_options() -> dict[str, Any]:
    """Build MCP server run options based on environment configuration.

    Returns:
        Dictionary of options to pass to mcp.run() via **kwargs.
        Empty dict for stdio (default), or dict with transport/host/port for HTTP.

        HTTP mode binds to 127.0.0.1 by default (loopback-only, no authentication
        is built in). Binding to any other interface is an explicit opt-in via
        MCP_HOST and requires the operator to provide reverse-proxy/auth
        protection before exposing the port.
    """
    transport_mode = _resolve_transport()

    if transport_mode == "http":
        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = _parse_port()
        logger.info(f"  Transport: HTTP streamable on {host}:{port}")
        logger.info(f"  MCP endpoint: http://{host}:{port}/mcp")
        if not _is_loopback_host(host):
            logger.warning(
                f"  SECURITY: binding to {host} exposes the MCP endpoint on all "
                "reachable interfaces with NO authentication. This is an explicit "
                "opt-in. Put a reverse proxy with authentication in front, or keep "
                "the bind loopback-only (127.0.0.1) for local use."
            )
        return {"transport": "http", "host": host, "port": port}

    # Default: stdio transport
    logger.info("  Transport: stdio")
    return {}
