"""Host-level smoke test for generated Prefab trees (issue #422).

Unlike ``test_generative.py`` — which mocks ``execute`` and the sandbox —
this module runs generated code for real through the Deno/Pyodide sandbox
and asserts the host-contract surface the client must honor:

- the sandbox executes generated Prefab code and returns a rendered app
  (Pyodide compat),
- the renderer resource advertises the CSP domains the client must enforce
  (``resourceDomains``/``connectDomains``),
- the chart-sizing guard still rejects charts nested in tabs,
- oversized generated views are rejected during the bounded stdout read
  (nextdns-mcp#538), before full JSON decode and host validation.

Requires the Deno binary (the Prefab sandbox backend); skipped without it.
"""

import shutil

import pytest

from nextdns_mcp.chart_placement import _validate_chart_placement
from nextdns_mcp.generative import NextDNSGenerativeUI, _build_renderer_resource
from nextdns_mcp.generative_sandbox import MAX_RESULT_BYTES, MAX_WIRE_LINE_BYTES

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


@requires_deno
@pytest.mark.asyncio
async def test_host_smoke_rejects_oversized_result_during_read(monkeypatch) -> None:
    """Regression (nextdns-mcp#538): a runaway view is rejected during the bounded stdout read.

    The generated Text alone exceeds MAX_WIRE_LINE_BYTES, so rejection must happen
    before the host buffers the whole line, JSON-decodes it, and validates the app.
    The decode/validation instrumentation is the discriminating assertion: with the
    unbounded ``stdout.readline()`` this ticket fixes, the oversized line IS decoded
    and handed to ``PrefabApp.from_json`` before the host-level size check fires, so
    the size assertions below fail on pre-fix code even though the error message matches.
    """
    import json as real_json

    import prefab_ui.app as prefab_app
    import prefab_ui.sandbox._pyodide as pyodide_sandbox

    decoded_line_sizes: list[int] = []
    validated_wire_sizes: list[int] = []

    class _RecordingJson:
        """Stand-in for the json module inside PyodideSandbox.run's module namespace."""

        @staticmethod
        def dumps(*args: object, **kwargs: object) -> str:
            return real_json.dumps(*args, **kwargs)

        @staticmethod
        def loads(data: bytes, *args: object, **kwargs: object) -> object:
            decoded_line_sizes.append(len(data))
            return real_json.loads(data, *args, **kwargs)

    real_from_json = prefab_app.PrefabApp.from_json

    def _recording_from_json(wire: object) -> object:
        validated_wire_sizes.append(len(real_json.dumps(wire)))
        return real_from_json(wire)

    monkeypatch.setattr(pyodide_sandbox, "json", _RecordingJson)
    monkeypatch.setattr(prefab_app.PrefabApp, "from_json", _recording_from_json)

    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tools = {t.name: t for t in await gen_ui._local.list_tools()}
    # The string is built at runtime in the worker so the submitted code stays
    # tiny; only the serialized result exceeds the wire cap.
    oversized = f'with PrefabApp() as app:\n    with Column(gap=4):\n        Text("x" * {MAX_WIRE_LINE_BYTES + 1})\n'
    with pytest.raises(ValueError, match="result exceeds maximum size"):
        await tools["generate_prefab_ui"].fn(code=oversized)

    # The oversized wire must never have been decoded or validated — on pre-fix
    # code both lists carry a ~MAX_WIRE_LINE_BYTES+ entry and these assertions fail.
    assert all(size <= MAX_WIRE_LINE_BYTES for size in decoded_line_sizes)
    assert all(size <= MAX_WIRE_LINE_BYTES for size in validated_wire_sizes)

    # The oversized response stopped the worker mid-stream; the next call must
    # recover on a fresh worker and render a normal view.
    app = await tools["generate_prefab_ui"].fn(code=SMOKE_CODE)
    assert app.model_dump(by_alias=True)["title"] == "Prefab"


@requires_deno
@pytest.mark.asyncio
async def test_host_smoke_result_just_under_limit_succeeds() -> None:
    """A view whose wire stays within MAX_RESULT_BYTES is not falsely rejected."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tools = {t.name: t for t in await gen_ui._local.list_tools()}
    # Build the near-limit string at runtime so the code stays under MAX_CODE_BYTES;
    # the serialized wire stays just inside MAX_RESULT_BYTES.
    text_len = MAX_RESULT_BYTES - 4096
    code = f'with PrefabApp() as app:\n    with Column(gap=4):\n        Text("x" * {text_len})\n'
    app = await tools["generate_prefab_ui"].fn(code=code)
    assert app.model_dump(by_alias=True)["title"] == "Prefab"


@requires_deno
@pytest.mark.asyncio
async def test_host_smoke_state_does_not_leak_between_calls() -> None:
    """Mutating shared interpreter state in one call cannot poison the next (issue #541)."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    gen_tool = {t.name: t for t in await gen_ui._local.list_tools()}["generate_prefab_ui"]

    poison = """import builtins
builtins._nextdns_poison = "injected"
with PrefabApp() as app:
    Heading("poison")
"""
    probe = """import builtins
with PrefabApp() as app:
    Text(str(getattr(builtins, "_nextdns_poison", "clean")))
"""
    await gen_tool.fn(code=poison)
    app = await gen_tool.fn(code=probe)
    tree = app.model_dump(by_alias=True)
    text = tree["view"]["children"][0]
    assert text["type"] == "Text"
    assert text["content"] == "clean"
