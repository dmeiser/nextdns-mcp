"""Tests for PrefabGenerativeUI provider, the owned sandbox, and sizing rules."""

import ast
import asyncio
import inspect
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from nextdns_mcp import chart_placement as chart_placement_module
from nextdns_mcp import generative as generative_module
from nextdns_mcp import generative_sandbox as sandbox_module
from nextdns_mcp import lifespan as lifespan_module
from nextdns_mcp.chart_placement import _validate_chart_placement
from nextdns_mcp.generative import (
    MAX_CODE_BYTES,
    MAX_DATA_BYTES,
    SANDBOX_PRELUDE,
    PrefabGenerativeUI,
    _build_renderer_resource,
    _parse_ui_data,
    _result_size_bytes,
)
from nextdns_mcp.generative_sandbox import MAX_RESULT_BYTES


def test_parse_ui_data():
    assert _parse_ui_data(None) is None
    assert _parse_ui_data("") is None
    assert _parse_ui_data("   ") is None
    assert _parse_ui_data('{"key": "value"}') == {"key": "value"}
    assert _parse_ui_data({"key": "value"}) == {"key": "value"}


def test_parse_ui_data_rejects_malformed_and_non_dict():
    with pytest.raises(ValueError, match="Invalid JSON in data parameter"):
        _parse_ui_data('{"a": ')
    for payload in ("[1,2]", "42", "true", '"hi"', "null"):
        if payload == "null":
            assert _parse_ui_data(payload) is None
            continue
        with pytest.raises(ValueError, match="must be a JSON object"):
            _parse_ui_data(payload)
    for bad in (123, ["a"], True):
        with pytest.raises(ValueError):
            _parse_ui_data(bad)


def test_validate_chart_placement_rejects_charts_in_tabs():
    bad_codes = [
        "with Tabs():\n    with Tab('Charts'):\n        AreaChart(data=[], series=[])",
        "with Tab('Charts'):\n    LineChart(data=[], series=[])",
        "with Tab('Charts'):\n    BarChart(data=[], series=[])",
        "with Tab('Charts'):\n    PieChart(data=[], dataKey='q')",
        "with Tab('Charts'):\n    DonutChart(data=[])",
    ]
    for code in bad_codes:
        with pytest.raises(ValueError, match="Recharts chart components"):
            _validate_chart_placement(code)


def test_validate_chart_placement_accepts_valid_code():
    good_codes = [
        "AreaChart(data=[], series=[])\nwith Tabs():\n    with Tab('Data'):\n        DataTable()",
        "with Column():\n    PieChart(data=[])",
    ]
    for code in good_codes:
        _validate_chart_placement(code)  # should not raise


def test_chart_after_tab_block_is_accepted():
    """A dedented chart after a Tab block is not nested (false-rejection regression)."""
    code = "with Tab('Data'):\n    DataTable(columns=[], rows=[])\nAreaChart(data=[], series=[])\n"
    _validate_chart_placement(code)  # should not raise


def test_chart_in_card_after_tab_block_is_accepted():
    """A chart in a Card following a Tab block is not nested (false-rejection regression)."""
    code = "with Tab('Data'):\n    DataTable(columns=[], rows=[])\nwith Card():\n    AreaChart(data=[], series=[])\n"
    _validate_chart_placement(code)  # should not raise


def test_chart_before_tab_block_is_accepted():
    """A chart placed before any Tab block is not nested."""
    code = "AreaChart(data=[], series=[])\nwith Tab('Data'):\n    DataTable(columns=[], rows=[])\n"
    _validate_chart_placement(code)  # should not raise


def test_multiple_tab_blocks_without_charts_are_accepted():
    """Several chart-free Tab blocks must not trip the validator."""
    code = "".join(f"with Tab('T{i}'):\n    DataTable(columns=[], rows=[])\n" for i in range(5))
    _validate_chart_placement(code)  # should not raise


def test_with_tabstrigger_chart_is_rejected():
    """A hallucinated TabsTrigger keeps its charts nested even though the prelude no longer aliases it."""
    code = "with TabsTrigger('Charts'):\n    AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_tab_functional_children_chart_is_rejected():
    """Functional Tab(children=[...]) nesting bypasses `with` matching."""
    code = "Tab(children=[AreaChart(data=[], series=[])])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_tabstrigger_functional_children_chart_is_rejected():
    """Functional TabsTrigger(children=[...]) nesting is nested too."""
    code = "TabsTrigger(value='a', children=[LineChart(data=[], series=[])])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_repeated_tab_blocks_scale_subquadratically(monkeypatch):
    """Many chart-free Tab blocks must validate in one linear walk (DoS regression).

    Wall-clock ratios move with machine load, so the walk's node examinations
    are counted instead and the scaling claim becomes an exact integer check.
    """

    def count_examinations(blocks: int) -> int:
        examinations = {"n": 0}
        original = chart_placement_module._node_visits

        def counting(node, in_tab):
            examinations["n"] += 1
            return original(node, in_tab)

        monkeypatch.setattr(chart_placement_module, "_node_visits", counting)
        code = "".join(f"with Tab('T{i}'):\n    pass\n" for i in range(blocks))
        _validate_chart_placement(code)  # should not raise
        return examinations["n"]

    # The walk visits the Module root plus four nodes per Tab block (the
    # `with`, its Tab call, the tab argument, and the `pass` body). A
    # re-scanning walk would visit far more; the exact values below pin
    # that linearity.
    assert count_examinations(400) == 1 + 4 * 400
    assert count_examinations(1600) == 1 + 4 * 1600


def test_with_alias_context_is_not_a_tab():
    """`with foo:` has a Name (not Call) context expr — charts inside are accepted."""
    code = "with foo:\n    AreaChart(data=[], series=[])\n"
    _validate_chart_placement(code)  # should not raise


def test_attribute_tab_call_is_not_a_tab():
    """`mod.Tab(...)` is an attribute call, not the bare Tab alias — accepted."""
    code = "with mod.Tab('Charts'):\n    AreaChart(data=[], series=[])\n"
    _validate_chart_placement(code)  # should not raise


def test_tab_positional_chart_arg_is_rejected():
    """A chart passed positionally to Tab(...) is still nested."""
    code = "Tab(AreaChart(data=[], series=[]))\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_bare_tab_call_without_chart_is_accepted():
    """A chart-free Tab(...) call leaves a later top-level chart alone."""
    code = "Tab('Data')\nAreaChart(data=[], series=[])\n"
    _validate_chart_placement(code)  # should not raise


def test_nested_tabs_with_chart_is_rejected():
    """A chart in a Tab inside a Tab is still nested."""
    code = "with Tab('a'):\n    with Tab('b'):\n        AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_syntax_error_is_ignored():
    """Unparseable input cannot nest anything, so it is accepted."""
    _validate_chart_placement("with Tab('x':\n")  # should not raise


def test_star_kwargs_are_walked_without_a_chart():
    """`Tab(**config)` is a keyword with arg=None — the **-unpacked Name must be visited."""
    _validate_chart_placement("Tab(**config)\n")  # should not raise
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement("Tab(children=[AreaChart(data=[], series=[])], **config)\n")


def test_with_context_expr_chart_is_rejected():
    """`with Tab(children=[...])` nests the chart in the context expression, which was never walked."""
    code = "with Tab(children=[AreaChart(data=[], series=[])]):\n    pass\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_nested_with_context_expr_chart_is_rejected():
    """A Card(...) context expression inside a Tab block carries the chart through."""
    code = "with Tab('Charts'):\n    with Card(children=[AreaChart(data=[], series=[])]):\n        pass\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_async_with_context_expr_chart_is_rejected():
    """`async with` shares the same traversal as `with`."""
    code = "async def render():\n    async with Tab(children=[LineChart(data=[], series=[])]):\n        pass\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_multi_item_with_body_opens_tab_scope():
    """Tab need not be the first item of a multi-item `with` for the body to be in scope."""
    code = "with Card(), Tab('Charts'):\n    AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(code)


