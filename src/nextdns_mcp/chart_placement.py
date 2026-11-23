"""Static validation of chart placement in generated Prefab code.

Owns the AST walk that rejects charts nested inside a Tab subtree before the
generated code is sent to the sandbox.

SPDX-License-Identifier: MIT
"""

import ast

_CHART_NAMES = {"AreaChart", "LineChart", "BarChart", "PieChart", "DonutChart"}
_TAB_NAMES = {"Tab", "TabsTrigger"}


def _call_name(node: ast.AST) -> str | None:
    """Return the callee name for simple `Name(...)` calls, else None."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name):
        return None
    return node.func.id


def _is_tab_call(node: ast.AST) -> bool:
    return _call_name(node) in _TAB_NAMES


def _is_chart_call(node: ast.AST) -> bool:
    return _call_name(node) in _CHART_NAMES


def _call_children(node: ast.Call) -> list[ast.AST]:
    """Return a call's argument expressions, including keyword values."""
    children: list[ast.AST] = list(node.args)
    children.extend(keyword.value for keyword in node.keywords)
    return children


def _call_visits(node: ast.Call, in_tab: bool) -> list[tuple[ast.AST, bool]]:
    """Child visits for a call: a Tab(...) call opens tab scope for its arguments."""
    if _is_tab_call(node):
        return [(child, True) for child in _call_children(node)]
    return [(child, in_tab) for child in ast.iter_child_nodes(node)]


def _with_visits(node: ast.With | ast.AsyncWith, in_tab: bool) -> list[tuple[ast.AST, bool]]:
    """Child visits for a `with`: items keep the current scope; the body opens tab scope."""
    visits: list[tuple[ast.AST, bool]] = [(item.context_expr, in_tab) for item in node.items]
    body_scope = in_tab or any(_is_tab_call(item.context_expr) for item in node.items)
    visits.extend((stmt, body_scope) for stmt in node.body)
    return visits


def _node_visits(node: ast.AST, in_tab: bool) -> list[tuple[ast.AST, bool]]:
    """Return the (node, scope) pairs to visit for one node's children."""
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _with_visits(node, in_tab)
    if isinstance(node, ast.Call):
        return _call_visits(node, in_tab)
    return [(child, in_tab) for child in ast.iter_child_nodes(node)]


def _chart_in_tab(root: ast.AST) -> bool:
    """Return True if a chart call occurs inside a Tab/TabsTrigger subtree.

    Iterative stack walk: each `with` item's context expression is visited,
    each `with` body is visited once regardless of item count, and node depth
    costs no stack frames, so deeply nested expressions cannot raise
    RecursionError.
    """
    stack: list[tuple[ast.AST, bool]] = [(root, False)]
    while stack:
        node, in_tab = stack.pop()
        if in_tab and _is_chart_call(node):
            return True
        stack.extend(_node_visits(node, in_tab))
    return False


def _validate_chart_placement(code: str) -> None:
    """Reject nesting charts in tabs to avoid 0px container collapse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return
    if _chart_in_tab(tree):
        raise ValueError(
            "Recharts chart components (AreaChart, LineChart, BarChart, PieChart) cannot be placed "
            "inside Tab components because tab panels initialize with 0px width, causing charts to "
            "render completely invisible. Place the chart outside of Tabs in a Column, Row, Card, or Grid."
        )
