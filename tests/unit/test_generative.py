"""Tests for NextDNSGenerativeUI provider and sizing rules."""

import asyncio
import inspect
import json
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from nextdns_mcp import chart_placement as chart_placement_module
from nextdns_mcp import generative as generative_module
from nextdns_mcp import openapi as openapi_module
from nextdns_mcp.chart_placement import _validate_chart_placement
from nextdns_mcp.generative import (
    MAX_CODE_BYTES,
    MAX_DATA_BYTES,
    SANDBOX_PRELUDE,
    NextDNSGenerativeUI,
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
    """TabsTrigger is aliased to Tab in the prelude, so its charts are nested too."""
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


def test_build_renderer_resource():
    resource = _build_renderer_resource({"resourceDomains": ["example.com"]})
    assert resource.name == "Prefab Generative Renderer"
    assert resource.mime_type == "text/html;profile=mcp-app"


@pytest.mark.asyncio
async def test_nextdns_generative_ui_setup_and_execution():
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    assert gen_ui._setup_done is True

    # Calling again is a no-op
    gen_ui._ensure_setup()

    # Verify registered tools
    tool_list = await gen_ui._local.list_tools()
    tools = {t.name: t for t in tool_list}
    assert "generate_prefab_ui" in tools
    assert "search_prefab_components" in tools

    gen_tool = tools["generate_prefab_ui"]
    assert gen_tool.annotations.read_only_hint is False
    assert gen_tool.annotations.destructive_hint is False

    comp_tool = tools["search_prefab_components"]
    assert comp_tool.annotations.read_only_hint is True
    assert comp_tool.annotations.destructive_hint is False

    # Test execution through generate_ui
    sandbox = MagicMock()
    sandbox.start = AsyncMock()
    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=sandbox),
    ):
        mock_exec.return_value = "fake_app"
        res = await gen_tool.fn(code="print('hi')", data='{"a": 1}')
        assert res == "fake_app"
        assert mock_exec.call_count == 1
        call_code = mock_exec.call_args[0][0]
        assert SANDBOX_PRELUDE in call_code
        assert "print('hi')" in call_code
        assert mock_exec.call_args[1]["data"] == {"a": 1}
        sandbox.start.assert_awaited_once()
        sandbox.stop.assert_called_once()


@pytest.mark.asyncio
async def test_nextdns_generative_ui_tool_lookup():
    """Compatibility: registered tools are retrievable via the local provider lookup."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()

    gen_tool = await gen_ui._local.get_tool("generate_prefab_ui")
    assert gen_tool is not None
    assert gen_tool.name == "generate_prefab_ui"
    assert gen_tool.annotations.read_only_hint is False
    assert gen_tool.annotations.destructive_hint is False

    comp_tool = await gen_ui._local.get_tool("search_prefab_components")
    assert comp_tool is not None
    assert comp_tool.name == "search_prefab_components"

    assert await gen_ui._local.get_tool("does-not-exist") is None


@pytest.mark.asyncio
async def test_nextdns_generative_ui_registers_renderer_resource():
    """Compatibility: _ensure_setup registers the renderer resource on the local provider."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()

    resources = await gen_ui._local.list_resources()
    uris = [str(r.uri) for r in resources]
    assert "ui://prefab/generative.html" in uris


@pytest.mark.asyncio
async def test_nextdns_generative_ui_lifespan_and_sandbox_reuse():
    """Compatibility: local-provider lifespan cycles cleanly and sandbox is reused."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()

    async with gen_ui._local.lifespan():
        pass

    sentinel = MagicMock(name="sandbox")
    gen_ui._sandbox = sentinel
    assert gen_ui._get_sandbox() is sentinel
    assert gen_ui._get_sandbox() is sentinel


@pytest.mark.asyncio
async def test_nextdns_generative_ui_without_components_tool():
    gen_ui = NextDNSGenerativeUI(include_components_tool=False)
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    tools = {t.name: t for t in tool_list}
    assert "generate_prefab_ui" in tools
    assert "search_prefab_components" not in tools


@pytest.mark.asyncio
async def test_lifespan_runs_setup_before_yield():
    """The override keeps the base provider's lazy setup and local-provider lifespan."""
    gen_ui = NextDNSGenerativeUI()
    assert gen_ui._setup_done is False

    async with gen_ui.lifespan():
        assert gen_ui._setup_done is True
        tools = {tool.name for tool in await gen_ui._local.list_tools()}

    assert "generate_prefab_ui" in tools