def test_multi_item_with_scales_linearly_in_items():
    """One `with` with many items visits its body once, not once per item (quadratic regression)."""

    def with_code(items: int) -> str:
        head = ", ".join(f"Tab('T{i}')" for i in range(items))
        body = "".join(f"    v{i} = " + " + ".join(str(j) for j in range(60)) + "\n" for i in range(120))
        return f"with {head}:\n{body}"

    start = time.perf_counter()
    _validate_chart_placement(with_code(50))  # should not raise
    small_elapsed = time.perf_counter() - start
    start = time.perf_counter()
    _validate_chart_placement(with_code(800))  # should not raise
    large_elapsed = time.perf_counter() - start
    assert large_elapsed < 10
    assert large_elapsed / max(small_elapsed, 1e-6) < 10


def test_deeply_nested_expression_does_not_recurse():
    """500 chained '+' ops nest hundreds of nodes deep — the walker must not use Python recursion."""
    expr = " + ".join(str(i) for i in range(500))
    _validate_chart_placement(f"with Tab('Charts'):\n    value = {expr}\n")  # should not raise


def test_deeply_nested_chart_under_tab_is_still_rejected():
    """Depth must not hide a chart: list literals nested 150 deep keep tripping the rule."""
    deep = "[AreaChart(data=[], series=[])]"
    for _ in range(150):
        deep = f"[{deep}]"
    with pytest.raises(ValueError, match="Recharts chart components"):
        _validate_chart_placement(f"Tab(children={deep})\n")


def _prelude_namespace() -> dict[str, Any]:
    """A namespace holding exactly what the sandbox prelude defines."""
    namespace: dict[str, Any] = {}
    exec(SANDBOX_PRELUDE, namespace)  # noqa: S102
    return namespace


def test_prelude_defines_no_shadcn_tab_aliases():
    """The TabsList/TabsTrigger/TabsContent shims built empty Tabs and rendered every panel at once."""
    namespace = _prelude_namespace()
    for alias in ("TabsList", "TabsTrigger", "TabsContent"):
        assert alias not in namespace
    assert namespace["Tabs"].__name__ == "Tabs"
    assert namespace["Tab"].__name__ == "Tab"


def test_prelude_native_tabs_nest_one_panel_per_tab():
    """The documented Tabs/Tab form keeps each panel inside its own Tab node."""
    namespace = _prelude_namespace()
    exec(  # noqa: S102
        "with PrefabApp() as app:\n"
        "    with Tabs():\n"
        "        with Tab('General', value='general'):\n"
        "            Text('general panel')\n"
        "        with Tab('Advanced', value='advanced'):\n"
        "            Text('advanced panel')\n",
        namespace,
    )
    tabs = namespace["app"].view.children[0]
    assert tabs.type == "Tabs"
    assert [panel.type for panel in tabs.children] == ["Tab", "Tab"]
    assert [[child.content for child in panel.children] for panel in tabs.children] == [
        ["general panel"],
        ["advanced panel"],
    ]


def test_prelude_data_table_keeps_rows_through_an_explicit_import():
    """An explicit DataTable import replaces the positional shim, so the docs name keyword arguments."""
    namespace = _prelude_namespace()
    exec(  # noqa: S102
        "from prefab_ui.components import DataTable\n"
        "table = DataTable(columns=[{'key': 'zone', 'header': 'Zone'}], rows=[{'zone': 'a'}])\n",
        namespace,
    )
    assert namespace["table"].rows == [{"zone": "a"}]


def test_generate_doc_names_the_native_tab_and_data_table_forms():
    """The tool instructions carry the tab form and the keyword DataTable call the sandbox accepts."""
    doc = generative_module.GENERATE_DOC
    assert "CRITICAL CHART SIZING RULE" in doc
    assert "`Tabs` container" in doc
    assert all(alias in doc for alias in ("TabsList", "TabsTrigger", "TabsContent"))
    assert "DataTable(columns=[...], rows=[...])" in doc


def test_build_renderer_resource():
    resource = _build_renderer_resource({"resourceDomains": ["example.com"]})
    assert resource.name == "Prefab Generative Renderer"
    assert resource.mime_type == "text/html;profile=mcp-app"


def test_renderer_csp_follows_the_renderer_declaration():
    """The advertised CSP domains come from the renderer's own declaration, not a copied literal."""
    csp = generative_module._renderer_csp()
    assert csp.resource_domains == ["https://cdn.jsdelivr.net"]
    assert csp.connect_domains == [
        "https://cdn.jsdelivr.net",
        "https://pypi.org",
        "https://files.pythonhosted.org",
    ]


