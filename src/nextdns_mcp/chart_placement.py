"""Static validation of chart placement in generated Prefab code.

Owns the AST walk that rejects charts nested inside a Tab subtree — whether by
`with` nesting, functional children, or `parent=` attachment, directly or
through a container attached to a Tab — before the generated code is sent to
the sandbox.

SPDX-License-Identifier: MIT
"""

import ast
from bisect import bisect_left

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

_Position = tuple[int, int]
_BindingEvent = tuple[_Position, bool]  # (where the name is bound, whether it receives a Tab call)
_Bindings = dict[str, list[_BindingEvent]]


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


def _call_visits(node: ast.Call, in_tab: bool, bindings: _Bindings) -> list[tuple[ast.AST, bool]]:
    """Child visits for a call: a Tab, or a call attached to one, opens tab scope."""
    if _opens_tab_scope(node, bindings):
        return [(child, True) for child in _call_children(node)]
    return [(child, in_tab) for child in ast.iter_child_nodes(node)]


def _position(node: ast.AST) -> _Position:
    """Source position used to order a name's bindings against its uses."""
    # ast.arg carries a position at runtime but typeshed types it as a bare AST node.
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _binding_position(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> _Position:
    """A comprehension target binds from the expression's start, before its element."""
    owner = parents.get(node)
    if isinstance(owner, ast.comprehension):
        return _position(parents[owner])
    return _position(node)


def _binds_tab(parent: ast.AST | None) -> bool:
    """Whether the construct binding a name gives it a Tab call."""
    if isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr):
        return _is_tab_call(parent.value)
    if isinstance(parent, ast.withitem):
        return _is_tab_call(parent.context_expr)
    return False


def _binding_for_node(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> tuple[_Position, str, bool] | None:
    """The binding this node declares: assignment/walrus/with-as target or parameter."""
    if isinstance(node, ast.arg):
        return (_position(node), node.arg, False)
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return (_binding_position(node, parents), node.id, _binds_tab(parents.get(node)))
    return None


def _sort_events(bindings: _Bindings) -> _Bindings:
    """Order each name's bindings by source position so a lookup can bisect."""
    for bound in bindings.values():
        bound.sort()
    return bindings


def _collect_bindings(tree: ast.AST) -> _Bindings:
    """Every name binding in the module, with whether it binds a Tab call.

    One linear pass over Store-context names and parameter declarations. The
    binding closest before a use decides, so a rebinding to a non-Tab value or
    a same-named parameter ends an earlier Tab binding's reach.
    """
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    bindings: _Bindings = {}
    for node in ast.walk(tree):
        binding = _binding_for_node(node, parents)
        if binding is not None:
            bindings.setdefault(binding[1], []).append((binding[0], binding[2]))
    return _sort_events(bindings)


def _last_binding_is_tab(bound: list[_BindingEvent], used_at: _Position) -> bool:
    """Whether the binding nearest before the use is a Tab; no earlier binding means no Tab."""
    index = bisect_left(bound, (used_at, False)) - 1
    return bound[index][1] if index >= 0 else False


def _is_tab_ref(node: ast.AST | None, bindings: _Bindings) -> bool:
    """True for a Tab call, or a name whose nearest earlier binding is one."""
    if _is_tab_call(node):
        return True
    if not isinstance(node, ast.Name):
        return False
    return _last_binding_is_tab(bindings.get(node.id, []), _position(node))


def _attaches_to_tab(node: ast.AST, bindings: _Bindings) -> bool:
    """True if the call attaches itself to a Tab via its parent= keyword."""
    if not isinstance(node, ast.Call):
        return False
    for keyword in node.keywords:
        if keyword.arg == "parent" and _is_tab_ref(_unwrap_named(keyword.value), bindings):
            return True
    return False


def _opens_tab_scope(node: ast.AST, bindings: _Bindings) -> bool:
    """True when a chart inside this expression would land in a 0px tab panel."""
    return _is_tab_call(node) or _attaches_to_tab(node, bindings)


def _with_visits(node: ast.With | ast.AsyncWith, in_tab: bool, bindings: _Bindings) -> list[tuple[ast.AST, bool]]:
    """Child visits for a `with`: items keep the current scope; the body opens tab scope."""
    visits: list[tuple[ast.AST, bool]] = [(item.context_expr, in_tab) for item in node.items]
    body_scope = in_tab or any(_opens_tab_scope(item.context_expr, bindings) for item in node.items)
    visits.extend((stmt, body_scope) for stmt in node.body)
    return visits


def _node_visits(node: ast.AST, in_tab: bool, bindings: _Bindings) -> list[tuple[ast.AST, bool]]:
    """Return the (node, scope) pairs to visit for one node's children."""
    if isinstance(node, ast.With | ast.AsyncWith):
        return _with_visits(node, in_tab, bindings)
    if isinstance(node, ast.Call):
        return _call_visits(node, in_tab, bindings)
    return [(child, in_tab) for child in ast.iter_child_nodes(node)]


def _chart_in_tab(root: ast.AST) -> bool:
    """Return True if a chart call occurs inside a Tab/TabsTrigger subtree.

    A chart is nested when it is reached under tab scope (`with Tab(...):`,
    Tab children, or the children of a container whose `parent=` resolves to a
    Tab) or when the chart itself carries `parent=` resolving to a Tab. A
    `parent=` name resolves through the binding nearest before its use, so a
    rebinding or a shadowing parameter ends the Tab's reach. Iterative stack
    walk: each `with` item's context expression is visited, each `with` body
    is visited once regardless of item count, and node depth costs no stack
    frames, so deeply nested expressions cannot raise RecursionError.
    """
    bindings = _collect_bindings(root)
    stack: list[tuple[ast.AST, bool]] = [(root, False)]
    while stack:
        node, in_tab = stack.pop()
        if _is_chart_call(node) and (in_tab or _attaches_to_tab(node, bindings)):
            return True
        stack.extend(_node_visits(node, in_tab, bindings))
    return False


def _validate_chart_placement(code: str) -> None:
    """Reject nesting charts in tabs to avoid 0px container collapse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return
    if _chart_in_tab(tree):
        raise ValueError(
            f"Recharts chart components ({', '.join(sorted(_CHART_NAMES))}) cannot be placed "
            "inside Tab components because tab panels initialize with 0px width, causing charts to "
            "render completely invisible. Place the chart outside of Tabs in a Column, Row, Card, or Grid."
        )
