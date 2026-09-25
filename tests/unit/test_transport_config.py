"""Unit tests for MCP transport configuration.

Tests environment variable parsing and validation for transport mode,
host, and port configuration via get_mcp_run_options().

SPDX-License-Identifier: MIT
"""

import os
from unittest.mock import patch

import pytest

from nextdns_mcp.server import get_mcp_run_options


def test_default_transport_is_stdio():
    """Verify default transport is stdio when MCP_TRANSPORT not set."""
    with patch.dict(os.environ, {}, clear=True):
        assert get_mcp_run_options() == {}


def test_http_transport_from_env():
    """Verify HTTP transport is selected when MCP_TRANSPORT=http."""
    with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True):
        options = get_mcp_run_options()
        assert options["transport"] == "http"


def test_http_transport_case_insensitive():
    """Verify transport mode is case-insensitive."""
    for value in ["HTTP", "Http", "http"]:
        with patch.dict(os.environ, {"MCP_TRANSPORT": value}, clear=True):
            options = get_mcp_run_options()
            assert options["transport"] == "http"


def test_default_host_and_port():
    """Verify default HTTP host and port values.

    The default host must be loopback-only (see #142): the HTTP endpoint has
    no built-in authentication, so it must not bind all interfaces by default.
    """
    with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True):
        options = get_mcp_run_options()
        assert options["host"] == "127.0.0.1"
        assert options["port"] == 8000


def test_custom_host_and_port():
    """Verify custom HTTP host and port are respected."""
    with patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_HOST": "0.0.0.0", "MCP_PORT": "9000"}):
        options = get_mcp_run_options()
        assert options["host"] == "0.0.0.0"
        assert options["port"] == 9000


def test_invalid_port_raises_error():
    """Verify invalid port value raises ValueError."""
    with (
        patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_PORT": "invalid"}),
        pytest.raises(ValueError),
    ):
        get_mcp_run_options()