@pytest.mark.asyncio
async def test_lifespan_stops_its_own_sandbox_on_exit():
    gen_ui = NextDNSGenerativeUI()
    sandbox = MagicMock(name="sandbox")
    gen_ui._sandbox = sandbox

    async with gen_ui.lifespan():
        pass

    sandbox._stop.assert_called_once()
    assert gen_ui._sandbox is None


@pytest.mark.asyncio
async def test_lifespan_is_a_noop_when_no_sandbox_was_started():
    gen_ui = NextDNSGenerativeUI()

    async with gen_ui.lifespan():
        pass

    assert gen_ui._sandbox is None


@pytest.mark.asyncio
async def test_a_sibling_servers_shutdown_leaves_this_servers_sandbox_running():
    """One server's shutdown stops only its own subprocess (issue #542).

    The process-global registry this replaces emptied `_ACTIVE_GENERATIVE_UI`,
    so server B starting and finishing while server A was live killed A's
    sandbox mid-read.
    """
    host_a = NextDNSGenerativeUI()
    host_b = NextDNSGenerativeUI()
    sandbox_a = MagicMock(name="sandbox-a")
    sandbox_b = MagicMock(name="sandbox-b")
    host_a._sandbox = sandbox_a
    host_b._sandbox = sandbox_b

    async with host_a.lifespan():
        # Server B starts and finishes WHILE server A is live:
        async with host_b.lifespan():
            pass
        sandbox_b._stop.assert_called_once()
        sandbox_a._stop.assert_not_called()
        assert host_b._sandbox is None
        assert host_a._sandbox is sandbox_a

    # A's own shutdown reclaims its own subprocess:
    sandbox_a._stop.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_stops_the_sandbox_off_the_event_loop():
    """`Sandbox._stop()` waits up to 5s on the subprocess, so it is not run on the loop."""
    stopped_on: list[threading.Thread] = []

    def blocking_stop() -> None:
        stopped_on.append(threading.current_thread())

    gen_ui = NextDNSGenerativeUI()
    sandbox = MagicMock(name="sandbox")
    sandbox._stop.side_effect = blocking_stop
    gen_ui._sandbox = sandbox

    async with gen_ui.lifespan():
        pass

    assert len(stopped_on) == 1
    assert stopped_on[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_lifespan_teardown_survives_a_failing_stop(caplog):
    """An unreapable subprocess is logged, and never aborts the rest of shutdown."""

    def exploding_stop() -> None:
        raise RuntimeError("terminate failed")

    gen_ui = NextDNSGenerativeUI()
    sandbox = MagicMock(name="sandbox")
    sandbox._stop.side_effect = exploding_stop
    gen_ui._sandbox = sandbox

    async with gen_ui.lifespan():
        pass

    assert gen_ui._sandbox is None
    assert "Failed to stop the Generative UI sandbox" in caplog.text


def test_openapi_lifespan_has_no_generative_dependency():
    """Core `openapi.py` must not reach back into the optional `generative` module."""
    source = inspect.getsource(openapi_module)
    assert "close_all_generative_ui" not in source
    assert "from .generative" not in source
    assert not hasattr(generative_module, "close_all_generative_ui")


@pytest.mark.asyncio
async def test_a_real_server_shutdown_stops_the_providers_sandbox():
    """FastMCP enters provider lifespans, so server teardown reaches this teardown.

    Without the override nothing in the shipped server stops the subprocess
    (the ~4 GB leak of issue #408).
    """
    server = FastMCP("NextDNS MCP Test")
    gen_ui = NextDNSGenerativeUI()
    server.add_provider(gen_ui)
    sandbox = MagicMock(name="sandbox")
    gen_ui._sandbox = sandbox

    async with server._lifespan_manager():
        pass

    sandbox._stop.assert_called_once()
    assert gen_ui._sandbox is None


@pytest.mark.asyncio
async def test_generate_ui_code_size_limit():
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]
    oversize = "a" * (MAX_CODE_BYTES + 1)
    with pytest.raises(ValueError, match="code exceeds maximum size"):
        await gen_tool.fn(code=oversize)


