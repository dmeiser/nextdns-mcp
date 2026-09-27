"""Regression tests for FastMCP HTTP host/origin protection (issue #269).

The documented local setup binds the streamable-HTTP transport to 127.0.0.1
and, unauthenticated, is exposed to DNS rebinding unless FastMCP's
``http_host_origin_protection`` is turned on. In ``auto`` mode it rejects a
request that carries a foreign ``Host`` or ``Origin`` before it reaches the MCP
endpoint, while leaving a legitimate local client untouched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VAR = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"
ALLOWED_HOSTS_VAR = "FASTMCP_HTTP_ALLOWED_HOSTS"
PROTECTION_AUTO = "auto"
MCP_ENDPOINT = "/mcp"
DOCKERFILES = ("Dockerfile", "Dockerfile.alpine")

_IMAGE_PROBE = """
from fastmcp import settings
from starlette.testclient import TestClient

from nextdns_mcp import server

app = server.configure().http_app(transport="http", path="/mcp")
with TestClient(app, base_url="http://127.0.0.1:8000") as client:
    status = client.get("/mcp", headers={"host": "evil.example"}).status_code
print(f"setting={settings.http_host_origin_protection}")
print(f"rebinding={status}")
"""


@pytest.fixture
def build_app(monkeypatch):
    """Return a builder for the ASGI app ``configure()`` produces."""
    from nextdns_mcp import server

    monkeypatch.setattr(server, "_mcp_server", None)

    def build(**kwargs):
        server.configure()
        return server.get_mcp_server().http_app(transport="http", path=MCP_ENDPOINT, **kwargs)

    return build


def _get(app, headers: dict[str, str], scheme: str = "http"):
    from starlette.testclient import TestClient

    with TestClient(app, base_url=f"{scheme}://127.0.0.1:8000") as client:
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


def _image_environment(dockerfile: str) -> dict[str, str]:
    """Return the ``KEY=VALUE`` environment a built image inherits from its ``ENV`` blocks."""
    pairs: dict[str, str] = {}
    lines = (REPO_ROOT / dockerfile).read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("ENV "):
            entries = [line[len("ENV ") :].strip()]
            while entries[-1].endswith("\\"):
                entries[-1] = entries[-1][:-1].strip()
                index += 1
                entries.append(lines[index].strip())
            for entry in " ".join(entries).split():
                key, _, value = entry.partition("=")
                pairs[key] = value
        index += 1
    return pairs


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_shipped_image_environment_turns_the_guard_on(dockerfile: str) -> None:
    """The images' own environment must resolve to ``auto`` and actually reject a rebinding.

    The image environment is the change's only functional artifact, so it is run
    through the real server rather than matched as text: the subprocess builds
    the HTTP app the way the entrypoint does and reports what a request carrying
    a foreign ``Host`` gets.
    """
    image_env = _image_environment(dockerfile)
    env = {key: value for key, value in image_env.items() if key != "PYTHONPATH"}
    env["PATH"] = os.environ.get("PATH", "")

    result = subprocess.run(
        [sys.executable, "-c", _IMAGE_PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    observed = dict(line.split("=", 1) for line in result.stdout.split("\n") if "=" in line)
    assert observed == {"setting": PROTECTION_AUTO, "rebinding": "421"}


def test_configure_does_not_enable_origin_protection_implicitly(build_app, monkeypatch) -> None:
    """Enabling it by default would 421 a reverse proxy fronting a loopback bind.

    ``auto`` also turns on Host allowlisting, so an implicit default would make
    every request from the proxy's own hostname a Misdirected Request. Operators
    opt in, and set the allowed hosts that go with it.
    """
    import fastmcp

    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", False)

    build_app()

    assert ENV_VAR not in os.environ
    assert fastmcp.settings.http_host_origin_protection is False


def test_loopback_endpoint_rejects_a_rebound_host_header(build_app) -> None:
    """DNS rebinding sends a foreign Host for a loopback socket: reject it."""
    response = _get(build_app(host_origin_protection=PROTECTION_AUTO), {"host": "evil.example"})

    assert response.status_code == 421


def test_loopback_endpoint_rejects_a_foreign_origin_header(build_app) -> None:
    """A cross-origin browser request to the loopback endpoint is forbidden."""
    response = _get(
        build_app(host_origin_protection=PROTECTION_AUTO),
        {"host": "127.0.0.1:8000", "origin": "http://evil.example"},
    )

    assert response.status_code == 403


def test_documented_proxy_configuration_starts_and_allows_the_proxy_host(build_app) -> None:
    """The recipe the docs prescribe: JSON allowlist + ``auto`` admits the proxy's Host.

    ``FASTMCP_HTTP_ALLOWED_HOSTS`` is a pydantic-settings JSON list, so the
    documented value is ``["name"]``; a bare hostname fails validation at import
    and never reaches a request.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fastmcp import settings; print(settings.http_allowed_hosts, settings.http_host_origin_protection)",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            ENV_VAR: PROTECTION_AUTO,
            ALLOWED_HOSTS_VAR: '["mcp.example.com"]',
        },
        check=True,
    )
    assert result.stdout.strip() == "['mcp.example.com'] auto"

    app = build_app(host_origin_protection=PROTECTION_AUTO, allowed_hosts=["mcp.example.com"])
    proxy_host = {"host": "mcp.example.com"}

    assert _get(app, proxy_host).status_code == _get(build_app(host_origin_protection=False), proxy_host).status_code
    assert _get(app, {"host": "evil.example"}).status_code == 421


def test_documented_proxy_recipe_needs_the_forwarded_scheme(build_app) -> None:
    """The allowlist alone 403s the proxy's own browser traffic on a cleartext bind.

    A TLS-terminating proxy forwards ``X-Forwarded-Proto: https``, but the
    server only acts on it when proxy headers are enabled for the proxy's
    address. Without that the guard compares an ``http`` expected origin with
    the browser's ``https`` ``Origin`` and refuses, so the recipe documents the
    proxy-header step before the allowlist.
    """
    app = build_app(host_origin_protection=PROTECTION_AUTO, allowed_hosts=["mcp.example.com"])
    unguarded = build_app(host_origin_protection=False)
    browser = {"host": "mcp.example.com", "origin": "https://mcp.example.com"}

    assert _get(app, browser, scheme="http").status_code == 403
    assert _get(app, browser, scheme="https").status_code == _get(unguarded, browser, scheme="https").status_code


def test_loopback_endpoint_serves_its_own_host(build_app) -> None:
    """The guard must not break the legitimate local client it is meant to protect."""
    headers = {"host": "127.0.0.1:8000"}

    guarded = _get(build_app(host_origin_protection=PROTECTION_AUTO), headers)
    unguarded = _get(build_app(host_origin_protection=False), headers)

    assert guarded.status_code == unguarded.status_code
