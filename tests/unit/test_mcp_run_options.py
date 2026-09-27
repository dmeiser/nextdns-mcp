"""Unit tests for get_mcp_run_options function."""

import os
from unittest.mock import patch

import pytest

from nextdns_mcp.config import ConfigurationError
from nextdns_mcp.server import _is_loopback_host, _parse_port, _resolve_transport, get_mcp_run_options


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

    def test_unknown_transport_raises_configuration_error(self):
        """Test that an unknown transport fails loudly instead of silently downgrading to stdio (#294)."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": "grpc"}, clear=True),
            pytest.raises(ConfigurationError),
        ):
            get_mcp_run_options()

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

    def test_invalid_port_raises_configuration_error(self):
        """Test that an invalid port value raises ConfigurationError (#283)."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_PORT": "invalid"}),
            pytest.raises(ConfigurationError),
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


class TestResolveTransport:
    """Test _resolve_transport validation for MCP_TRANSPORT (#294).

    A value the operator clearly did not intend must fail loudly instead of
    silently downgrading to stdio; a trivially misspelled-but-clear value
    (extra whitespace, case) must still resolve.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http", "http"),
            ("HTTP", "http"),  # case-insensitive
            ("Http", "http"),
            (" http", "http"),  # leading whitespace stripped
            ("http ", "http"),  # trailing whitespace stripped
            ("  HTTP  ", "http"),
            ("stdio", "stdio"),
            (" STDIO ", "stdio"),  # case-insensitive and trimmed
        ],
    )
    def test_valid_transports_resolve(self, raw, expected):
        """A recognized transport (modulo case/whitespace) resolves to it."""
        with patch.dict(os.environ, {"MCP_TRANSPORT": raw}, clear=True):
            assert _resolve_transport() == expected

    def test_unset_defaults_to_stdio(self):
        """With MCP_TRANSPORT unset, resolve to the stdio default."""
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_transport() == "stdio"

    @pytest.mark.parametrize("raw", ["https", "sse", "typo", "", " "])
    def test_invalid_transports_raise_configuration_error(self, raw):
        """An unrecognized transport fails loudly with ConfigurationError (#294)."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": raw}, clear=True),
            pytest.raises(ConfigurationError),
        ):
            _resolve_transport()

    @pytest.mark.parametrize("raw", ["https", "sse", "typo", ""])
    def test_invalid_transports_raise_through_get_mcp_run_options(self, raw):
        """The failure surfaces through get_mcp_run_options, not a silent stdio."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": raw}, clear=True),
            pytest.raises(ConfigurationError),
        ):
            get_mcp_run_options()


class TestParsePort:
    """Test _parse_port validation for MCP_PORT (#283).

    A malformed MCP_PORT must fail fast with the project's typed
    ConfigurationError, naming the variable the operator set, exactly as
    NEXTDNS_HTTP_TIMEOUT does, instead of a bare ValueError from int().
    """

    def test_unset_defaults_to_8000(self):
        """With MCP_PORT unset, the default port is 8000."""
        with patch.dict(os.environ, {}, clear=True):
            assert _parse_port() == 8000

    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("8000", 8000), ("65535", 65535)])
    def test_valid_ports_parse_to_int(self, raw, expected):
        """A port in range parses to the integer mcp.run() expects."""
        with patch.dict(os.environ, {"MCP_PORT": raw}, clear=True):
            assert _parse_port() == expected

    @pytest.mark.parametrize("raw", ["invalid", "", "  ", "8000.5", "0", "-1", "65536", "99999"])
    def test_invalid_ports_raise_configuration_error(self, raw):
        """Non-numeric, empty, and out-of-range ports fail with ConfigurationError."""
        with (
            patch.dict(os.environ, {"MCP_PORT": raw}, clear=True),
            pytest.raises(ConfigurationError, match="MCP_PORT"),
        ):
            _parse_port()

    @pytest.mark.parametrize("raw", ["invalid", "", "0", "65536"])
    def test_invalid_ports_raise_through_get_mcp_run_options(self, raw):
        """The typed failure surfaces through get_mcp_run_options, not a bare ValueError."""
        with (
            patch.dict(os.environ, {"MCP_TRANSPORT": "http", "MCP_PORT": raw}, clear=True),
            pytest.raises(ConfigurationError, match="MCP_PORT"),
        ):
            get_mcp_run_options()


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
