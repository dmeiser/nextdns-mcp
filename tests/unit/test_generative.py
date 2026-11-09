"""Tests for NextDNSGenerativeUI provider and sizing rules."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextdns_mcp.generative import (
    SANDBOX_PRELUDE,
    NextDNSGenerativeUI,
    _build_renderer_resource,
    _parse_ui_data,
    _validate_chart_placement,
)


def test_parse_ui_data():
    assert _parse_ui_data(None) is None
    assert _parse_ui_data("") is None
    assert _parse_ui_data("   ") is None
    assert _parse_ui_data('{"key": "value"}') == {"key": "value"}
    assert _parse_ui_data({"key": "value"}) == {"key": "value"}


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
    assert gen_tool.annotations.read_only_hint is True
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
async def test_nextdns_generative_ui_without_components_tool():
    gen_ui = NextDNSGenerativeUI(include_components_tool=False)
    gen_ui._ensure_setup()
    tool_list = await gen_ui._local.list_tools()
    tools = {t.name: t for t in tool_list}
    assert "generate_prefab_ui" in tools
    assert "search_prefab_components" not in tools
