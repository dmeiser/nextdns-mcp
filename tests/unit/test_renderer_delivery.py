"""Renderer delivery regressions: one self-contained bundle, no external CSP.

SPDX-License-Identifier: MIT
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastmcp import Client
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_UI_TOOLS = ("overviewDashboard", "plotAnalytics", "generate_prefab_ui")
_EMPTY_CSP = {
    "resourceDomains": [],
    "connectDomains": [],
    "frameDomains": [],
    "baseUriDomains": [],
}
_REPO_ROOT = Path(__file__).parents[2]


class _ExternalAssetParser(HTMLParser):
    """Collect real external script/link tags, not HTML literals inside the bundle."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"script", "link"}:
            return
        values = dict(attrs)
        url = values.get("src") or values.get("href")
        if url:
            self.urls.append(url)


def _external_assets(html: str) -> list[str]:
    parser = _ExternalAssetParser()
    parser.feed(html)
    return parser.urls


@pytest.mark.asyncio
async def test_every_ui_resource_is_bundled_with_no_external_csp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The #750 oracle: both composites and the generative tool run without CDN assets or origins.

    The bundled bundle still carries a browser-side Pyodide streaming bridge, but
    with no allowed resource or connect origin it cannot fetch its loader. The
    guarded server sandbox remains the only Python executor; completed results
    still reach the same renderer through the tool result.
    """
    monkeypatch.delenv("PREFAB_RENDERER_URL", raising=False)
    monkeypatch.delenv("PREFAB_BUNDLED_RENDERER", raising=False)
    monkeypatch.setenv("NEXTDNS_API_KEY", "k")
    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
    monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

    from nextdns_mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        for name in _UI_TOOLS:
            uri = tools[name].meta["ui"]["resourceUri"]
            resource = (await client.read_resource(uri))[0]
            assert _external_assets(resource.text) == [], f"{name} still loads external renderer assets"
            assert resource.meta["ui"]["csp"] == _EMPTY_CSP, f"{name} still permits external origins"
            if name == "generate_prefab_ui":
                # The pinned renderer bundles the same streaming-capable bundle in
                # both modes; the empty CSP, not the absence of this handler, is
                # what keeps partial tool input from starting browser-side Python.
                assert "ontoolinputpartial" in resource.text
                assert "cdn.jsdelivr.net/pyodide/" in resource.text


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"HTTP server never listened on port {port}")


def test_bundled_resources_read_over_streamable_http() -> None:
    """The official HTTP client must read the >1 MiB bundle without an SSE event-cap failure.

    The in-process Client above cannot see streamable-HTTP framing, which is
    where nextdns-mcp#750's delivery broke after the switch to the bundle.
    """
    port = _free_port()
    env = {**os.environ, "NEXTDNS_API_KEY": "k", "NEXTDNS_GENERATIVE_UI": "on", "NEXTDNS_TOOL_SEARCH": "off"}
    env.update(
        MCP_TRANSPORT="http",
        MCP_HOST="127.0.0.1",
        MCP_PORT=str(port),
        FASTMCP_HTTP_HOST_ORIGIN_PROTECTION="off",
        FASTMCP_CHECK_FOR_UPDATES="off",
    )
    env.pop("PREFAB_RENDERER_URL", None)
    env.pop("PREFAB_BUNDLED_RENDERER", None)

    server = subprocess.Popen(
        [sys.executable, "-m", "nextdns_mcp.server"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def _probe() -> None:
        async with (
            asyncio.timeout(90),
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            for name in _UI_TOOLS:
                uri = str(tools[name].meta["ui"]["resourceUri"])
                resource = (await session.read_resource(uri)).contents[0]
                assert len(resource.text.encode()) > 1024 * 1024, f"{name} did not serve the bundle"
                assert resource.meta["ui"]["csp"] == _EMPTY_CSP

    try:
        _wait_for_port(port)
        asyncio.run(_probe())
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)


@pytest.mark.parametrize("dockerfile", ("Dockerfile", "Dockerfile.alpine"))
def test_both_images_default_to_the_bundled_renderer(dockerfile: str) -> None:
    assert "PREFAB_BUNDLED_RENDERER=1" in (_REPO_ROOT / dockerfile).read_text(encoding="utf-8")