def _touched_names(module: Any) -> set[str]:
    """Names a module's code (not prose) references: imports, aliases, attribute accesses."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _class_bases(module: Any) -> dict[str, list[str]]:
    """Every class a module defines, mapped to the names of its base classes."""
    tree = ast.parse(inspect.getsource(module))
    return {
        node.name: [ast.unparse(base) for base in node.bases]
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }


def test_generative_code_touches_no_upstream_private():
    """WORK-283: composition, not inheritance — the reported coupling is gone.

    Neither module may subclass or reach into FastMCP's ``GenerativeUI`` or
    prefab-ui's ``PyodideSandbox``: their process, teardown, setup,
    registration and sandbox state are upstream privates whose rename would
    drop a guard at request time. Traced over the syntax tree of BOTH modules
    (the maintainer's rebase condition) so docstrings and comments describing
    what used to happen cannot satisfy the assertion.
    """
    reached = _touched_names(generative_module) | _touched_names(sandbox_module)
    forbidden = {
        "PyodideSandbox",
        "Sandbox",
        "GenerativeUI",
        "_build_csp",
        "_gen",
        "_ensure_setup",
        "_setup_done",
        "_get_sandbox",
        "_sandbox",
        "_local",
        "_add_component",
        "_stop",
        "_stop_process",
        "close_all_generative_ui",
    }
    assert not (reached & forbidden), f"upstream private surface still touched: {sorted(reached & forbidden)}"
    classes = _class_bases(generative_module) | _class_bases(sandbox_module)
    assert classes["PrefabGenerativeUI"] == ["Provider"]
    assert classes["DenoPyodideSandbox"] == []


def test_new_worker_is_fresh_per_render_and_registered_for_teardown():
    """Each render gets its own worker, and the provider keeps it until that render ends.

    The shared lazily-created worker was the bug (#746): two renders held the
    same object, so the first render's cleanup stopped the second one's worker.
    """
    gen_ui = PrefabGenerativeUI()
    first = gen_ui._new_worker()
    second = gen_ui._new_worker()

    assert isinstance(first, sandbox_module.DenoPyodideSandbox)
    assert first is not second
    assert gen_ui._active_workers == {first, second}


async def _tool_map(gen_ui: PrefabGenerativeUI) -> dict[str, Any]:
    """Every tool this provider publishes, keyed by name."""
    return {tool.name: tool for tool in await gen_ui.list_tools()}


def _stub_worker() -> MagicMock:
    """A worker double exposing the lifecycle surface the provider calls."""
    worker = MagicMock(name="worker")
    worker.start = AsyncMock()
    return worker


@pytest.mark.asyncio
async def test_prefab_generative_ui_publishes_its_tools():
    gen_ui = PrefabGenerativeUI()

    tools = await _tool_map(gen_ui)
    assert "generate_prefab_ui" in tools
    assert "search_prefab_components" in tools

    gen_tool = tools["generate_prefab_ui"]
    assert gen_tool.annotations.read_only_hint is False
    assert gen_tool.annotations.destructive_hint is False

    comp_tool = tools["search_prefab_components"]
    assert comp_tool.annotations.read_only_hint is True
    assert comp_tool.annotations.destructive_hint is False

    worker = _stub_worker()
    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=worker),
    ):
        mock_exec.return_value = "fake_app"
        res = await gen_tool.fn(code="print('hi')", data='{"a": 1}')
        assert res == "fake_app"
        assert mock_exec.call_count == 1
        call_code = mock_exec.call_args[0][0]
        assert SANDBOX_PRELUDE in call_code
        assert "print('hi')" in call_code
        assert mock_exec.call_args[1]["data"] == {"a": 1}
        worker.start.assert_awaited_once()
        worker.stop.assert_called_once()


@pytest.mark.asyncio
async def test_prefab_generative_ui_tool_lookup():
    """Compatibility: registered tools are retrievable via the provider lookup."""
    gen_ui = PrefabGenerativeUI()

    gen_tool = await gen_ui.get_tool("generate_prefab_ui")
    assert gen_tool is not None
    assert gen_tool.name == "generate_prefab_ui"
    assert gen_tool.annotations.read_only_hint is False
    assert gen_tool.annotations.destructive_hint is False

    comp_tool = await gen_ui.get_tool("search_prefab_components")
    assert comp_tool is not None
    assert comp_tool.name == "search_prefab_components"

    assert await gen_ui.get_tool("does-not-exist") is None


@pytest.mark.asyncio
async def test_prefab_generative_ui_registers_renderer_resource():
    """Compatibility: the renderer resource is listed on the provider."""
    gen_ui = PrefabGenerativeUI()

    resources = await gen_ui.list_resources()
    uris = [str(r.uri) for r in resources]
    assert "ui://prefab/generative.html" in uris


@pytest.mark.asyncio
async def test_lifespan_teardown_stops_only_the_provider_it_was_entered_on():
    """One server's shutdown stops only its own workers (issue #542).

    The process-global registry this replaces emptied `_ACTIVE_GENERATIVE_UI`,
    so server B starting and finishing while server A was live killed A's
    worker mid-read.
    """
    host_a = PrefabGenerativeUI()
    host_b = PrefabGenerativeUI()
    worker_a = MagicMock(name="worker-a")
    worker_b = MagicMock(name="worker-b")
    host_a._active_workers = {worker_a}
    host_b._active_workers = {worker_b}

    async with host_a.lifespan():
        # Server B starts and finishes WHILE server A is live:
        async with host_b.lifespan():
            pass
        worker_b.stop.assert_called_once()
        worker_a.stop.assert_not_called()
        assert host_b._active_workers == set()
        assert host_a._active_workers == {worker_a}

    # A's own shutdown reclaims its own subprocess:
    worker_a.stop.assert_called_once()


@pytest.mark.asyncio
async def test_prefab_generative_ui_without_components_tool():
    gen_ui = PrefabGenerativeUI(include_components_tool=False)
    tools = await _tool_map(gen_ui)
    assert "generate_prefab_ui" in tools
    assert "search_prefab_components" not in tools


@pytest.mark.asyncio
async def test_lifespan_publishes_tools_while_it_runs():
    gen_ui = PrefabGenerativeUI()

    async with gen_ui.lifespan():
        tools = {tool.name for tool in await gen_ui.list_tools()}

    assert "generate_prefab_ui" in tools


@pytest.mark.asyncio
async def test_lifespan_stops_its_own_worker_on_exit():
    gen_ui = PrefabGenerativeUI()
    worker = MagicMock(name="worker")
    gen_ui._active_workers = {worker}

    async with gen_ui.lifespan():
        pass

    worker.stop.assert_called_once()
    assert gen_ui._active_workers == set()


@pytest.mark.asyncio
async def test_lifespan_stops_every_worker_in_flight():
    """Shutdown reaps all of this provider's renders, not only the last one (#746).

    With one shared worker, teardown could only reap that one; per-render
    workers are only reclaimed if the provider tracks the whole set.
    """
    gen_ui = PrefabGenerativeUI()
    workers = [MagicMock(name=f"worker-{index}") for index in range(3)]
    gen_ui._active_workers = set(workers)

    async with gen_ui.lifespan():
        pass

    for worker in workers:
        worker.stop.assert_called_once()
    assert gen_ui._active_workers == set()


@pytest.mark.asyncio
async def test_lifespan_is_a_noop_when_no_worker_was_started():
    gen_ui = PrefabGenerativeUI()

    async with gen_ui.lifespan():
        pass

    assert gen_ui._active_workers == set()


@pytest.mark.asyncio
async def test_lifespan_stops_the_worker_off_the_event_loop():
    """`worker.stop()` waits up to 5s on the subprocess, so it is not run on the loop."""
    stopped_on: list[threading.Thread] = []

    def blocking_stop() -> None:
        stopped_on.append(threading.current_thread())

    gen_ui = PrefabGenerativeUI()
    worker = MagicMock(name="worker")
    worker.stop.side_effect = blocking_stop
    gen_ui._active_workers = {worker}

    async with gen_ui.lifespan():
        pass

    assert len(stopped_on) == 1
    assert stopped_on[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_lifespan_teardown_survives_a_failing_stop(caplog):
    """An unreapable subprocess is logged, and never aborts the rest of shutdown."""

    def exploding_stop() -> None:
        raise RuntimeError("terminate failed")

    gen_ui = PrefabGenerativeUI()
    worker = MagicMock(name="worker")
    worker.stop.side_effect = exploding_stop
    gen_ui._active_workers = {worker}

    async with gen_ui.lifespan():
        pass

    assert gen_ui._active_workers == set()
    assert "Failed to stop the Generative UI sandbox" in caplog.text


def test_lifespan_has_no_generative_dependency():
    """Core `lifespan.py` must not reach back into the optional `generative` module."""
    source = inspect.getsource(lifespan_module)
    assert "close_all_generative_ui" not in source
    assert "from .generative" not in source
    assert not hasattr(generative_module, "close_all_generative_ui")


@pytest.mark.asyncio
async def test_a_real_server_shutdown_stops_the_providers_worker():
    """FastMCP enters provider lifespans, so server teardown reaches this teardown.

    Without the override nothing in the shipped server stops the subprocess
    (the ~4 GB leak of issue #408).
    """
    server = FastMCP("NextDNS MCP Test")
    gen_ui = PrefabGenerativeUI()
    server.add_provider(gen_ui)
    worker = MagicMock(name="worker")
    gen_ui._active_workers = {worker}

    async with server._lifespan_manager():
        pass

    worker.stop.assert_called_once()
    assert gen_ui._active_workers == set()


@pytest.mark.asyncio
async def test_generate_ui_code_size_limit():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversize = "a" * (MAX_CODE_BYTES + 1)
    with pytest.raises(ValueError, match="code exceeds maximum size"):
        await gen_tool.fn(code=oversize)


@pytest.mark.asyncio
async def test_generate_ui_oversized_code_skips_ast_parsing():
    """Oversized code is rejected by the byte guard before ast.parse runs."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversize = "a" * (MAX_CODE_BYTES + 1)
    with (
        patch("nextdns_mcp.generative._validate_chart_placement") as mock_validate,
        pytest.raises(ValueError, match="code exceeds maximum size"),
    ):
        await gen_tool.fn(code=oversize)
    mock_validate.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ui_rejects_invalid_data_type_before_serialization():
    """Non-str/non-dict data raises ValueError from validation, not TypeError from json.dumps."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    for bad in (123, ["a"], True):
        with (
            patch("nextdns_mcp.generative.json") as mock_json,
            pytest.raises(ValueError, match="data parameter must be a dict or JSON object string"),
        ):
            await gen_tool.fn(code="print(1)", data=bad)
        mock_json.dumps.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ui_oversized_dict_data():
    """An oversized dict is rejected after type validation, with a single measurement serialization."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversized_dict = {"payload": "b" * (MAX_DATA_BYTES + 1)}
    with (
        patch("nextdns_mcp.generative.json") as mock_json,
        pytest.raises(ValueError, match="data exceeds maximum size"),
    ):
        mock_json.dumps.side_effect = json.dumps
        await gen_tool.fn(code="print(1)", data=oversized_dict)
    assert mock_json.dumps.call_count == 1


@pytest.mark.asyncio
async def test_generate_ui_serializes_valid_dict_data_once():
    """Valid dict data is measured with a single json.dumps before sandbox execution."""
    import json as real_json

    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]

    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
        patch("nextdns_mcp.generative.json") as mock_json,
    ):
        mock_json.dumps.side_effect = real_json.dumps
        mock_exec.return_value = "fake_app"
        res = await gen_tool.fn(code="print('hi')", data={"a": 1})
        assert res == "fake_app"
        assert mock_json.dumps.call_count == 1
        assert mock_exec.call_args[1]["data"] == {"a": 1}


@pytest.mark.asyncio
async def test_generate_ui_data_size_limit():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversize_data = "b" * (MAX_DATA_BYTES + 1)
    with pytest.raises(ValueError, match="data exceeds maximum size"):
        await gen_tool.fn(code="print(1)", data=oversize_data)


@pytest.mark.asyncio
async def test_generate_ui_result_size_limit():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]

    class OversizedApp:
        def to_json(self):
            return {"payload": "x" * (MAX_RESULT_BYTES + 1)}

    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
    ):
        mock_exec.return_value = OversizedApp()
        with pytest.raises(ValueError, match="result exceeds maximum size"):
            await gen_tool.fn(code="print(1)")


