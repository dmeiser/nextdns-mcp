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

from nextdns_mcp import charts
from nextdns_mcp.tools import dashboard
from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools.dashboard import components as components_module

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "src" / "nextdns_mcp" / "tools" / "dashboard"

# One concern per module: fetch/shape, UI, summary text, dispatch + tool wrapper.
PACKAGE_MODULES = ("__init__.py", "data.py", "components.py", "formatters.py", "tool.py")

# The primitives the dashboard and the plotting tool share. They belong to
# ``nextdns_mcp.charts``, which neither consumer owns.
SHARED_PRIMITIVES = (
    "build_chart_data",
    "build_chart_series",
    "resolve_time_point",
    "series_budget_error",
    "validate_series_payload",
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


def test_dashboard_imports_no_private_name_from_the_shared_charts_module():
    """Anything crossing into ``charts`` crosses under a public name."""
    for module_file in sorted(PACKAGE_DIR.glob("*.py")):
        for node in _import_statements(module_file):
            if node.module != "charts":
                continue
            for alias in node.names:
                assert not alias.name.startswith("_"), f"{module_file.name} imports private charts.{alias.name}"


@pytest.mark.parametrize("name", SHARED_PRIMITIVES)
def test_shared_primitive_is_defined_in_charts(name: str):
    """The primitives are charts' own API: consumers import them, they do not re-make them.

    The first attempt at this ticket left ``build_chart_data = _build_chart_data`` at
    the bottom of ``tools/plots.py`` and imported the alias - the coupling wearing the
    public name. A top-level assignment of one of these names under ``tools/`` is that
    shape again, so it fails here even though the import would keep working.
    """
    original = getattr(charts, name)
    assert original.__module__ == charts.__name__

    for consumer in (plots_module, components_module):
        bound = getattr(consumer, name, None)
        assert bound is None or bound is original, f"{consumer.__name__} binds its own {name}"

    for module_file in sorted((REPO_ROOT / "src" / "nextdns_mcp" / "tools").rglob("*.py")):
        tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            ):
                pytest.fail(f"{module_file.name} re-aliases {name} instead of importing it from charts")


def test_package_public_surface():
    """Callers import the tool from the package, so the package must export it."""
    assert dashboard.overviewDashboard.__module__.startswith("nextdns_mcp.tools.dashboard")
    assert "overviewDashboard" in dashboard.__all__
