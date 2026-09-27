"""Regression tests for FastMCP HTTP host/origin protection (issue #269).

The documented local setup binds the streamable-HTTP transport to 127.0.0.1,
but FastMCP's ``http_host_origin_protection`` defaults to ``False`` -- the one
free control that mitigates DNS rebinding against that unauthenticated loopback
endpoint is switched off. ``configure()`` must therefore turn on ``auto`` mode,
and a request that carries a foreign ``Host`` or ``Origin`` must be rejected
before it reaches the MCP endpoint.
"""

import os
import subprocess
import sys

import pytest

ENV_VAR = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"
PROTECTION_AUTO = "auto"
MCP_ENDPOINT = "/mcp"
# The bind address ``get_mcp_run_options()`` defaults to; the ASGI scope's
# ``server`` entry mirrors it, which is what FastMCP's guard keys on.
LOOPBACK_BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture
def http_app(monkeypatch):
    """Return the ASGI app ``configure()`` produces, with ambient settings cleared."""
    import fastmcp

    from nextdns_mcp import server

    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", False)
    monkeypatch.setattr(server, "_mcp_server", None)

    def build():
        server.configure()
        return server.get_mcp_server().http_app(transport="http", path=MCP_ENDPOINT)

    return build


def _get(app, headers: dict[str, str]):
    from starlette.testclient import TestClient

    with TestClient(app, base_url=LOOPBACK_BASE_URL) as client:
        return client.get(MCP_ENDPOINT, headers=headers)


def test_env_var_enables_auto_mode_in_fastmcp() -> None:
    """The shipped value must actually resolve FastMCP's setting to the auto mode."""
    result = subprocess.run(
        [sys.executable, "-c", "from fastmcp import settings; print(settings.http_host_origin_protection)"],
        capture_output=True,
        text=True,
        env={**os.environ, ENV_VAR: PROTECTION_AUTO},
        check=True,
    )
    assert result.stdout.strip() == PROTECTION_AUTO


def test_configure_enables_auto_mode_by_default(http_app) -> None:
    """The local loopback deployment the issue describes is the one guard covers."""
    import fastmcp

    http_app()

    assert os.environ[ENV_VAR] == PROTECTION_AUTO
    assert fastmcp.settings.http_host_origin_protection == PROTECTION_AUTO


def test_configure_keeps_an_explicit_operator_choice(http_app, monkeypatch) -> None:
    """An operator who set the variable keeps it, as with the update check."""
    import fastmcp

    monkeypatch.setenv(ENV_VAR, "false")
    http_app()

    assert fastmcp.settings.http_host_origin_protection is False


def test_loopback_endpoint_rejects_a_rebound_host_header(http_app) -> None:
    """DNS rebinding sends a foreign Host for a loopback socket: reject it."""
    response = _get(http_app(), {"host": "evil.example"})

    assert response.status_code == 421


def test_loopback_endpoint_rejects_a_foreign_origin_header(http_app) -> None:
    """A cross-origin browser request to the loopback endpoint is forbidden."""
    response = _get(
        http_app(),
        {"host": "127.0.0.1:8000", "origin": "http://evil.example"},
    )

    assert response.status_code == 403


def test_loopback_endpoint_serves_its_own_host(http_app) -> None:
    """The guard must not break the legitimate local client it is meant to protect."""
    response = _get(http_app(), {"host": "127.0.0.1:8000"})

    assert response.status_code != 421