class _ChunkedRaw:
    """Raw stream stub returning fixed-size chunks across read calls."""

    def __init__(self, data: bytes, chunk_size: int) -> None:
        self._data = data
        self._chunk_size = chunk_size
        self._pos = 0
        self.closed = False

    def read(self, _n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos : self._pos + self._chunk_size]
        self._pos += len(chunk)
        return chunk

    def read1(self, n: int) -> bytes:
        return self.read(n)

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return 0


def test_bounded_stdout_readline_reassembles_chunked_line():
    bounded = sandbox_module._BoundedStdout(
        _ChunkedRaw(b'{"id": "abc", "result": {"view": {}}}\n{"id": "def"}\n', chunk_size=7),
        limit=1024,
        on_overflow=MagicMock(),
    )
    assert bounded.readline() == b'{"id": "abc", "result": {"view": {}}}\n'
    # Bytes past the newline belong to the next response.
    assert bounded.readline() == b'{"id": "def"}\n'
    assert bounded.readline() == b""


def test_bounded_stdout_readline_raises_past_limit_and_stops_sandbox():
    overflow = MagicMock()
    bounded = sandbox_module._BoundedStdout(
        _ChunkedRaw(b"x" * 100 + b"\n", chunk_size=16),
        limit=32,
        on_overflow=overflow,
    )
    with pytest.raises(ValueError, match="result exceeds maximum size"):
        bounded.readline()
    overflow.assert_called_once_with()


def test_bounded_stdout_readline_rejects_newline_chunk_past_limit():
    """A line whose newline arrives in the chunk that crosses the limit is still rejected."""
    overflow = MagicMock()
    bounded = sandbox_module._BoundedStdout(
        _ChunkedRaw(b"x" * 100 + b"\n", chunk_size=128),
        limit=32,
        on_overflow=overflow,
    )
    with pytest.raises(ValueError, match="result exceeds maximum size"):
        bounded.readline()
    overflow.assert_called_once_with()


def test_bounded_stdout_readline_returns_partial_line_at_eof():
    bounded = sandbox_module._BoundedStdout(
        _ChunkedRaw(b'{"id": "abc"}', chunk_size=4),
        limit=1024,
        on_overflow=MagicMock(),
    )
    assert bounded.readline() == b'{"id": "abc"}'
    assert bounded.readline() == b""


