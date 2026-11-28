"""Tests for PrefabGenerativeUI provider, chart placement, and sizing rules.

Covers UI-data parsing, chart placement validation, the sandbox prelude,
renderer resources, the provider's published tools and lifespan, and the
generate_ui size limits and typed errors. Generated-tree validation lives in
``test_generative_tree_validation.py`` and the owned Deno sandbox in
``test_generative_sandbox.py``.
"""

import ast
import inspect
import json
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from prefab_ui.app import PrefabApp

from nextdns_mcp import chart_placement as chart_placement_module
from nextdns_mcp import generative as generative_module
from nextdns_mcp import generative_sandbox as sandbox_module
from nextdns_mcp import lifespan as lifespan_module
from nextdns_mcp.chart_placement import validate_chart_placement
from nextdns_mcp.generative import (
    MAX_CODE_BYTES,
    MAX_DATA_BYTES,
    MAX_JSON_DEPTH,
    SANDBOX_PRELUDE,
    PrefabGenerativeUI,
    _build_renderer_resource,
    _parse_ui_data,
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


def _nested_containers(levels: int) -> dict[str, Any]:
    """A document with exactly ``levels`` container levels: the root dict plus nested lists."""
    value: Any = 1
    for _ in range(levels - 1):
        value = [value]
    return {"a": value}


def test_parse_ui_data_rejects_non_finite_numbers_in_json_text():
    """NaN and Infinity decode in Python but fail JSON.parse in the worker (issue #764)."""
    for payload in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
        with pytest.raises(ValueError, match="not a valid JSON number"):
            _parse_ui_data(payload)


def test_parse_ui_data_rejects_non_finite_numbers_in_a_dict():
    """A dict the host passed never went through a parser, so its floats are walked too."""
    for payload in (
        {"a": float("nan")},
        {"a": float("inf")},
        {"a": float("-inf")},
        {"a": [{"b": [float("nan")]}]},
    ):
        with pytest.raises(ValueError, match="NaN or Infinity"):
            _parse_ui_data(payload)


def test_parse_ui_data_accepts_finite_numbers():
    assert _parse_ui_data('{"a": -1.5e308, "b": 0}') == {"a": -1.5e308, "b": 0}
    assert _parse_ui_data({"a": 1.5e-308}) == {"a": 1.5e-308}


def test_parse_ui_data_rejects_nesting_past_the_cap():
    with pytest.raises(ValueError, match="maximum nesting depth"):
        _parse_ui_data(_nested_containers(MAX_JSON_DEPTH))
    with pytest.raises(ValueError, match="maximum nesting depth"):
        _parse_ui_data('{"a": ' + "[" * (MAX_JSON_DEPTH + 5) + "]" * (MAX_JSON_DEPTH + 5) + "}")


def test_parse_ui_data_accepts_nesting_at_the_cap():
    assert _parse_ui_data(_nested_containers(MAX_JSON_DEPTH - 1)) == _nested_containers(MAX_JSON_DEPTH - 1)


def test_parse_ui_data_rejects_cycles_in_a_dict():
    """A branching cycle is refused on the first descent, not queued generation by generation."""
    root: dict[str, Any] = {}
    for key in "abcdefghij":
        branch: dict[str, Any] = {}
        for sub in "abcdefghij":
            branch[sub] = root
        root[key] = branch

    start = time.perf_counter()
    with pytest.raises(ValueError, match="circular reference"):
        _parse_ui_data(root)
    assert time.perf_counter() - start < 1

    self_cycle: dict[str, Any] = {}
    self_cycle["self"] = self_cycle
    with pytest.raises(ValueError, match="circular reference"):
        _parse_ui_data(self_cycle)

    holder: list[Any] = []
    holder.append((holder,))
    with pytest.raises(ValueError, match="circular reference"):
        _parse_ui_data({"a": holder})


def test_parse_ui_data_accepts_shared_subobjects():
    """An object reachable through two sibling branches is not a cycle."""
    shared = {"x": [1, 2]}
    assert _parse_ui_data({"a": shared, "b": [shared]}) == {"a": {"x": [1, 2]}, "b": [{"x": [1, 2]}]}


def test_parse_ui_data_handles_tuples():
    """json.dumps encodes a tuple as an array, so tuple chains get the same depth bound."""
    assert _parse_ui_data({"a": (1, "two")}) == {"a": (1, "two")}
    value: Any = 1
    for _ in range(MAX_JSON_DEPTH + 1):
        value = (value,)
    with pytest.raises(ValueError, match="maximum nesting depth"):
        _parse_ui_data({"a": value})


def test_decode_ui_json_reports_a_decoder_stack_blowout_as_a_validation_error():
    """A decoder stack blowout is reported as a validation error, not leaked as RecursionError.

    The exact depth at which a C scanner runs out of stack depends on the
    interpreter's stack size, so force the exception rather than tying the test
    to a document length that only fails on some runners.
    """
    with (
        patch("nextdns_mcp.generative.json.loads", side_effect=RecursionError("maximum recursion depth")),
        pytest.raises(ValueError, match="maximum nesting depth") as raised,
    ):
        generative_module._decode_ui_json('{"a": 1}')
    assert isinstance(raised.value.__cause__, RecursionError)


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
            validate_chart_placement(code)


def test_validate_chart_placement_accepts_valid_code():
    good_codes = [
        "AreaChart(data=[], series=[])\nwith Tabs():\n    with Tab('Data'):\n        DataTable()",
        "with Column():\n    PieChart(data=[])",
    ]
    for code in good_codes:
        validate_chart_placement(code)  # should not raise


def test_chart_after_tab_block_is_accepted():
    """A dedented chart after a Tab block is not nested (false-rejection regression)."""
    code = "with Tab('Data'):\n    DataTable(columns=[], rows=[])\nAreaChart(data=[], series=[])\n"
    validate_chart_placement(code)  # should not raise


def test_chart_in_card_after_tab_block_is_accepted():
    """A chart in a Card following a Tab block is not nested (false-rejection regression)."""
    code = "with Tab('Data'):\n    DataTable(columns=[], rows=[])\nwith Card():\n    AreaChart(data=[], series=[])\n"
    validate_chart_placement(code)  # should not raise


def test_chart_before_tab_block_is_accepted():
    """A chart placed before any Tab block is not nested."""
    code = "AreaChart(data=[], series=[])\nwith Tab('Data'):\n    DataTable(columns=[], rows=[])\n"
    validate_chart_placement(code)  # should not raise


def test_multiple_tab_blocks_without_charts_are_accepted():
    """Several chart-free Tab blocks must not trip the validator."""
    code = "".join(f"with Tab('T{i}'):\n    DataTable(columns=[], rows=[])\n" for i in range(5))
    validate_chart_placement(code)  # should not raise


def test_with_tabstrigger_chart_is_rejected():
    """A hallucinated TabsTrigger keeps its charts nested even though the prelude no longer aliases it."""
    code = "with TabsTrigger('Charts'):\n    AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_placement_error_names_every_rejected_tab_form():
    """The diagnostic names the tab form it rejected, not only ``Tab`` (WORK-386)."""
    code = "with TabsTrigger('Charts'):\n    DonutChart(data=[])\n"
    with pytest.raises(ValueError) as excinfo:
        validate_chart_placement(code)
    message = str(excinfo.value)
    assert "DonutChart" in message
    assert "TabsTrigger" in message
    # The chart list stays derived from CHART_COMPONENT_NAMES.
    assert all(name in message for name in chart_placement_module.CHART_COMPONENT_NAMES)


def test_tab_functional_children_chart_is_rejected():
    """Functional Tab(children=[...]) nesting bypasses `with` matching."""
    code = "Tab(children=[AreaChart(data=[], series=[])])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_tabstrigger_functional_children_chart_is_rejected():
    """Functional TabsTrigger(children=[...]) nesting is nested too."""
    code = "TabsTrigger(value='a', children=[LineChart(data=[], series=[])])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_repeated_tab_blocks_scale_subquadratically(monkeypatch):
    """Many chart-free Tab blocks must validate in linear time (DoS regression).

    Wall-clock ratios move with machine load, so the walk's node examinations
    are counted instead and the scaling claim becomes an exact integer check.
    """

    def count_examinations(blocks: int) -> int:
        examinations = {"n": 0}
        original = chart_placement_module._node_visits

        def counting(node, in_tab, ctx):
            examinations["n"] += 1
            return original(node, in_tab, ctx)

        monkeypatch.setattr(chart_placement_module, "_node_visits", counting)
        code = "".join(f"with Tab('T{i}'):\n    pass\n" for i in range(blocks))
        validate_chart_placement(code)  # should not raise
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
    validate_chart_placement(code)  # should not raise


def test_attribute_tab_call_is_not_a_tab():
    """`mod.Tab(...)` is an attribute call, not the bare Tab alias — accepted."""
    code = "with mod.Tab('Charts'):\n    AreaChart(data=[], series=[])\n"
    validate_chart_placement(code)  # should not raise


def test_tab_positional_chart_arg_is_rejected():
    """A chart passed positionally to Tab(...) is still nested."""
    code = "Tab(AreaChart(data=[], series=[]))\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_bare_tab_call_without_chart_is_accepted():
    """A chart-free Tab(...) call leaves a later top-level chart alone."""
    code = "Tab('Data')\nAreaChart(data=[], series=[])\n"
    validate_chart_placement(code)  # should not raise


def test_all_chart_types_in_tab_are_rejected():
    """Every Recharts chart type exported by prefab_ui is rejected inside a Tab."""
    for chart in (
        "AreaChart",
        "LineChart",
        "BarChart",
        "PieChart",
        "DonutChart",
        "RadarChart",
        "RadialChart",
        "ScatterChart",
        "WaterfallChart",
    ):
        code = f"with Tab('Charts'):\n    {chart}(data=[], series=[])\n"
        with pytest.raises(ValueError, match="Recharts chart components"):
            validate_chart_placement(code)


def test_chart_parent_tab_call_is_rejected():
    """A chart attaching to a Tab via parent= is nested even outside a with block."""
    code = "AreaChart(data=[], series=[], parent=Tab('Charts'))\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_parent_tab_binding_is_rejected():
    """parent= referencing a name bound to a Tab call earlier is nested."""
    code = "t = Tab('Charts')\nAreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_walrus_tab_with_scope_is_rejected():
    """`with (t := Tab(...)):` still opens tab scope for its body."""
    code = "with (t := Tab('Charts')):\n    AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_walrus_tab_binding_parent_is_rejected():
    """A walrus-bound Tab referenced by a later parent= is nested."""
    code = "with (t := Tab('Charts')):\n    DataTable(columns=[], rows=[])\nAreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_with_as_tab_binding_parent_is_rejected():
    """`with Tab(...) as t` binds t, so a later parent=t is nested."""
    code = "with Tab('Charts') as t:\n    DataTable(columns=[], rows=[])\nAreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_annotated_tab_binding_parent_is_rejected():
    """An annotated assignment to a Tab call binds the name for parent=."""
    code = "t: Component = Tab('Charts')\nAreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_walrus_chart_call_without_tab_is_accepted():
    """A walrus-bound chart outside any tab stays accepted."""
    code = "(c := AreaChart(data=[], series=[]))\n"
    validate_chart_placement(code)  # should not raise


def test_chart_parent_non_tab_binding_is_accepted():
    """parent= referencing a non-Tab container stays accepted."""
    code = "c = Card()\nAreaChart(data=[], series=[], parent=c)\n"
    validate_chart_placement(code)  # should not raise


def test_chart_parent_before_tab_binding_is_accepted():
    """parent= naming a Tab bound only later stays accepted (source-order binding)."""
    code = "AreaChart(data=[], series=[], parent=t)\nt = Tab('Charts')\n"
    validate_chart_placement(code)  # should not raise


def test_chart_in_card_attached_to_tab_is_rejected():
    """A container with parent=tab carries its children into the 0px tab panel."""
    code = "t = Tab('Charts')\nCard(parent=t, children=[LineChart(data=[], series=[])])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_in_with_card_attached_to_tab_is_rejected():
    """A `with Card(parent=tab)` body is inside the tab panel."""
    code = "t = Tab('Charts')\nwith Card(parent=t):\n    LineChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_in_card_attached_to_inline_tab_is_rejected():
    """A container's parent= need not go through a name."""
    code = "Card(parent=Tab('Charts'), children=[AreaChart(data=[], series=[])])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_in_tab_attached_container_after_children_keyword_is_rejected():
    """parent= is inspected regardless of its position among the keywords."""
    code = "t = Tab('Charts')\nCard(children=[AreaChart(data=[], series=[])], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_parent_rebound_to_container_is_accepted():
    """An earlier Tab binding does not survive a rebinding to a non-Tab."""
    code = "t = Tab('Charts')\nt = Column()\nAreaChart(data=[], series=[], parent=t)\n"
    validate_chart_placement(code)  # should not raise


def test_chart_parent_parameter_shadows_tab_binding_is_accepted():
    """A same-named parameter in another function is not the earlier Tab binding."""
    code = "def build():\n    t = Tab('Charts')\n    return t\ndef panel(t):\n    return AreaChart(data=[], series=[], parent=t)\n"
    validate_chart_placement(code)  # should not raise


def test_chart_parent_outer_tab_binding_in_function_is_rejected():
    """A function body that closes over a Tab name still nests the chart."""
    code = "t = Tab('Charts')\ndef panel():\n    return AreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_parent_never_bound_name_is_accepted():
    """A parent= name with no binding anywhere resolves to no Tab."""
    code = "AreaChart(data=[], series=[], parent=unknown)\n"
    validate_chart_placement(code)  # should not raise


def test_method_parent_skips_class_scope_and_sees_enclosing_tab():
    """Methods do not close over class-body names; the enclosing module Tab applies."""
    code = "t = Tab('Charts')\nclass Panel:\n    t = Column()\n    def build(self):\n        return AreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_parent_attribute_is_accepted():
    """Only bare names get binding lookups; an attribute is not a known Tab binding."""
    code = "AreaChart(data=[], series=[], parent=container.tab)\n"
    validate_chart_placement(code)  # should not raise


def test_chart_parent_loop_target_is_accepted():
    """A loop target rebinds the name to an unknown value, not the earlier Tab."""
    code = "t = Tab('Charts')\nfor t in rows:\n    AreaChart(data=[], series=[], parent=t)\n"
    validate_chart_placement(code)  # should not raise


def test_module_chart_parent_is_not_overridden_by_local_in_uncalled_function():
    """A local Tab in an uncalled function does not change a module-level Column."""
    code = "t = Column()\ndef unused():\n    t = Tab('Charts')\n    return t\nAreaChart(data=[], series=[], parent=t)\n"
    validate_chart_placement(code)  # should not raise


def test_module_tab_parent_is_not_shadowed_by_parameter_in_uncalled_function():
    """A parameter in an uncalled function does not hide a module-level Tab."""
    code = "t = Tab('Charts')\ndef unused(t):\n    return t\nAreaChart(data=[], series=[], parent=t)\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_nested_function_closes_over_tab_binding():
    """A real closure still resolves the enclosing scope's Tab binding."""
    code = "def outer():\n    t = Tab('Charts')\n    def inner():\n        return AreaChart(data=[], series=[], parent=t)\n    return inner\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_chart_parent_comprehension_target_is_accepted():
    """A comprehension target rebinds the name to an unknown value."""
    code = "t = Tab('Charts')\n[(AreaChart(data=[], series=[], parent=t)) for t in rows]\n"
    validate_chart_placement(code)  # should not raise


def test_nested_tabs_with_chart_is_rejected():
    """A chart in a Tab inside a Tab is still nested."""
    code = "with Tab('a'):\n    with Tab('b'):\n        AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_syntax_error_is_ignored():
    """Unparseable input cannot nest anything, so it is accepted."""
    validate_chart_placement("with Tab('x':\n")  # should not raise


def test_star_kwargs_are_walked_without_a_chart():
    """`Tab(**config)` is a keyword with arg=None — the **-unpacked Name must be visited."""
    validate_chart_placement("Tab(**config)\n")  # should not raise
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement("Tab(children=[AreaChart(data=[], series=[])], **config)\n")


def test_with_context_expr_chart_is_rejected():
    """`with Tab(children=[...])` nests the chart in the context expression, which was never walked."""
    code = "with Tab(children=[AreaChart(data=[], series=[])]):\n    pass\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_nested_with_context_expr_chart_is_rejected():
    """A Card(...) context expression inside a Tab block carries the chart through."""
    code = "with Tab('Charts'):\n    with Card(children=[AreaChart(data=[], series=[])]):\n        pass\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_async_with_context_expr_chart_is_rejected():
    """`async with` shares the same traversal as `with`."""
    code = "async def render():\n    async with Tab(children=[LineChart(data=[], series=[])]):\n        pass\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_multi_item_with_body_opens_tab_scope():
    """Tab need not be the first item of a multi-item `with` for the body to be in scope."""
    code = "with Card(), Tab('Charts'):\n    AreaChart(data=[], series=[])\n"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(code)


def test_multi_item_with_scales_linearly_in_items():
    """One `with` with many items visits its body once, not once per item (quadratic regression)."""

    def with_code(items: int) -> str:
        head = ", ".join(f"Tab('T{i}')" for i in range(items))
        body = "".join(f"    v{i} = " + " + ".join(str(j) for j in range(60)) + "\n" for i in range(120))
        return f"with {head}:\n{body}"

    start = time.perf_counter()
    validate_chart_placement(with_code(50))  # should not raise
    small_elapsed = time.perf_counter() - start
    start = time.perf_counter()
    validate_chart_placement(with_code(800))  # should not raise
    large_elapsed = time.perf_counter() - start
    assert large_elapsed < 10
    assert large_elapsed / max(small_elapsed, 1e-6) < 10


def test_deeply_nested_expression_does_not_recurse():
    """500 chained '+' ops nest hundreds of nodes deep — the walker must not use Python recursion."""
    expr = " + ".join(str(i) for i in range(500))
    validate_chart_placement(f"with Tab('Charts'):\n    value = {expr}\n")  # should not raise


def test_deeply_nested_chart_under_tab_is_still_rejected():
    """Depth must not hide a chart: list literals nested 150 deep keep tripping the rule."""
    deep = "[AreaChart(data=[], series=[])]"
    for _ in range(150):
        deep = f"[{deep}]"
    with pytest.raises(ValueError, match="Recharts chart components"):
        validate_chart_placement(f"Tab(children={deep})\n")


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


def test_chart_names_come_from_one_shared_constant():
    """Validation and model guidance read one ``CHART_COMPONENT_NAMES`` (issue #803 / DRY-4).

    The chart names were re-typed in the validator, in ``GENERATE_DOC``, and in the
    component-search note, so a chart type added to one list could be missing from
    the instructions a model writes its code from.
    """
    names = chart_placement_module.CHART_COMPONENT_NAMES
    joined = ", ".join(names)

    assert chart_placement_module._CHART_NAMES == frozenset(names)

    with pytest.raises(ValueError, match=joined):
        validate_chart_placement("with Tab('Charts'):\n    PieChart(data=[])")

    assert f"charts ({joined})" in generative_module.GENERATE_DOC
    assert f"charts ({joined})" in generative_module._CHART_PLACEMENT_NOTE


def test_build_renderer_resource():
    resource = _build_renderer_resource({"resourceDomains": ["example.com"]})
    assert resource.name == "Prefab Generative Renderer"
    assert resource.mime_type == "text/html;profile=mcp-app"


def test_renderer_csp_admits_no_external_origins():
    """The bundled renderer needs no CDN: generated Python runs in the server sandbox."""
    csp = generative_module._renderer_csp()
    assert csp.resource_domains == []
    assert csp.connect_domains == []
    assert csp.frame_domains == []
    assert csp.base_uri_domains == []


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


def _passive_app() -> PrefabApp:
    """A passive PrefabApp shaped like real execute output (from_json of a wire dict)."""
    return PrefabApp.from_json(
        {
            "view": {
                "type": "Column",
                "children": [
                    {"type": "Text", "content": "hello"},
                    {"type": "Link", "content": "docs", "href": "https://nextdns.io"},
                ],
            },
            "state": {"count": 1},
        }
    )


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
    fake_app = _passive_app()
    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=worker),
    ):
        mock_exec.return_value = fake_app
        res = await gen_tool.fn(code="print('hi')", data='{"a": 1}')
        assert res is fake_app
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


