"""Static validation of chart placement in generated Prefab code.

Owns the AST walk that rejects charts nested inside a Tab subtree — whether by
`with` nesting, functional children, or `parent=` attachment — before the
generated code is sent to the sandbox.

SPDX-License-Identifier: MIT
"""

import ast
from collections.abc import Iterable

_CHART_NAMES = {
    "AreaChart",
    "LineChart",
    "BarChart",
    "PieChart",
    "DonutChart",
    "RadarChart",
    "RadialChart",
    "ScatterChart",
    "WaterfallChart",
}
_TAB_NAMES = {"Tab", "TabsTrigger"}


def _unwrap_named(node: ast.AST | None) -> ast.AST | None:
    """Unwrap walrus expressions to the value they assign."""
    while isinstance(node, ast.NamedExpr):
        node = node.value
    return node


def _call_name(node: ast.AST | None) -> str | None:
    """Return the callee name for simple `Name(...)` calls, else None."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name):
        return None
    return node.func.id


def _is_tab_call(node: ast.AST | None) -> bool:
    return _call_name(_unwrap_named(node)) in _TAB_NAMES


def _is_chart_call(node: ast.AST) -> bool:
    return _call_name(_unwrap_named(node)) in _CHART_NAMES


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


def _name_targets(targets: Iterable[ast.AST]) -> list[str]:
    """Plain-name ids among assignment targets."""
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _assign_binding(node: ast.Assign | ast.AnnAssign | ast.NamedExpr) -> list[str]:
    """Name targets of an assignment whose value is a Tab call."""
    if not _is_tab_call(node.value):
        return []
    if isinstance(node, ast.Assign):
        return _name_targets(node.targets)
    if isinstance(node, ast.NamedExpr):
        return [node.target.id]
    return _name_targets([node.target])


def _with_binding(node: ast.With | ast.AsyncWith) -> list[str]:
    """`with Tab(...) as t` binds t to the tab component."""
    return _name_targets(
        item.optional_vars for item in node.items if item.optional_vars is not None and _is_tab_call(item.context_expr)
    )


def _tab_binding_names(node: ast.AST) -> list[str]:
    """Names this node binds to a Tab component (assignment, walrus, `with ... as`)."""
    if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
        return _assign_binding(node)
    if isinstance(node, ast.With | ast.AsyncWith):
        return _with_binding(node)
    return []


def _is_tab_ref(node: ast.AST | None, tab_bindings: set[str]) -> bool:
    """True if the expression is a Tab call or a name bound to one."""
    if _is_tab_call(node):
        return True
    return isinstance(node, ast.Name) and node.id in tab_bindings


def _tab_parent_arg(node: ast.AST, tab_bindings: set[str]) -> bool:
    """True if the call attaches itself to a Tab via its parent= keyword."""
    if not isinstance(node, ast.Call):
        return False
    for keyword in node.keywords:
        if keyword.arg != "parent":
            continue
        if _is_tab_ref(_unwrap_named(keyword.value), tab_bindings):
            return True
    return False


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

    A chart is nested when it is reached under tab scope (`with Tab(...):` or
    Tab children) or when it attaches itself to a Tab via `parent=`, whether
    the Tab is passed inline or through a name bound to a Tab call earlier in
    the walk. Iterative stack walk in source order: each `with` item's context
    expression is visited, each `with` body is visited once regardless of item
    count, and node depth costs no stack frames, so deeply nested expressions
    cannot raise RecursionError.
    """
    stack: list[tuple[ast.AST, bool]] = [(root, False)]
    tab_bindings: set[str] = set()
    while stack:
        node, in_tab = stack.pop()
        tab_bindings.update(_tab_binding_names(node))
        if _is_chart_call(node) and (in_tab or _tab_parent_arg(node, tab_bindings)):
            return True
        stack.extend(reversed(_node_visits(node, in_tab)))
    return False


def _validate_chart_placement(code: str) -> None:
    """Reject nesting charts in tabs to avoid 0px container collapse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return
    if _chart_in_tab(tree):
        raise ValueError(
            "Recharts chart components (AreaChart, LineChart, BarChart, PieChart, DonutChart, "
            "RadarChart, RadialChart, ScatterChart, WaterfallChart) cannot be placed "
            "inside Tab components because tab panels initialize with 0px width, causing charts to "
            "render completely invisible. Place the chart outside of Tabs in a Column, Row, Card, or Grid."
        )