class _StubStream:
    """A pipe stand-in the sandbox can write to, read from, and close."""

    def __init__(self, owner: "_StubProcess", readline: Callable[[], bytes] | None = None) -> None:
        self._owner = owner
        self._readline = readline
        self.closed = False

    def readline(self) -> bytes:
        if self._readline is not None:
            return self._readline()
        return self._owner.next_answer()

    def read(self, _n: int) -> bytes:
        return self.readline()

    def write(self, data: bytes) -> None:
        self._owner.add_request(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _StubProcess:
    """A worker stand-in that speaks the sandbox wire protocol.

    Answers are built from the request being answered, so a test can pin the
    request framing (id/code/data) while the answer's id still matches. The
    lifecycle methods record what the sandbox did to the worker.
    """

    def __init__(self, responder: Callable[[dict[str, Any]], bytes] | None = None) -> None:
        self._responder = responder or _result_answer
        self.requests: list[dict[str, Any]] = []
        self._answered = 0
        self._pending = b""
        self.stdin = _StubStream(self)
        self.stdout = _StubStream(self)
        self.stderr = _StubStream(self)
        self.terminated = False
        self.killed = False
        self.terminate_calls = 0

    def add_request(self, frame: bytes) -> None:
        self._pending += frame
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            self.requests.append(json.loads(line))

    def next_answer(self) -> bytes:
        request = self.requests[self._answered]
        self._answered += 1
        return self._responder(request)

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True
        self.terminate_calls += 1

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:
        self.killed = True


def _result_answer(request: dict[str, Any]) -> bytes:
    return json.dumps({"id": request["id"], "result": {"view": "ok"}}).encode() + b"\n"


def _error_answer(request: dict[str, Any]) -> bytes:
    return json.dumps({"id": request["id"], "error": "boom"}).encode() + b"\n"


class _BrokenStdIn(_StubStream):
    def write(self, data: bytes) -> None:
        raise OSError("broken pipe")


@pytest.mark.asyncio
async def test_run_frames_one_request_per_execution_and_discards_the_worker():
    """run() owns the wire round trip: one frame in, one frame out, then the worker is reaped."""
    sandbox = sandbox_module.DenoPyodideSandbox()
    proc = _StubProcess()
    sandbox._process = proc

    result = await sandbox.run("print(1)", data={"k": "v"})

    assert result == {"view": "ok"}
    (request,) = proc.requests
    assert set(request) == {"id", "code", "data"}
    assert request["code"] == "print(1)"
    assert request["data"] == {"k": "v"}
    assert len(request["id"]) == 32
    int(request["id"], 16)
    # The worker is discarded after the execution: a fresh one starts next time.
    assert sandbox._process is None
    assert proc.terminated


@pytest.mark.asyncio
async def test_run_defaults_data_to_an_empty_object():
    sandbox = sandbox_module.DenoPyodideSandbox()
    proc = _StubProcess()
    sandbox._process = proc

    await sandbox.run("print(1)")

    assert proc.requests[0]["data"] == {}


@pytest.mark.asyncio
async def test_run_reports_a_code_error_frame():
    """A worker-reported failure reaches the caller as RuntimeError (execute() maps it to the tool error)."""
    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = _StubProcess(responder=_error_answer)

    with pytest.raises(RuntimeError, match="boom"):
        await sandbox.run("print(1)")


@pytest.mark.asyncio
async def test_run_ignores_a_null_error_field_on_a_result_frame():
    """A frame with a result and a null error is a success; only a string error is a failure."""

    def null_error_answer(request: dict[str, Any]) -> bytes:
        return json.dumps({"id": request["id"], "result": {"view": "ok"}, "error": None}).encode() + b"\n"

    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = _StubProcess(responder=null_error_answer)

    assert await sandbox.run("print(1)") == {"view": "ok"}


@pytest.mark.asyncio
async def test_run_rejects_a_frame_for_another_request():
    def wrong_id_answer(request: dict[str, Any]) -> bytes:
        return json.dumps({"id": "not-the-request", "result": {"view": "ok"}}).encode() + b"\n"

    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = _StubProcess(responder=wrong_id_answer)

    with pytest.raises(RuntimeError, match="Invalid sandbox response"):
        await sandbox.run("print(1)")


@pytest.mark.asyncio
async def test_run_rejects_a_frame_without_a_payload():
    def payloadless_answer(request: dict[str, Any]) -> bytes:
        return json.dumps({"id": request["id"]}).encode() + b"\n"

    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = _StubProcess(responder=payloadless_answer)

    with pytest.raises(RuntimeError, match="Invalid sandbox response"):
        await sandbox.run("print(1)")


@pytest.mark.asyncio
async def test_run_rejects_a_malformed_frame():
    def garbage_answer(request: dict[str, Any]) -> bytes:
        return b"{not json\n"

    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = _StubProcess(responder=garbage_answer)

    with pytest.raises(RuntimeError, match="Invalid sandbox response"):
        await sandbox.run("print(1)")


@pytest.mark.asyncio
async def test_run_reports_a_worker_that_answers_nothing():
    def empty_answer(request: dict[str, Any]) -> bytes:
        return b""

    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = _StubProcess(responder=empty_answer)

    with pytest.raises(RuntimeError, match="Sandbox process died unexpectedly"):
        await sandbox.run("print(1)")


@pytest.mark.asyncio
async def test_run_reports_a_worker_whose_pipes_are_gone():
    sandbox = sandbox_module.DenoPyodideSandbox()
    proc = _StubProcess()
    proc.stdin = None  # type: ignore[assignment]
    sandbox._process = proc

    with pytest.raises(RuntimeError, match="Sandbox failed to start"):
        await sandbox.run("print(1)")


@pytest.mark.asyncio
async def test_run_reports_a_broken_pipe_and_discards_the_worker():
    sandbox = sandbox_module.DenoPyodideSandbox()
    proc = _StubProcess()
    proc.stdin = _BrokenStdIn(proc)
    sandbox._process = proc

    with pytest.raises(RuntimeError, match="Sandbox process died unexpectedly"):
        await sandbox.run("print(1)")

    assert proc.terminated
    assert sandbox._process is None


@pytest.mark.asyncio
async def test_run_stops_the_worker_when_the_call_is_cancelled():
    """A cancelled execution re-raises CancelledError and reaps the worker (lock released)."""
    sandbox = sandbox_module.DenoPyodideSandbox()
    proc = _StubProcess()

    def blocked_until_stopped() -> bytes:
        for _ in range(200):
            if proc.terminated:
                return b""
            time.sleep(0.02)
        return b""

    proc.stdout = _StubStream(proc, readline=blocked_until_stopped)
    sandbox._process = proc

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sandbox.run("print(1)"), timeout=0.05)

    assert proc.terminated
    assert sandbox._process is None


@pytest.mark.asyncio
async def test_run_cold_starts_a_worker_when_none_is_running(monkeypatch):
    """run() lazily starts a worker, wraps its stdout, and reports framing through it."""
    proc = _StubProcess()

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        future.set_result(proc)
        return future

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", AsyncMock())
    sandbox = sandbox_module.DenoPyodideSandbox()

    result = await sandbox.run("print(1)")

    assert result == {"view": "ok"}
    assert isinstance(proc.stdout, sandbox_module._BoundedStdout)
    assert sandbox._process is None


@pytest.mark.asyncio
async def test_start_cold_starts_only_when_no_live_worker_runs(monkeypatch):
    """start() cold-starts once, then no-ops while a live worker exists and restarts a dead one."""
    spawned: list[object] = []

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        spawned.append(env)
        future = asyncio.get_running_loop().create_future()
        future.set_result(_StubProcess())
        return future

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", AsyncMock())
    sandbox = sandbox_module.DenoPyodideSandbox()

    await sandbox.start()
    assert len(spawned) == 1
    await sandbox.start()  # live worker: no second spawn
    assert len(spawned) == 1
    sandbox._process = None
    await sandbox.start()  # dead worker: respawn
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_concurrent_renders_own_their_own_worker(monkeypatch):
    """Cleanup for one render leaves a render still in flight able to finish (#746).

    Ran through the real ``DenoPyodideSandbox`` with stubbed worker processes:
    the first render reaps only the process it started, and the second render's
    process is still un-terminated — and still registered for shutdown — while
    that render executes.
    """
    procs: list[_StubProcess] = []

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        proc = _StubProcess()
        procs.append(proc)
        future.set_result(proc)
        return future

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", AsyncMock())

    gen_ui = PrefabGenerativeUI()
    seen: list[sandbox_module.DenoPyodideSandbox] = []
    entered = asyncio.Event()
    released = [asyncio.Event(), asyncio.Event()]

    async def fake_execute(code: str, *, data: dict[str, Any] | None = None, sandbox: Any = None) -> str:
        index = len(seen)
        seen.append(sandbox)
        if index == 1:
            entered.set()
        await released[index].wait()
        return f"app-{index + 1}"

    monkeypatch.setattr(generative_module.prefab_generative, "execute", fake_execute)

    first = asyncio.create_task(gen_ui._run_in_sandbox("print(1)", None))
    second = asyncio.create_task(gen_ui._run_in_sandbox("print(2)", None))
    await asyncio.wait_for(entered.wait(), timeout=5)

    assert seen[0] is not seen[1], "both renders shared one worker"
    assert gen_ui._active_workers == {seen[0], seen[1]}
    assert not procs[0].terminated and not procs[1].terminated

    released[0].set()
    assert await first == "app-1"  # the first render's cleanup runs here
    assert procs[0].terminated, "the first render did not reap its own worker"
    assert not procs[1].terminated, "the first render's cleanup reached the other render's worker"
    assert gen_ui._active_workers == {seen[1]}

    released[1].set()
    assert await second == "app-2"
    assert procs[1].terminated
    assert gen_ui._active_workers == set()


@pytest.mark.asyncio
async def test_result_size_bytes_model_dump_json():
    class DumpApp:
        def model_dump_json(self):
            return '{"payload": "' + "x" * (MAX_RESULT_BYTES + 1) + '"}'

    assert _result_size_bytes(DumpApp()) > MAX_RESULT_BYTES


def test_result_size_bytes_serialization_fallback():
    class UnserializableApp:
        def to_json(self):
            raise TypeError("not serializable")

        def __str__(self):
            return "small"

    assert _result_size_bytes(UnserializableApp()) == len(b"small")

    class BrokenDumpApp:
        def model_dump_json(self):
            raise TypeError("broken")

        def __str__(self):
            return "tiny"

    assert _result_size_bytes(BrokenDumpApp()) == len(b"tiny")