def _assert_typed_generation_error(result: Any, code: str, message_part: str) -> None:
    """The generate_prefab_ui failure contract: a flagged result with a typed payload."""
    assert result.is_error is True
    assert result.structured_content["code"] == code
    assert message_part in result.structured_content["error"]


@pytest.mark.asyncio
async def test_generate_ui_code_size_limit():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversize = "a" * (MAX_CODE_BYTES + 1)
    result = await gen_tool.fn(code=oversize)
    _assert_typed_generation_error(result, "invalid_argument", "code exceeds maximum size")


@pytest.mark.asyncio
async def test_generate_ui_oversized_code_skips_ast_parsing():
    """Oversized code is rejected by the byte guard before ast.parse runs."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversize = "a" * (MAX_CODE_BYTES + 1)
    with patch("nextdns_mcp.generative.validate_chart_placement") as mock_validate:
        result = await gen_tool.fn(code=oversize)
    _assert_typed_generation_error(result, "invalid_argument", "code exceeds maximum size")
    mock_validate.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ui_rejects_invalid_data_type_before_serialization():
    """Non-str/non-dict data raises ValueError from validation, not TypeError from json.dumps."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    for bad in (123, ["a"], True):
        with patch("nextdns_mcp.generative.json") as mock_json:
            result = await gen_tool.fn(code="print(1)", data=bad)
        _assert_typed_generation_error(
            result, "invalid_argument", "data parameter must be a dict or JSON object string"
        )
        mock_json.dumps.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ui_oversized_dict_data():
    """An oversized dict is rejected after type validation, with a single measurement serialization."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversized_dict = {"payload": "b" * (MAX_DATA_BYTES + 1)}
    with patch("nextdns_mcp.generative.json") as mock_json:
        mock_json.dumps.side_effect = json.dumps
        result = await gen_tool.fn(code="print(1)", data=oversized_dict)
    _assert_typed_generation_error(result, "invalid_argument", "data exceeds maximum size")
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
        fake_app = _passive_app()
        dumps_before_execute: list[int] = []

        async def _record_dumps(*args, **kwargs):
            dumps_before_execute.append(mock_json.dumps.call_count)
            return fake_app

        mock_exec.side_effect = _record_dumps
        res = await gen_tool.fn(code="print('hi')", data={"a": 1})
        assert res is fake_app
        assert dumps_before_execute == [1]
        assert mock_exec.call_args[1]["data"] == {"a": 1}


@pytest.mark.asyncio
async def test_generate_ui_data_size_limit():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    oversize_data = "b" * (MAX_DATA_BYTES + 1)
    result = await gen_tool.fn(code="print(1)", data=oversize_data)
    _assert_typed_generation_error(result, "invalid_argument", "data exceeds maximum size")


@pytest.mark.asyncio
async def test_generate_ui_rejects_non_finite_data_before_the_sandbox():
    """A NaN in either data form is a typed validation error, and no worker starts (issue #764)."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    for payload, message_part in (
        ('{"v": NaN}', "not a valid JSON number"),
        ({"v": float("nan")}, "NaN or Infinity"),
    ):
        with patch.object(gen_ui, "_new_worker") as mock_worker:
            result = await gen_tool.fn(code="print('hi')", data=payload)
        _assert_typed_generation_error(result, "invalid_argument", message_part)
        mock_worker.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ui_rejects_deeply_nested_data_before_the_sandbox():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    deep = "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
    with patch.object(gen_ui, "_new_worker") as mock_worker:
        result = await gen_tool.fn(code="print('hi')", data=f'{{"v": {deep}}}')
    _assert_typed_generation_error(result, "invalid_argument", "maximum nesting depth")
    mock_worker.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ui_rejects_deep_tuples_in_dict_data_before_the_sandbox():
    """The tuple path QA probed is refused by the tool, not by the worker's RecursionError."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    value: Any = 1
    for _ in range(MAX_JSON_DEPTH + 1):
        value = (value,)
    with patch.object(gen_ui, "_new_worker") as mock_worker:
        result = await gen_tool.fn(code="print('hi')", data={"v": value})
    _assert_typed_generation_error(result, "invalid_argument", "maximum nesting depth")
    mock_worker.assert_not_called()


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
        result = await gen_tool.fn(code="print(1)")
    _assert_typed_generation_error(result, "response_too_large", "result exceeds maximum size")


@pytest.mark.asyncio
async def test_generate_ui_invalid_json_data_is_a_typed_error():
    """The typed JSON error contract (issue #778): validation failures carry error+code."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    result = await gen_tool.fn(code="print(1)", data="{")
    _assert_typed_generation_error(result, "invalid_argument", "Invalid JSON in data parameter")


@pytest.mark.asyncio
async def test_generate_ui_timeout_is_a_typed_error():
    """A sandbox timeout answers the typed contract instead of raising out of the tool."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    with (
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
        patch.object(generative_module, "_start_within_budget", new=AsyncMock()),
        patch.object(
            generative_module,
            "_execute_within_budget",
            new=AsyncMock(side_effect=TimeoutError("generate_prefab_ui execution exceeded 10.0s")),
        ),
    ):
        result = await gen_tool.fn(code="print(1)")
    _assert_typed_generation_error(result, "timeout", "exceeded")


@pytest.mark.asyncio
async def test_generate_ui_mid_read_oversize_is_a_typed_error():
    """The bounded-read guard's refusal maps to response_too_large, not internal_error."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    with (
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
        patch.object(generative_module, "_start_within_budget", new=AsyncMock()),
        patch.object(
            generative_module,
            "_execute_within_budget",
            new=AsyncMock(side_effect=sandbox_module._ResultTooLarge("result exceeds maximum size of 65536 bytes")),
        ),
    ):
        result = await gen_tool.fn(code="print(1)")
    _assert_typed_generation_error(result, "response_too_large", "result exceeds maximum size")