@pytest.mark.asyncio
async def test_generate_ui_oversized_code_skips_ast_parsing():
    """Oversized code is rejected by the byte guard before ast.parse runs."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]
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
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]
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
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]
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

    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]

    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=MagicMock(start=AsyncMock())),
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
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]
    oversize_data = "b" * (MAX_DATA_BYTES + 1)
    with pytest.raises(ValueError, match="data exceeds maximum size"):
        await gen_tool.fn(code="print(1)", data=oversize_data)


@pytest.mark.asyncio
async def test_generate_ui_result_size_limit():
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]

    class OversizedApp:
        def to_json(self):
            return {"payload": "x" * (MAX_RESULT_BYTES + 1)}

    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=MagicMock(start=AsyncMock())),
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
    from nextdns_mcp.generative_sandbox import _BoundedStdout

    line = b'{"id": "abc", "result": {"view": {}}}\n'
    raw = _ChunkedRaw(line + b'{"id": "def"}\n', chunk_size=7)
    bounded = _BoundedStdout(raw, limit=1024, on_overflow=MagicMock())
    assert bounded.readline() == line
    # Bytes past the newline belong to the next response.
    assert bounded.readline() == b'{"id": "def"}\n'
    assert bounded.readline() == b""


def test_bounded_stdout_readline_raises_past_limit_and_stops_sandbox():
    from nextdns_mcp.generative_sandbox import _BoundedStdout

    overflow = MagicMock()
    raw = _ChunkedRaw(b"x" * 100 + b"\n", chunk_size=16)
    bounded = _BoundedStdout(raw, limit=32, on_overflow=overflow)
    with pytest.raises(ValueError, match="result exceeds maximum size"):
        bounded.readline()
    overflow.assert_called_once_with()


def test_bounded_stdout_readline_rejects_newline_chunk_past_limit():
    """A line whose newline arrives in the chunk that crosses the limit is still rejected."""
    from nextdns_mcp.generative_sandbox import _BoundedStdout

    overflow = MagicMock()
    raw = _ChunkedRaw(b"x" * 100 + b"\n", chunk_size=128)
    bounded = _BoundedStdout(raw, limit=32, on_overflow=overflow)
    with pytest.raises(ValueError, match="result exceeds maximum size"):
        bounded.readline()
    overflow.assert_called_once_with()


def test_get_sandbox_returns_bounded_result_sandbox():
    """The wiring that installs the fix: generate_ui must use the bounded sandbox."""
    from nextdns_mcp.generative_sandbox import _BoundedResultSandbox

    gen_ui = NextDNSGenerativeUI()
    sandbox = gen_ui._get_sandbox()
    assert isinstance(sandbox, _BoundedResultSandbox)
    assert gen_ui._get_sandbox() is sandbox


def test_bounded_stdout_readline_returns_partial_line_at_eof():
    from nextdns_mcp.generative_sandbox import _BoundedStdout

    raw = _ChunkedRaw(b'{"id": "abc"}', chunk_size=4)
    bounded = _BoundedStdout(raw, limit=1024, on_overflow=MagicMock())
    assert bounded.readline() == b'{"id": "abc"}'
    assert bounded.readline() == b""


def test_bounded_result_sandbox_wraps_stdout_after_start():
    from nextdns_mcp import generative_sandbox
    from nextdns_mcp.generative_sandbox import _BoundedResultSandbox, _BoundedStdout

    sandbox = _BoundedResultSandbox()
    proc = MagicMock()
    proc.stdout = MagicMock()
    sandbox._process = proc
    # Drive the real _BoundedResultSandbox._start with a stubbed base start.
    original_start = generative_sandbox._Sandbox._start

    async def fake_start(self):
        self._process = proc

    try:
        generative_sandbox._Sandbox._start = fake_start  # type: ignore[assignment]
        asyncio.run(sandbox._start())
        assert isinstance(proc.stdout, _BoundedStdout)
        wrapper = proc.stdout
        # Re-entering the start path over a live worker must not double-wrap.
        asyncio.run(sandbox._start())
        assert proc.stdout is wrapper
    finally:
        generative_sandbox._Sandbox._start = original_start  # type: ignore[assignment]


def test_bounded_result_sandbox_start_without_process_is_safe():
    from nextdns_mcp import generative_sandbox
    from nextdns_mcp.generative_sandbox import _BoundedResultSandbox

    sandbox = _BoundedResultSandbox()
    original_start = generative_sandbox._Sandbox._start

    async def fake_start(self):
        self._process = None

    try:
        generative_sandbox._Sandbox._start = fake_start  # type: ignore[assignment]
        asyncio.run(sandbox._start())  # must not raise
    finally:
        generative_sandbox._Sandbox._start = original_start  # type: ignore[assignment]
    assert sandbox._process is None


def test_result_size_bytes_model_dump_json():
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
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    gen_tool = {t.name: t for t in tool_list}["generate_prefab_ui"]
    sandbox_mock = MagicMock(start=AsyncMock())
    cancelled = {"seen": False}

    async def never_resolves(*args, **kwargs):
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=sandbox_mock),
    ):
        mock_exec.side_effect = never_resolves
        with pytest.raises(TimeoutError, match="execution exceeded"):
            await gen_tool.fn(code="print(1)")
        assert cancelled["seen"]


# --- Sandbox hardening (issue #541) ---


def test_sandbox_env_strips_host_secrets(monkeypatch):
    """Host variables outside the allowlist never reach the Deno subprocess.

    The sanitizer is an allowlist, not a name-substring blocklist, so
    credential-bearing variables with unmarked names are dropped too.
    """
    from nextdns_mcp.generative_sandbox import _sandbox_env

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
    env = _sandbox_env()
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
    env = _sandbox_env()
    assert env["DENO_DIR"] == "/tmp/deno-cache"
    assert env["DENO_NO_UPDATE_CHECK"] == "1"


def test_sandbox_env_is_a_copy(monkeypatch):
    """Mutating the returned dict must not touch the host environment."""
    from nextdns_mcp.generative_sandbox import _sandbox_env

    env = _sandbox_env()
    before = dict(os.environ)
    env["INJECTED"] = "x"
    assert os.environ.get("INJECTED") is None
    assert dict(os.environ) == before


def test_sandbox_startup_env_bounds_v8_heap(monkeypatch):
    """The worker heap is bounded well below the upstream 4 GiB ceiling."""
    from nextdns_mcp.generative_sandbox import SANDBOX_V8_HEAP_MB, _sandbox_startup_env

    monkeypatch.setenv("NEXTDNS_API_KEY", "super-secret-key")
    monkeypatch.setenv("DENO_V8_FLAGS", "--max-old-space-size=8192")
    env = _sandbox_startup_env()
    assert env["DENO_V8_FLAGS"] == f"--max-old-space-size={SANDBOX_V8_HEAP_MB}"
    assert SANDBOX_V8_HEAP_MB < 4096
    assert "NEXTDNS_API_KEY" not in env


@pytest.mark.asyncio
async def test_hardened_sandbox_start_spawns_with_sanitized_env(monkeypatch):
    """_start forwards a sanitized environment with a bounded heap to the Deno worker."""
    from nextdns_mcp import generative_sandbox

    monkeypatch.setenv("NEXTDNS_API_KEY", "super-secret-key")
    captured_envs = []

    class _FakeProc:
        pass

    def fake_spawn(env):
        captured_envs.append(env)
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        future.set_result(_FakeProc())
        return future

    monkeypatch.setattr(generative_sandbox, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(generative_sandbox, "_await_ready", AsyncMock())

    sandbox = generative_sandbox.HardenedPyodideSandbox()
    await sandbox._start()

    assert len(captured_envs) == 1
    env = captured_envs[0]
    assert "NEXTDNS_API_KEY" not in env
    assert env["DENO_V8_FLAGS"] == f"--max-old-space-size={generative_sandbox.SANDBOX_V8_HEAP_MB}"
    # The interpreter is not marked alive until readiness is confirmed.
    assert sandbox._process is not None


@pytest.mark.asyncio
async def test_hardened_sandbox_discards_worker_after_run():
    """Each execution runs on a fresh interpreter: the worker is stopped afterwards."""
    from nextdns_mcp.generative_sandbox import HardenedPyodideSandbox

    sandbox = HardenedPyodideSandbox()
    stop_calls = []
    monkeypatch_stop = MagicMock(side_effect=lambda: stop_calls.append(1))
    sandbox._stop = monkeypatch_stop  # type: ignore[method-assign]
    with patch.object(
        HardenedPyodideSandbox.__bases__[0], "run", new_callable=AsyncMock, return_value={"ok": True}
    ) as base_run:
        result = await sandbox.run(code="print(1)")
    assert result == {"ok": True}
    base_run.assert_awaited_once()
    assert stop_calls == [1]


@pytest.mark.asyncio
async def test_hardened_sandbox_run_stops_worker_even_on_error():
    """A failing execution still discards the interpreter."""
    from nextdns_mcp.generative_sandbox import HardenedPyodideSandbox

    sandbox = HardenedPyodideSandbox()
    sandbox._stop = MagicMock()  # type: ignore[method-assign]
    with (
        patch.object(
            HardenedPyodideSandbox.__bases__[0], "run", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await sandbox.run(code="print(1)")
    sandbox._stop.assert_called_once()


@pytest.mark.asyncio
async def test_hardened_sandbox_start_is_lazy_and_idempotent():
    """start() cold-starts only when no live worker exists."""
    from nextdns_mcp.generative_sandbox import HardenedPyodideSandbox

    sandbox = HardenedPyodideSandbox()
    sandbox._start = AsyncMock()  # type: ignore[method-assign]
    await sandbox.start()
    sandbox._start.assert_awaited_once()
    # A live (freshly started) worker is not restarted.
    sandbox._process = MagicMock(poll=MagicMock(return_value=None))
    await sandbox.start()
    sandbox._start.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_sandbox_returns_hardened_instance():
    gen_ui = NextDNSGenerativeUI()
    sandbox = gen_ui._get_sandbox()
    from nextdns_mcp.generative_sandbox import HardenedPyodideSandbox

    assert isinstance(sandbox, HardenedPyodideSandbox)
    assert gen_ui._get_sandbox() is sandbox


@pytest.mark.asyncio
async def test_generate_ui_cold_start_timeout_is_distinct():
    """A slow sandbox boot raises a cold-start error and never runs user code."""
    gen_ui = NextDNSGenerativeUI()
    gen_ui._ensure_setup()
    gen_tool = {t.name: t for t in await gen_ui._local.list_tools()}["generate_prefab_ui"]

    async def never_started():
        await asyncio.sleep(9999)

    sandbox = MagicMock(start=AsyncMock(side_effect=never_started))
    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=sandbox),
    ):
        with pytest.raises(TimeoutError, match="cold start exceeded"):
            await gen_tool.fn(code="print(1)")
        mock_exec.assert_not_called()


class _FakeStderr:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    """Minimal stand-in for the Deno subprocess during startup."""

    def __init__(self, lines: list[bytes], returncode: int | None = None) -> None:
        self.stderr = _FakeStderr(lines)
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode


def test_deno_command_raises_when_deno_missing(monkeypatch):
    from nextdns_mcp import generative_sandbox

    monkeypatch.setattr(generative_sandbox.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Deno is required"):
        generative_sandbox._deno_command()


def test_discard_startup_reclaims_only_completed_workers():
    import concurrent.futures

    from prefab_ui.sandbox import PyodideSandbox

    from nextdns_mcp.generative_sandbox import _discard_startup

    stopped: list[object] = []
    fake_proc = MagicMock(name="proc")
    with patch.object(PyodideSandbox, "_stop_process", lambda p: stopped.append(p)):
        done = concurrent.futures.Future()
        done.set_result(fake_proc)
        _discard_startup(done)
        assert stopped == [fake_proc]

        cancelled = concurrent.futures.Future()
        cancelled.cancel()
        _discard_startup(cancelled)  # no-op
        assert stopped == [fake_proc]

        failed = concurrent.futures.Future()
        failed.set_exception(RuntimeError("spawn error"))
        _discard_startup(failed)  # no-op
        assert stopped == [fake_proc]


def test_startup_error_truncates_long_output():
    from nextdns_mcp.generative_sandbox import _startup_error

    short = str(_startup_error(["boom"]))
    assert short == "Pyodide sandbox failed to start:\nboom"

    long = str(_startup_error(["x" * 1500, "y" * 1500]))
    assert long.startswith("Pyodide sandbox failed to start:")
    assert "\n...\n" in long
    assert len(long) < 2200


@pytest.mark.asyncio
async def test_await_ready_consumes_lines_until_ready():
    from nextdns_mcp.generative_sandbox import _await_ready

    proc = _FakeProc([b"boot log\n", b"pyodide:ready\n"])
    await _await_ready(proc)  # returns once readiness is signalled


@pytest.mark.asyncio
async def test_await_ready_raises_when_worker_dies():
    from nextdns_mcp.generative_sandbox import _await_ready

    proc = _FakeProc([b"import error\n", b""], returncode=1)
    with pytest.raises(RuntimeError, match="Pyodide sandbox failed to start"):
        await _await_ready(proc)


@pytest.mark.asyncio
async def test_read_startup_line_passes_through_empty_lines_while_alive():
    """An empty readline with the worker still alive must not raise."""
    from nextdns_mcp.generative_sandbox import _read_startup_line

    proc = _FakeProc([b""], returncode=None)
    lines: list[str] = []
    decoded = await _read_startup_line(proc, proc.stderr, lines)  # type: ignore[arg-type]
    assert decoded == ""
    assert lines == [""]


@pytest.mark.asyncio
async def test_hardened_start_stops_worker_when_readiness_fails(monkeypatch):
    from prefab_ui.sandbox import PyodideSandbox

    from nextdns_mcp import generative_sandbox

    stopped: list[object] = []
    fake_proc = MagicMock(name="proc")

    def fake_spawn(env):
        future = asyncio.get_event_loop().create_future()
        future.set_result(fake_proc)
        return future

    monkeypatch.setattr(generative_sandbox, "_spawn_deno", fake_spawn)
    monkeypatch.setattr(generative_sandbox, "_await_ready", AsyncMock(side_effect=RuntimeError("boot failed")))
    monkeypatch.setattr(PyodideSandbox, "_stop_process", lambda p: stopped.append(p))

    sandbox = generative_sandbox.HardenedPyodideSandbox()
    with pytest.raises(RuntimeError, match="boot failed"):
        await sandbox._start()
    assert stopped == [fake_proc]
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
async def test_hardened_start_cancellation_registers_reclaim(monkeypatch):
    from nextdns_mcp import generative_sandbox

    pending = _SpyFuture()
    monkeypatch.setattr(generative_sandbox, "_spawn_deno", lambda env: pending)
    sandbox = generative_sandbox.HardenedPyodideSandbox()

    task = asyncio.ensure_future(sandbox._start())
    await asyncio.sleep(0)  # let the task reach the shield
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending.recorded_callbacks  # worker will be reclaimed if it appears