@pytest.mark.asyncio
async def test_generate_ui_execution_timeout():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    worker = _stub_worker()
    cancelled = {"seen": False}

    async def never_resolves(*args, **kwargs):
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=worker),
    ):
        mock_exec.side_effect = never_resolves
        with pytest.raises(TimeoutError, match="execution exceeded"):
            await gen_tool.fn(code="print(1)")
        assert cancelled["seen"]
    worker.stop.assert_called_once()


# --- Worker hardening (issue #541) ---


def test_sandbox_env_strips_host_secrets(monkeypatch):
    """Host variables outside the allowlist never reach the Deno subprocess.

    The sanitizer is an allowlist, not a name-substring blocklist, so
    credential-bearing variables with unmarked names are dropped too.
    """
    monkeypatch.setenv("NEXTDNS_API_KEY", "super-secret-key")
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", "/run/secrets/key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("SOME_PASSWORD", "hunter2")
    monkeypatch.setenv("MY_PRIVATE_KEY", "pem")
    monkeypatch.setenv("DB_DSN", "postgres://user:pass@db/internal")
    monkeypatch.setenv("SERVICE_ACCOUNT_JSON", '{"client_email":"svc@example.com"}')
    monkeypatch.setenv("APP_CONFIG_JSON", '{"api_key":"leak-me"}')
    monkeypatch.setenv("RANDOM_UNMARKED_VAR", "whatever")
    # Credential-bearing names inside the DENO_ namespace must not pass on a prefix.
    monkeypatch.setenv("DENO_AUTH_TOKENS", "deadbeef@deno.land")
    monkeypatch.setenv("DENO_KV_ACCESS_TOKEN", "kv_pat_x")
    env = sandbox_module._sandbox_env()
    for stripped in (
        "NEXTDNS_API_KEY",
        "NEXTDNS_API_KEY_FILE",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "SOME_PASSWORD",
        "MY_PRIVATE_KEY",
        "DB_DSN",
        "SERVICE_ACCOUNT_JSON",
        "APP_CONFIG_JSON",
        "RANDOM_UNMARKED_VAR",
        "DENO_AUTH_TOKENS",
        "DENO_KV_ACCESS_TOKEN",
    ):
        assert stripped not in env
    # Approved variables the worker needs are preserved.
    assert env.get("PATH") == os.environ.get("PATH")
    assert env.get("HOME") == os.environ.get("HOME")
    # Only the explicitly approved Deno runtime variables pass through.
    monkeypatch.setenv("DENO_DIR", "/tmp/deno-cache")
    monkeypatch.setenv("DENO_NO_UPDATE_CHECK", "1")
    env = sandbox_module._sandbox_env()
    assert env["DENO_DIR"] == "/tmp/deno-cache"
    assert env["DENO_NO_UPDATE_CHECK"] == "1"


def test_sandbox_env_is_a_copy(monkeypatch):
    """Mutating the returned dict must not touch the host environment."""
    env = sandbox_module._sandbox_env()
    before = dict(os.environ)
    env["INJECTED"] = "x"
    assert os.environ.get("INJECTED") is None
    assert dict(os.environ) == before


def test_sandbox_startup_env_bounds_v8_heap(monkeypatch):
    """The worker heap is bounded well below the upstream 4 GiB ceiling."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "super-secret-key")
    monkeypatch.setenv("DENO_V8_FLAGS", "--max-old-space-size=8192")
    env = sandbox_module._sandbox_startup_env()
    assert env["DENO_V8_FLAGS"] == f"--max-old-space-size={sandbox_module.SANDBOX_V8_HEAP_MB}"
    assert sandbox_module.SANDBOX_V8_HEAP_MB < 4096
    assert "NEXTDNS_API_KEY" not in env


@pytest.mark.asyncio
async def test_start_spawns_with_sanitized_env_and_a_bounded_heap(monkeypatch):
    """_start forwards a sanitized environment with a bounded heap to the Deno worker."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "super-secret-key")
    captured_envs: list[dict[str, str]] = []
    proc = _StubProcess()

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        captured_envs.append(env)
        future = asyncio.get_running_loop().create_future()
        future.set_result(proc)
        return future

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", AsyncMock())

    sandbox = sandbox_module.DenoPyodideSandbox()
    started = await sandbox._start()

    assert started is proc
    (env,) = captured_envs
    assert "NEXTDNS_API_KEY" not in env
    assert env["DENO_V8_FLAGS"] == f"--max-old-space-size={sandbox_module.SANDBOX_V8_HEAP_MB}"
    assert sandbox._process is proc
    # Every answer the worker gives passes through the bounded reader.
    assert isinstance(proc.stdout, sandbox_module._BoundedStdout)


@pytest.mark.asyncio
async def test_start_reaps_the_worker_when_readiness_fails(monkeypatch):
    proc = _StubProcess()

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        future.set_result(proc)
        return future

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", AsyncMock(side_effect=RuntimeError("boot failed")))

    sandbox = sandbox_module.DenoPyodideSandbox()
    with pytest.raises(RuntimeError, match="boot failed"):
        await sandbox._start()
    assert proc.terminated
    assert sandbox._process is None


class _SpyFuture(asyncio.Future):
    """Future that records done-callbacks so cancellation behaviour is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded_callbacks = []

    def add_done_callback(self, fn, *, context=None):
        self.recorded_callbacks.append(fn)
        return super().add_done_callback(fn, context=context)


@pytest.mark.asyncio
async def test_start_cancellation_registers_reclaim(monkeypatch):
    pending = _SpyFuture()
    monkeypatch.setattr(sandbox_module, "_spawn_deno", lambda env: pending)
    sandbox = sandbox_module.DenoPyodideSandbox()

    task = asyncio.ensure_future(sandbox._start())
    await asyncio.sleep(0)  # let the task reach the shield
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending.recorded_callbacks  # worker will be reclaimed if it appears


@pytest.mark.asyncio
async def test_stop_during_cold_start_reaps_the_worker_that_boots_after_it(monkeypatch):
    """A worker that finishes booting after shutdown must not leak (issue #746).

    ``stop()`` used to swap the process slot with no coordination with the cold
    start, so ``_start`` published a subprocess afterwards that nothing owned:
    a Deno/Pyodide process surviving the server that spawned it.
    """
    proc = _StubProcess()
    booted = asyncio.Event()
    release_boot = asyncio.Event()

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        future.set_result(proc)
        return future

    async def slow_ready(process: Any) -> None:
        booted.set()
        await release_boot.wait()

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", slow_ready)

    sandbox = sandbox_module.DenoPyodideSandbox()
    task = asyncio.ensure_future(sandbox._start())
    await booted.wait()
    sandbox.stop()  # teardown lands mid-cold-start, on the shutdown's worker thread
    release_boot.set()

    with pytest.raises(RuntimeError, match="Sandbox is shut down"):
        await task
    assert proc.terminated
    assert sandbox._process is None


@pytest.mark.asyncio
async def test_a_stopped_sandbox_starts_no_new_worker(monkeypatch):
    """After teardown the sandbox is closed: a late render fails instead of spawning."""
    spawned: list[dict[str, str]] = []

    def fake_spawn(env: dict[str, str]) -> asyncio.Future:
        spawned.append(env)
        future = asyncio.get_running_loop().create_future()
        future.set_result(_StubProcess())
        return future

    monkeypatch.setattr(sandbox_module, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(sandbox_module, "_await_ready", AsyncMock())

    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox.stop()

    with pytest.raises(RuntimeError, match="Sandbox is shut down"):
        await sandbox.start()
    with pytest.raises(RuntimeError, match="Sandbox is shut down"):
        await sandbox.run("print(1)")
    assert spawned == []


def test_stop_is_locked_and_idempotent():
    """Two stops (the render's finally plus lifespan) reap the worker exactly once."""
    proc = _StubProcess()
    sandbox = sandbox_module.DenoPyodideSandbox()
    sandbox._process = proc

    sandbox.stop()
    sandbox.stop()

    assert proc.terminated
    assert proc.terminate_calls == 1
    assert sandbox._process is None
    assert sandbox._closed


def _reap_mock(**poll_return: Any) -> MagicMock:
    proc = MagicMock(name="proc")
    proc.poll.return_value = poll_return.get("value")
    return proc


def test_reap_process_terminates_and_closes_every_pipe():
    proc = _reap_mock(value=None)

    sandbox_module._reap_process(proc)

    proc.terminate.assert_called_once()
    proc.communicate.assert_called_once_with(timeout=5)
    proc.kill.assert_not_called()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        stream.close.assert_called_once()


def test_reap_process_leaves_an_exited_worker_unterminated():
    proc = _reap_mock(value=0)

    sandbox_module._reap_process(proc)

    proc.terminate.assert_not_called()
    proc.communicate.assert_called_once_with(timeout=5)


def test_reap_process_kills_a_worker_that_ignores_terminate():
    proc = _reap_mock(value=None)
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="deno", timeout=5),
        ("", ""),
    ]

    sandbox_module._reap_process(proc)

    proc.kill.assert_called_once()
    assert proc.communicate.call_count == 2


