"""Guards the dashboard package's module boundaries (WORK-117).

The reported defect was a 1,100-line module reaching across into its plotting
sibling for ``_``-prefixed helpers. A split that nothing defends collapses back
the first time someone needs a card in a hurry, so this test reads the source
rather than the behaviour: the package layout, what each module is allowed to
import, and where the shared time-series primitives are actually defined.

SPDX-License-Identifier: MIT
"""

import ast
from pathlib import Path

import pytest

from nextdns_mcp import chart_ui, charts
from nextdns_mcp.tools import dashboard
from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.dashboard import components as components_module

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "src" / "nextdns_mcp" / "tools" / "dashboard"
SRC_DIR = REPO_ROOT / "src" / "nextdns_mcp"

# One concern per module: fetch/shape, UI, summary text, dispatch + tool wrapper.
PACKAGE_MODULES = ("__init__.py", "data.py", "components.py", "formatters.py", "tool.py")

# The primitives the dashboard and the plotting tool share. The pure series and
# budget helpers belong to ``nextdns_mcp.charts``; the builders that construct a
# Prefab component belong to ``nextdns_mcp.chart_ui`` - the split that keeps
# ``spec_tools``, and every generated tool behind it, from importing
# ``prefab_ui`` (issue #550). Neither consumer owns either module.
SHARED_PRIMITIVES = (
    (charts, "analytics_range_error"),
    (charts, "build_chart_data"),
    (charts, "downsample_chart_series"),
    (charts, "resolve_time_point"),
    (charts, "series_budget_error"),
    (charts, "validate_series_payload"),
    (chart_ui, "build_chart_series"),
    (chart_ui, "build_timeseries_area_chart"),
)


def _import_statements(module_file: Path) -> list[ast.ImportFrom]:
    """Every ``from ... import`` in one source file."""
    tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]


def test_dashboard_is_a_package_not_a_module():
    """The god module stayed split: a package directory, one concern per module."""
    assert PACKAGE_DIR.is_dir()
    for name in PACKAGE_MODULES:
        assert (PACKAGE_DIR / name).is_file(), f"{name} missing from the dashboard package"
    assert not (PACKAGE_DIR.parent / "dashboard.py").exists(), "dashboard.py came back beside the package"


def test_dashboard_never_imports_a_sibling_tool_module():
    """The dashboard reads shared code through the package root, not through plots."""
    for module_file in sorted(PACKAGE_DIR.glob("*.py")):
        for node in _import_statements(module_file):
            if node.level and node.module in {"plots", "logs", "doh", "metrics"}:
                pytest.fail(f"{module_file.name} imports sibling tool module .{node.module}")


def test_dashboard_imports_no_private_name_from_a_sibling_dashboard_module():
    """Cross-module helpers cross under public names (issue #637).

    The package modules shared row shapers, card builders, and summary
    helpers by importing each other's ``_``-prefixed names, so a private
    rename in the owning module silently broke its siblings. A name that
    crosses a module boundary is that module's API and lives without the
    underscore; ``_``-prefixed names stay module-internal, like the
    dashboard-internal ``data._fetch_analytics_payload`` documented in
    AGENT.md. This fails if a cited private import is restored.
    """
    sibling_modules = {"data", "components", "formatters", "tool"}
    for module_file in sorted(PACKAGE_DIR.glob("*.py")):
        for node in _import_statements(module_file):
            if not node.level or not node.module:
                continue
            if node.module.split(".")[0] not in sibling_modules:
                continue
            for alias in node.names:
                assert not alias.name.startswith("_"), f"{module_file.name} imports private .{node.module}.{alias.name}"


def test_dashboard_imports_no_private_name_from_the_shared_charts_modules():
    """Anything crossing into ``charts`` or ``chart_ui`` crosses under a public name."""
    for module_file in sorted(PACKAGE_DIR.glob("*.py")):
        for node in _import_statements(module_file):
            if node.module not in {"charts", "chart_ui"}:
                continue
            for alias in node.names:
                assert not alias.name.startswith("_"), f"{module_file.name} imports private {node.module}.{alias.name}"


