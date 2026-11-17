"""Tests for NextDNSGenerativeUI provider and sizing rules."""

import ast
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextdns_mcp.generative import (
    MAX_CODE_BYTES,
    MAX_DATA_BYTES,
    MAX_RESULT_BYTES,
    SANDBOX_PRELUDE,
    NextDNSGenerativeUI,
    _build_renderer_resource,
    _keyword_value_in_tab,
    _parse_ui_data,
    _result_size_bytes,
    _validate_chart_placement,
)


def test_parse_ui_data():
    assert _parse_ui_data(None) is None
    assert _parse_ui_data("") is None
    assert _parse_ui_data("   ") is None
    assert _parse_ui_data('{"key": "value"}') == {"key": "value"}
    assert _parse_ui_data({"key": "value"}) == {"key": "value"}


def test_parse_ui_data_rejects_malformed_and_non_dict():
    import pytest

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


def test_repeated_tab_blocks_scale_subquadratically():
    """Many chart-free Tab blocks must validate in sub-quadratic time (DoS regression)."""
    small = "".join(f"with Tab('T{i}'):\n    pass\n" for i in range(400))
    large = "".join(f"with Tab('T{i}'):\n    pass\n" for i in range(1600))
    start = time.perf_counter()
    _validate_chart_placement(small)  # should not raise
    small_elapsed = time.perf_counter() - start
    start = time.perf_counter()
    _validate_chart_placement(large)  # should not raise
    large_elapsed = time.perf_counter() - start
    assert large_elapsed < 10
    assert large_elapsed / max(small_elapsed, 1e-6) < 10


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


def test_none_keyword_value_is_not_a_chart():
    """A keyword argument with no value cannot hold a chart."""
    assert _keyword_value_in_tab(ast.keyword(arg=None, value=None)) is False


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
    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=MagicMock()),
    ):
        mock_exec.return_value = "fake_app"
        res = await gen_tool.fn(code="print('hi')", data='{"a": 1}')
        assert res == "fake_app"
        assert mock_exec.call_count == 1
        call_code = mock_exec.call_args[0][0]
        assert SANDBOX_PRELUDE in call_code
        assert "print('hi')" in call_code
        assert mock_exec.call_args[1]["data"] == {"a": 1}


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


def test_close_calls_stop_when_sandbox_initialized():
    gen_ui = NextDNSGenerativeUI()
    sandbox = MagicMock()
    gen_ui._sandbox = sandbox
    gen_ui.close()
    sandbox._stop.assert_called_once()
    assert gen_ui._sandbox is None


def test_close_noop_when_not_initialized():
    gen_ui = NextDNSGenerativeUI()
    gen_ui._sandbox = None
    gen_ui.close()  # should not raise
    assert gen_ui._sandbox is None


def test_close_all_generative_ui():
    from nextdns_mcp.generative import close_all_generative_ui

    gen_ui = NextDNSGenerativeUI()
    sandbox = MagicMock()
    gen_ui._sandbox = sandbox
    close_all_generative_ui()
    sandbox._stop.assert_called_once()


@pytest.mark.asyncio
async def test_server_lifespan_cleanup():
    from unittest.mock import patch

    from nextdns_mcp.openapi import _server_lifespan

    with patch("nextdns_mcp.generative.close_all_generative_ui") as mock_close:
        async with _server_lifespan(MagicMock()):
            pass
        mock_close.assert_called_once()


def test_close_all_generative_ui_handles_exception():
    from nextdns_mcp.generative import close_all_generative_ui

    gen_ui = NextDNSGenerativeUI()
    # Make close raise
    gen_ui.close = MagicMock(side_effect=RuntimeError("boom"))
    # Should not propagate
    close_all_generative_ui()


@pytest.mark.asyncio
async def test_server_lifespan_cleanup_handles_exception():
    from unittest.mock import patch

    from nextdns_mcp.openapi import _server_lifespan

    with patch("nextdns_mcp.generative.close_all_generative_ui", side_effect=RuntimeError("boom")):
        # Should not raise
        async with _server_lifespan(MagicMock()):
            pass


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
        patch.object(gen_ui, "_get_sandbox", return_value=MagicMock()),
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
    from nextdns_mcp.generative import _BoundedStdout

    line = b'{"id": "abc", "result": {"view": {}}}\n'
    raw = _ChunkedRaw(line + b'{"id": "def"}\n', chunk_size=7)
    bounded = _BoundedStdout(raw, limit=1024, on_overflow=MagicMock())
    assert bounded.readline() == line
    # Bytes past the newline belong to the next response.
    assert bounded.readline() == b'{"id": "def"}\n'
    assert bounded.readline() == b""


def test_bounded_stdout_readline_raises_past_limit_and_stops_sandbox():
    from nextdns_mcp.generative import _BoundedStdout

    overflow = MagicMock()
    raw = _ChunkedRaw(b"x" * 100 + b"\n", chunk_size=16)
    bounded = _BoundedStdout(raw, limit=32, on_overflow=overflow)
    with pytest.raises(ValueError, match="result exceeds maximum size"):
        bounded.readline()
    overflow.assert_called_once_with()


def test_bounded_stdout_readline_returns_partial_line_at_eof():
    from nextdns_mcp.generative import _BoundedStdout

    raw = _ChunkedRaw(b'{"id": "abc"}', chunk_size=4)
    bounded = _BoundedStdout(raw, limit=1024, on_overflow=MagicMock())
    assert bounded.readline() == b'{"id": "abc"}'
    assert bounded.readline() == b""


def test_bounded_result_sandbox_wraps_stdout_after_start():
    from nextdns_mcp import generative
    from nextdns_mcp.generative import _BoundedResultSandbox, _BoundedStdout

    sandbox = _BoundedResultSandbox()
    proc = MagicMock()
    proc.stdout = MagicMock()
    sandbox._process = proc
    # Drive the real _BoundedResultSandbox._start with a stubbed base start.
    original_start = generative._Sandbox._start

    async def fake_start(self):
        self._process = proc

    try:
        generative._Sandbox._start = fake_start  # type: ignore[assignment]
        asyncio.run(sandbox._start())
        assert isinstance(proc.stdout, _BoundedStdout)
        wrapper = proc.stdout
        # Re-entering the start path over a live worker must not double-wrap.
        asyncio.run(sandbox._start())
        assert proc.stdout is wrapper
    finally:
        generative._Sandbox._start = original_start  # type: ignore[assignment]


def test_bounded_result_sandbox_start_without_process_is_safe():
    from nextdns_mcp import generative
    from nextdns_mcp.generative import _BoundedResultSandbox

    sandbox = _BoundedResultSandbox()
    original_start = generative._Sandbox._start

    async def fake_start(self):
        self._process = None

    try:
        generative._Sandbox._start = fake_start  # type: ignore[assignment]
        asyncio.run(sandbox._start())  # must not raise
    finally:
        generative._Sandbox._start = original_start  # type: ignore[assignment]
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
    sandbox_mock = MagicMock()

    async def never_resolves(*args, **kwargs):
        await asyncio.sleep(9999)

    with (
        patch("fastmcp.apps.generative._gen.execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_get_sandbox", return_value=sandbox_mock),
    ):
        mock_exec.side_effect = never_resolves
        with pytest.raises(TimeoutError, match="execution exceeded"):
            await gen_tool.fn(code="print(1)")
        # sandbox reset attempted
        # at least one reset method may have been accessed