def test_reap_process_tolerates_unopened_pipes():
    proc = _reap_mock(value=0)
    proc.stdin = None

    sandbox_module._reap_process(proc)  # must not raise

    proc.terminate.assert_not_called()


def test_deno_command_raises_when_deno_missing(monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Deno is required"):
        sandbox_module._deno_command()


async def _wait_for_reap(stopped: list[object], expected: list[object]) -> None:
    """The default executor runs the scheduled reap asynchronously; give it a bounded window."""
    for _ in range(200):
        if stopped == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"executor never ran the reap: {stopped!r}")


@pytest.mark.asyncio
async def test_discard_startup_reclaims_only_completed_workers():
    """A cancelled or failed start is left alone; a completed one is reaped off the loop."""
    stopped: list[object] = []
    fake_proc = MagicMock(name="proc")
    with patch.object(sandbox_module, "_reap_process", lambda p: stopped.append(p)):
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(fake_proc)
        sandbox_module._discard_startup(done)
        await _wait_for_reap(stopped, [fake_proc])

        cancelled: asyncio.Future = asyncio.get_running_loop().create_future()
        cancelled.cancel()
        sandbox_module._discard_startup(cancelled)  # no-op
        assert stopped == [fake_proc]

        failed: asyncio.Future = asyncio.get_running_loop().create_future()
        failed.set_exception(RuntimeError("spawn error"))
        sandbox_module._discard_startup(failed)  # no-op
        assert stopped == [fake_proc]


def test_startup_error_truncates_long_output():
    short = str(sandbox_module._startup_error(["boom"]))
    assert short == "Pyodide sandbox failed to start:\nboom"

    long = str(sandbox_module._startup_error(["x" * 1500, "y" * 1500]))
    assert long.startswith("Pyodide sandbox failed to start:")
    assert "\n...\n" in long
    assert len(long) < 2200


class _FakeStderr:
    """A stderr pipe that answers from a canned list, then reports EOF.

    ``reads`` counts submissions, and ``max_reads`` makes a reader that keeps
    submitting after EOF fail loudly instead of spinning for the whole test run.
    """

    def __init__(self, lines: list[bytes], max_reads: int = 4) -> None:
        self._lines = list(lines)
        self._max_reads = max_reads
        self.reads = 0

    def readline(self) -> bytes:
        self.reads += 1
        assert self.reads <= self._max_reads, f"stderr read {self.reads} times; expected at most {self._max_reads}"
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    """Minimal stand-in for the Deno subprocess during startup."""

    def __init__(self, lines: list[bytes], returncode: int | None = None) -> None:
        self.stderr = _FakeStderr(lines)
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode


def _flag_paths(flag: str, cmd: list[str]) -> list[str]:
    """Paths carried by one ``--allow-*`` launch flag; empty for a bare (unscoped) flag."""
    for arg in cmd:
        if arg == flag:
            return []  # bare flag: the whole host filesystem
        if arg.startswith(f"{flag}="):
            return arg[len(flag) + 1 :].split(",")
    raise AssertionError(f"{flag} missing from the Deno launch arguments")


def test_deno_command_scopes_host_filesystem_access(isolated_sandbox_deno_dir, monkeypatch):
    """Regression (dmeiser/nextdns-mcp#593): the Deno launch bounds host filesystem access.

    Before the fix `_deno_command` passed bare ``--allow-read`` and ``--allow-write``,
    so the subprocess running LLM-generated Prefab/Pyodide code could read or write
    every file the server process can touch — a mounted ``NEXTDNS_API_KEY_FILE``
    secret and the application source tree included. The fix scopes reads to the
    sandbox assets plus its own cache directory, and writes to that cache directory
    alone.
    """
    from prefab_ui.sandbox._pyodide import DENO_LOCK, PREFAB_SRC, RUNNER_JS

    from nextdns_mcp import generative_sandbox

    monkeypatch.setattr(generative_sandbox.shutil, "which", lambda _name: "/usr/local/bin/deno")
    cmd = generative_sandbox._deno_command()

    # Neither bare flag may survive: each must carry an explicit path list.
    assert "--allow-read" not in cmd
    assert "--allow-write" not in cmd
    read_paths = {Path(p).resolve() for p in _flag_paths("--allow-read", cmd)}
    write_paths = {Path(p).resolve() for p in _flag_paths("--allow-write", cmd)}
    assert read_paths, "--allow-read must be scoped to explicit paths"
    assert write_paths, "--allow-write must be scoped to explicit paths"

    sandbox_dir = generative_sandbox._sandbox_deno_dir().resolve()

    # Reads cover the sandbox assets the runner needs — the runner itself, the
    # Prefab source tree it walks, the frozen lockfile — and its own cache dir.
    assert {Path(RUNNER_JS).resolve(), Path(PREFAB_SRC).resolve(), Path(DENO_LOCK).resolve()} <= read_paths
    # ...and nothing else: no path outside the assets and the sandbox cache dir.
    assert read_paths <= {
        Path(RUNNER_JS).resolve(),
        Path(PREFAB_SRC).resolve(),
        Path(DENO_LOCK).resolve(),
        sandbox_dir,
    }
    # The server's own source tree is not readable: no read path is inside src/.
    source_tree = Path(generative_sandbox.__file__).resolve().parents[1]
    assert not any(p == source_tree or p.is_relative_to(source_tree) for p in read_paths)

    # Writes reach only the dedicated sandbox cache directory.
    assert write_paths == {sandbox_dir}
    assert sandbox_dir != Path(tempfile.gettempdir()).resolve()


@pytest.fixture
def isolated_sandbox_deno_dir(monkeypatch, tmp_path):
    """Pin the sandbox cache directory to ``tmp_path`` and reset the process-scoped cache.

    ``_sandbox_deno_dir()`` caches one directory per process, so a test that
    leaves that cache set both leaks a directory into the real temp dir and
    leaks state into whichever test runs next.
    """
    from nextdns_mcp import generative_sandbox

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(generative_sandbox, "_sandbox_deno_dir_path", None)
    yield tmp_path
    cached = generative_sandbox._sandbox_deno_dir_path
    if cached is not None:
        shutil.rmtree(cached, ignore_errors=True)


