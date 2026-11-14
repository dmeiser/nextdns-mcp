"""Tests for NextDNSGenerativeUI provider and sizing rules."""

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
