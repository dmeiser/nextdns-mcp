"""Unit tests for get_mcp_run_options function."""

import os
from unittest.mock import patch

import pytest

from nextdns_mcp.server import _is_loopback_host, get_mcp_run_options


class TestGetMcpRunOptions:
    """Test get_mcp_run_options function."""

    def test_default_stdio_returns_empty_dict(self):
        """Test that default (stdio) returns empty dict."""
        with patch.dict(os.environ, {}, clear=True):
            options = get_mcp_run_options()
            assert options == {}

    def test_stdio_explicit_returns_empty_dict(self):
        """Test that explicit stdio returns empty dict."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": "stdio"}):
            options = get_mcp_run_options()
            assert options == {}

    def test_http_returns_transport_dict_with_defaults(self):
        """Test that HTTP mode returns dict with default host and port."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True):
            options = get_mcp_run_options()
            assert options == {"transport": "http", "host": "127.0.0.1", "port": 8000}

    def test_http_default_host_is_loopback_only(self):
        """Regression test for #142: default HTTP bind must be loopback-only.

        Without MCP_HOST set, the server must never bind a non-loopback
        interface, because the HTTP endpoint has no authentication.
        """
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True):
            options = get_mcp_run_options()
            assert _is_loopback_host(options["host"]), (
                f"Default HTTP bind must be loopback-only, got {options['host']!r}"
            )

    def test_http_with_custom_host_and_port(self):
        """Test that HTTP mode respects custom host and port."""
        with patch.dict(
            os.environ,
            {"MCP_TRANSPORT": "http", "MCP_HOST": "127.0.0.1", "MCP_PORT": "9999"},
        ):
            options = get_mcp_run_options()
            assert options == {"transport": "http", "host": "127.0.0.1", "port": 9999}

    def test_http_case_insensitive(self):
        """Test that transport mode is case-insensitive."""
        for transport_value in ["HTTP", "Http", "http"]:
            with patch.dict(os.environ, {"MCP_TRANSPORT": transport_value}, clear=True):
                options = get_mcp_run_options()
                assert options["transport"] == "http"
                assert "host" in options
                assert "port" in options

    def test_unknown_transport_falls_back_to_stdio(self):
        """Test that unknown transport mode falls back to stdio."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": "grpc"}):
            options = get_mcp_run_options()
            assert options == {}

    def test_http_with_only_custom_host(self):
        """Test HTTP with only host customized."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_HOST": "localhost"}, clear=True):
            options = get_mcp_run_options()
            assert options == {"transport": "http", "host": "localhost", "port": 8000}

    def test_http_with_only_custom_port(self):
        """Test HTTP with only port customized."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_PORT": "3000"}, clear=True):
            options = get_mcp_run_options()
            assert options == {"transport": "http", "host": "127.0.0.1", "port": 3000}

    def test_http_non_loopback_host_emits_security_warning(self):
        """Test that an explicit non-loopback bind logs a security warning."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_HOST": "0.0.0.0"}, clear=True),
            patch("nextdns_mcp.server.logger.warning") as mock_warning,
        ):
            options = get_mcp_run_options()
            assert options["host"] == "0.0.0.0"
            mock_warning.assert_called_once()
            assert "SECURITY" in mock_warning.call_args.args[0]

    def test_http_loopback_host_emits_no_security_warning(self):
        """Test that the default loopback bind emits no security warning."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True),
            patch("nextdns_mcp.server.logger.warning") as mock_warning,
        ):
            get_mcp_run_options()
            mock_warning.assert_not_called()

    def test_invalid_port_raises_value_error(self):
        """Test that invalid port value raises ValueError."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_PORT": "invalid"}),
            pytest.raises(ValueError),
        ):
            get_mcp_run_options()

    def test_options_can_be_unpacked_to_mcp_run(self):
        """Test that returned dict can be unpacked with **kwargs."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}, clear=True):
            options = get_mcp_run_options()
            # Simulate mcp.run(**options) - just verify dict structure
            assert isinstance(options, dict)
            # Verify all keys are valid Python identifiers (can be used as kwargs)
            for key in options:
                assert key.isidentifier(), f"Key '{key}' is not a valid identifier"


class TestIsLoopbackHost:
    """Test _is_loopback_host helper."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_hosts(self, host):
        """Test that loopback addresses are recognized."""
        assert _is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5"])
    def test_non_loopback_hosts(self, host):
        """Test that non-loopback addresses are not recognized as loopback."""
        assert _is_loopback_host(host) is False