def test_sandbox_deno_dir_never_adopts_a_preexisting_path(isolated_sandbox_deno_dir):
    """A planted directory or symlink at a guessable name is never adopted (issue #593 review).

    The sandbox's writable path has to be created exclusively (``O_CREAT |
    O_EXCL``, mode 0700). A predictable ``<tmp>/nextdns-mcp-sandbox-<uid>``
    adopted with ``mkdir(exist_ok=True)`` silently accepts a symlink to ``/``,
    which grants the worker the whole host, or another user's world-writable
    directory, which exposes the module and wheel cache to poisoning before the
    runner revokes its permissions.
    """
    from nextdns_mcp import generative_sandbox

    tmp = isolated_sandbox_deno_dir
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    planted_dir = tmp / f"nextdns-mcp-sandbox-{uid}"
    planted_dir.mkdir()
    planted_dir.chmod(0o777)  # umask would otherwise clip mkdir(mode=0o777)
    planted_link = tmp / "nextdns-mcp-sandbox-link"
    planted_link.symlink_to("/")

    sandbox_dir = generative_sandbox._sandbox_deno_dir()

    assert sandbox_dir not in (planted_dir, planted_link)
    # Its own real directory, not a symlink, private to this process.
    st = os.lstat(sandbox_dir)
    assert stat.S_ISDIR(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o700
    if hasattr(os, "getuid"):
        assert st.st_uid == os.getuid()
    # Unpredictable name: the prefix plus a random suffix, never the uid key.
    assert sandbox_dir.name.startswith("nextdns-mcp-sandbox-")
    assert sandbox_dir.name != f"nextdns-mcp-sandbox-{uid}"
    # The planted paths are left exactly as they were found.
    assert os.readlink(planted_link) == "/"
    assert stat.S_IMODE(planted_dir.stat().st_mode) == 0o777


def test_sandbox_deno_dir_is_canonical_reused_and_recreated(isolated_sandbox_deno_dir):
    """The directory is resolved, reused for the process lifetime, and recreated if removed.

    A symlinked ``TMPDIR`` (or macOS ``/var`` -> ``/private/var``) must not put a
    lexical path in the Deno flags that Deno resolves somewhere else, and a cache
    directory removed mid-run must not leave the worker pointed at a missing path.
    """
    from nextdns_mcp import generative_sandbox

    tmp = isolated_sandbox_deno_dir
    link_root = tmp / "link-root"
    link_root.symlink_to(tmp, target_is_directory=True)

    first = generative_sandbox._sandbox_deno_dir()
    assert not first.is_symlink()
    assert first == first.resolve()
    assert first != link_root / first.name
    # The next worker start reuses the same cache instead of re-downloading.
    assert generative_sandbox._sandbox_deno_dir() == first

    shutil.rmtree(first)
    second = generative_sandbox._sandbox_deno_dir()
    assert second != first
    assert second.is_dir()


def test_sandbox_deno_dir_is_the_directory_both_launch_sites_name(isolated_sandbox_deno_dir, monkeypatch):
    """``_deno_command`` and ``_sandbox_startup_env`` name one and the same cache directory."""
    from nextdns_mcp import generative_sandbox

    monkeypatch.setattr(generative_sandbox.shutil, "which", lambda _name: "/usr/local/bin/deno")
    sandbox_dir = generative_sandbox._sandbox_deno_dir()

    assert _flag_paths("--allow-write", generative_sandbox._deno_command()) == [str(sandbox_dir)]
    assert str(sandbox_dir) in _flag_paths("--allow-read", generative_sandbox._deno_command())
    assert generative_sandbox._sandbox_startup_env()["DENO_DIR"] == str(sandbox_dir)


def test_sandbox_deno_dir_does_not_accumulate_across_restarts(isolated_sandbox_deno_dir):
    """The exit hook removes the process cache instead of leaving a directory per run."""
    from nextdns_mcp import generative_sandbox

    sandbox_dir = generative_sandbox._sandbox_deno_dir()
    assert sandbox_dir.is_dir()
    generative_sandbox._drop_sandbox_deno_dir()
    assert not sandbox_dir.exists()


def test_drop_sandbox_deno_dir_without_a_created_cache_is_a_noop(isolated_sandbox_deno_dir):
    """The exit hook must tolerate a cache that was never created (e.g. no sandbox ran)."""
    from nextdns_mcp import generative_sandbox

    generative_sandbox._drop_sandbox_deno_dir()  # must not raise when the cache is absent


def test_sandbox_startup_env_relocates_deno_cache(isolated_sandbox_deno_dir, monkeypatch):
    """DENO_DIR points at the sandbox cache dir so the wheel cache lands inside the only writable path."""
    from nextdns_mcp import generative_sandbox

    monkeypatch.setenv("NEXTDNS_API_KEY", "super-secret-key")
    env = generative_sandbox._sandbox_startup_env()
    assert env["DENO_DIR"] == str(generative_sandbox._sandbox_deno_dir())
    assert "NEXTDNS_API_KEY" not in env


@pytest.mark.asyncio
async def test_await_ready_consumes_lines_until_ready():
    proc = _FakeProc([b"boot log\n", b"pyodide:ready\n"])
    await sandbox_module._await_ready(proc)  # returns once readiness is signalled


@pytest.mark.asyncio
async def test_await_ready_raises_when_worker_dies():
    proc = _FakeProc([b"import error\n", b""], returncode=1)
    with pytest.raises(RuntimeError, match="Pyodide sandbox failed to start"):
        await sandbox_module._await_ready(proc)
    assert proc.stderr.reads == 2  # the log line, then the EOF that ends the wait


@pytest.mark.asyncio
async def test_await_ready_raises_on_stderr_eof_while_worker_alive():
    """A worker that closes stderr without signalling must fail startup, not spin.

    An empty `readline()` is EOF, so readiness can never arrive; before the fix the
    `while True` loop re-submitted reads to the executor until the caller's cold-start
    timeout cancelled it (issue #595).
    """
    proc = _FakeProc([b"boot log\n"], returncode=None)  # poll() stays None: the worker is alive
    with pytest.raises(RuntimeError, match="Pyodide sandbox failed to start"):
        await sandbox_module._await_ready(proc)
    assert proc.stderr.reads == 2  # the log line, then the EOF that ends the wait


@pytest.mark.asyncio
async def test_read_startup_line_raises_on_eof_while_worker_alive():
    """EOF on a live worker's stderr is a failed startup, the same as a dead one."""
    stderr = _FakeStderr([b""])
    lines: list[str] = []
    with pytest.raises(RuntimeError, match="Pyodide sandbox failed to start"):
        await sandbox_module._read_startup_line(stderr, lines)
    assert stderr.reads == 1
    assert lines == []


@pytest.mark.asyncio
async def test_generate_ui_cold_start_timeout_is_distinct():
    """A slow sandbox boot raises a cold-start error and never runs user code."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]

    async def never_started():
        await asyncio.sleep(9999)

    worker = MagicMock(start=AsyncMock(side_effect=never_started))
    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=worker),
        patch.object(generative_module, "SANDBOX_START_TIMEOUT", 0.05),
    ):
        with pytest.raises(TimeoutError, match="cold start exceeded"):
            await gen_tool.fn(code="print(1)")
        mock_exec.assert_not_called()
    worker.stop.assert_called_once()
