"""Unit tests for MCP transport configuration.

Tests environment variable parsing and validation for transport mode,
host, and port configuration.

SPDX-License-Identifier: MIT
"""

import os
from unittest.mock import patch

import pytest


def test_default_transport_is_stdio():
    """Verify default transport is stdio when MCP_TRANSPORT not set."""
    with patch.dict(os.environ, {}, clear=True):
        transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
        assert transport == "stdio"


def test_http_transport_from_env():
    """Verify HTTP transport is selected when MCP_TRANSPORT=http."""
    with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}):
        transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
        assert transport == "http"


def test_http_transport_case_insensitive():
    """Verify transport mode is case-insensitive."""
    for value in ["HTTP", "Http", "http"]:
        with patch.dict(os.environ, {"MCP_TRANSPORT": value}):
            transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
            assert transport == "http"


def test_default_host_and_port():
    """Verify default HTTP host and port values."""
    with patch.dict(os.environ, {}, clear=True):
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        assert host == "0.0.0.0"
        assert port == 8000


def test_custom_host_and_port():
    """Verify custom HTTP host and port are respected."""
    with patch.dict(os.environ, {"MCP_HOST": "127.0.0.1", "MCP_PORT": "9000"}):
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        assert host == "127.0.0.1"
        assert port == 9000


def test_invalid_port_raises_error():
    """Verify invalid port value raises ValueError."""
    with patch.dict(os.environ, {"MCP_PORT": "invalid"}):
        with pytest.raises(ValueError):
            int(os.getenv("MCP_PORT", "8000"))