@pytest.mark.parametrize(("module", "name"), SHARED_PRIMITIVES)
def test_shared_primitive_is_defined_in_its_shared_module(module, name: str) -> None:
    """The primitives are shared modules' own API: consumers import them, they do not re-make them.

    The first attempt at this ticket left ``build_chart_data = _build_chart_data`` at
    the bottom of ``tools/plots.py`` and imported the alias - the coupling wearing the
    public name. A top-level assignment of one of these names under ``tools/`` is that
    shape again, so it fails here even though the import would keep working.
    """
    original = getattr(module, name)
    assert original.__module__ == module.__name__

    for consumer in (plots_module, components_module):
        bound = getattr(consumer, name, None)
        assert bound is None or bound is original, f"{consumer.__name__} binds its own {name}"

    for module_file in sorted((REPO_ROOT / "src" / "nextdns_mcp" / "tools").rglob("*.py")):
        tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            ):
                pytest.fail(f"{module_file.name} re-aliases {name} instead of importing it from {module.__name__}")


def test_charts_module_stays_free_of_the_ui_component_library():
    """``charts`` is the UI-free half of the split (issue #550).

    ``spec_tools`` reads the series budgets from ``charts``, and every generated
    tool stands behind it: a ``prefab_ui`` import back in ``charts.py`` pulls the
    component tree into a process that renders nothing. The behaviour test in
    ``tests/unit/test_plotting.py`` proves it at import time; this one says which
    module must stay clean.
    """
    tree = ast.parse((SRC_DIR / "charts.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert "prefab_ui" not in imported, "charts.py imports prefab_ui again"


def test_timeseries_area_chart_is_built_in_one_place():
    """No analytics caller re-inlines the ``AreaChart`` kwargs (issue #439).

    ``SHARED_PRIMITIVES`` catches a consumer re-aliasing the factory, but not
    one quietly rebuilding the component by hand - the duplicated-kwargs shape
    this ticket removed. ``chart_ui.py`` is the only module in the package that
    may construct an ``AreaChart``; consumers call ``build_timeseries_area_chart``.
    """
    for module_file in sorted((REPO_ROOT / "src" / "nextdns_mcp").rglob("*.py")):
        if module_file.name == "chart_ui.py":
            continue
        tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AreaChart":
                pytest.fail(f"{module_file.name} constructs AreaChart instead of calling the shared factory")


def test_package_public_surface():
    """Callers import the tool from the package, so the package must export it."""
    assert dashboard.overviewDashboard.__module__.startswith("nextdns_mcp.tools.dashboard")
    assert "overviewDashboard" in dashboard.__all__


def test_overview_dashboard_delegates_once():
    """The published wrapper ends in one delegation, with nothing after it (WORK-279).

    ``overviewDashboard`` used to close with two identical
    ``return await _overview_dashboard_impl(...)`` blocks in a row; the second
    could never run and read as a second implementation path. This test pins
    the removal so a copy-paste re-introduction fails on the broken tree - the
    same shape ``8f6249c`` pinned for the dead ``OpenApiSpecNotFound`` class.
    """
    module_file = PACKAGE_DIR / "tool.py"
    tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
    (func,) = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "overviewDashboard"
    ]
    returns = [node for node in ast.walk(func) if isinstance(node, ast.Return)]
    assert len(returns) == 1, f"expected exactly one delegation to _overview_dashboard_impl, found {len(returns)}"
    assert func.body[-1] is returns[0], "a statement sits after the delegation; the return must be the tail"
    call = returns[0].value
    assert isinstance(call, ast.Await) and isinstance(call.value, ast.Call), "the delegation must be a single await"
    assert isinstance(call.value.func, ast.Name) and call.value.func.id == "_overview_dashboard_impl"
    assert not call.value.args, "the delegation must pass every argument by keyword"
    assert [kw.arg for kw in call.value.keywords] == [
        "profile_id",
        "from_time",
        "to_time",
        "limit",
        "section",
        "view_type",
        "domain_view",
    ], "the delegation's keyword arguments drifted from the published signature"
