"""Renderer delivery regressions: one self-contained bundle, no external CSP.

SPDX-License-Identifier: MIT
"""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastmcp import Client

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


@pytest.mark.parametrize("dockerfile", ("Dockerfile", "Dockerfile.alpine"))
def test_both_images_default_to_the_bundled_renderer(dockerfile: str) -> None:
    assert "PREFAB_BUNDLED_RENDERER=1" in (_REPO_ROOT / dockerfile).read_text(encoding="utf-8")