@pytest.mark.asyncio
async def test_generate_ui_runtime_failure_is_a_typed_error(caplog):
    """A dead sandbox worker answers the typed contract instead of raising out of the tool.

    The failure is also logged: returning a payload instead of raising means
    FastMCP's own error logging no longer sees it.
    """
    import logging

    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    with (
        caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative"),
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
        patch.object(generative_module, "_start_within_budget", new=AsyncMock()),
        patch.object(
            generative_module,
            "_execute_within_budget",
            new=AsyncMock(side_effect=RuntimeError("Sandbox process died unexpectedly")),
        ),
    ):
        result = await gen_tool.fn(code="print(1)")
    _assert_typed_generation_error(result, "internal_error", "Sandbox process died unexpectedly")
    assert any(r.name == "nextdns_mcp.generative" and "execution failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_generate_ui_spawn_oserror_is_a_typed_error():
    """An OSError from the worker spawn maps to internal_error, not a bare raise."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    with (
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
        patch.object(generative_module, "_start_within_budget", new=AsyncMock()),
        patch.object(
            generative_module,
            "_execute_within_budget",
            new=AsyncMock(side_effect=PermissionError(13, "Permission denied", "/usr/local/bin/deno")),
        ),
    ):
        result = await gen_tool.fn(code="print(1)")
    _assert_typed_generation_error(result, "internal_error", "Permission denied")


@pytest.mark.asyncio
async def test_generate_ui_accepts_utf8_result_under_cap():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]

    class UnicodeApp:
        def to_json(self):
            return {"text": "é" * 50_000}

    app = UnicodeApp()
    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
    ):
        mock_exec.return_value = app
        assert await gen_tool.fn(code="print(1)") is app
