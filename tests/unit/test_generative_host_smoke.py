"""Host-level smoke test for generated Prefab trees (issue #422).

Unlike ``test_generative.py`` — which mocks ``execute`` and the sandbox —
this module runs generated code for real through the Deno/Pyodide sandbox
and asserts the host-contract surface the client must honor:

- the sandbox executes generated Prefab code and returns a rendered app
  (Pyodide compat),
- the renderer resource advertises the CSP domains the client must enforce
  (``resourceDomains``/``connectDomains``),
- the chart-sizing guard still rejects charts nested in tabs.

Requires the Deno binary (the Prefab sandbox backend); skipped without it.
"""

import shutil

import pytest

from nextdns_mcp.generative import (
    NextDNSGenerativeUI,
    _build_renderer_resource,
    _validate_chart_placement,
)

requires_deno = pytest.mark.skipif(
    shutil.which("deno") is None,
    reason="Prefab sandbox requires the Deno binary",
)

SMOKE_CODE = """with PrefabApp() as app:
    with Column(gap=4):
        Heading("Smoke Dashboard")
        BarChart(
            data=[{"month": "Jan", "q": 100}, {"month": "Feb", "q": 200}],
            series=[ChartSeries(data_key="q", label="Queries")],
            x_axis="month",
        )
"""


@requires_deno
@pytest.mark.asyncio
async def test_host_smoke_executes_generated_tree() -> None:
    """Generated Prefab code executes in the real sandbox and renders a chart."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tools = {t.name: t for t in await gen_ui._local.list_tools()}
    app = await tools["generate_prefab_ui"].fn(code=SMOKE_CODE)
    tree = app.model_dump(by_alias=True)
    assert tree["title"] == "Prefab"
    column = tree["view"]["children"][0]
    assert column["type"] == "Column"
    charts = [c for c in column["children"] if c["type"] == "BarChart"]
    assert len(charts) == 1
    assert charts[0]["data"] == [
        {"month": "Jan", "q": 100},
        {"month": "Feb", "q": 200},
    ]


@requires_deno
@pytest.mark.asyncio
async def test_host_smoke_renderer_resource_advertises_csp() -> None:
    """The renderer resource carries the CSP domains the host must enforce."""
    from fastmcp.apps.generative import _build_csp

    resource = _build_renderer_resource(_build_csp())
    assert resource.mime_type == "text/html;profile=mcp-app"
    csp = resource.meta["ui"]["csp"]
    assert csp["resourceDomains"] == ["https://cdn.jsdelivr.net"]
    assert csp["connectDomains"] == [
        "https://cdn.jsdelivr.net",
        "https://pypi.org",
        "https://files.pythonhosted.org",
    ]


def test_host_smoke_chart_sizing_guard_rejects_charts_in_tabs() -> None:
    """Charts nested in tabs are rejected so they never render at 0px width."""
    with pytest.raises(ValueError, match="cannot be placed"):
        _validate_chart_placement("with Tabs():\n    with Tab('Charts'):\n        BarChart(data=[], series=[])")
