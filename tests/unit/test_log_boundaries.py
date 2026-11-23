"""Guards the logs package's module boundaries (WORK-275).

The guarded download path used to be one 683-line module holding six
concerns. A split that nothing defends collapses back the first time someone
needs a helper in a hurry, so this test reads the source rather than the
behaviour: the package layout, the one-way import edges between the modules,
the purity of the two modules that own no I/O, and the re-exported surface
the rest of the repo resolves on the package.

SPDX-License-Identifier: MIT
"""

import ast
from pathlib import Path

from nextdns_mcp.tools import logs as logs_module
from nextdns_mcp.tools.logs import retention, tool

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "src" / "nextdns_mcp" / "tools" / "logs"

# One concern per module, per the recorded split:
# __init__ - the re-exported surface only.
# retention - the registry, eviction, shutdown cleanup, temp-file removal.
# guard - hop vetting and the pinned resolver.
# preview - the pure preview accounting (no httpx, no os, no I/O).
# stream - body -> disk under the total-size cap.
# redirects - the unauthenticated hop chain.
# tool - downloadLogs's implementation and its error mapping.
PACKAGE_MODULES = ("__init__.py", "retention.py", "guard.py", "preview.py", "stream.py", "redirects.py", "tool.py")

# The dependency direction is a DAG and runs one way:
# preview <- stream <- redirects <- tool; guard <- redirects; retention <- tool.
# ``__init__`` is the surface: it may re-export from any sibling, but no sibling
# may import it. ``tool`` additionally imports ``guard`` and ``stream`` for the
# two exception types it maps to payloads - class objects, not behaviour.
ALLOWED_PACKAGE_IMPORTS = {
    "__init__": {"retention", "guard", "preview", "stream", "redirects", "tool"},
    "retention": set(),
    "guard": set(),
    "preview": set(),
    "stream": {"preview"},
    "redirects": {"guard", "stream"},
    "tool": {"redirects", "retention", "guard", "stream"},
}

# The edges the split exists to create: drop one and a concern leaked back
# into its consumer.
REQUIRED_PACKAGE_IMPORTS = {
    "stream": {"preview"},
    "redirects": {"guard", "stream"},
    "tool": {"redirects", "retention"},
}


def _module_node(module_file: Path) -> ast.Module:
    return ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))


def _package_imports(module_file: Path) -> set[str]:
    """The sibling modules one source file imports (``from . import x`` / ``from .x import``)."""
    found = set()
    for node in ast.walk(_module_node(module_file)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:  # ``from .guard import ...``
                found.add(node.module.split(".")[0])
            else:  # ``from . import guard, stream``
                found.update(alias.name for alias in node.names)
    return found


def test_logs_is_a_package_not_a_module():
    """The god module stayed split: a package directory, one concern per module."""
    assert PACKAGE_DIR.is_dir()
    for name in PACKAGE_MODULES:
        assert (PACKAGE_DIR / name).is_file(), f"{name} missing from the logs package"
    assert not (PACKAGE_DIR.parent / "logs.py").exists(), "logs.py came back beside the package"


def test_every_module_names_its_concern():
    """The split is documented where a reader lands: each module docstring says what it owns."""
    for name in PACKAGE_MODULES:
        docstring = ast.get_docstring(_module_node(PACKAGE_DIR / name))
        assert docstring, f"{name} has no module docstring naming its concern"


def test_import_edges_run_one_way():
    """No sibling imports ``__init__``; every intra-package import stays inside §2's DAG."""
    for name in PACKAGE_MODULES:
        imports = _package_imports(PACKAGE_DIR / name)
        owner = name.removesuffix(".py")
        assert owner != "__init__" or imports <= ALLOWED_PACKAGE_IMPORTS["__init__"]
        if owner == "__init__":
            continue
        assert "__init__" not in imports, f"{name} imports the package surface back"
        assert imports <= ALLOWED_PACKAGE_IMPORTS[owner], f"{name} imports {imports - ALLOWED_PACKAGE_IMPORTS[owner]}"
    for owner, required in REQUIRED_PACKAGE_IMPORTS.items():
        assert required <= _package_imports(PACKAGE_DIR / f"{owner}.py"), f"{owner} lost its edge to {required}"


def test_import_graph_is_acyclic():
    """A cycle would make the "one concern per module" claim a fiction."""
    edges = {name.removesuffix(".py"): _package_imports(PACKAGE_DIR / name) for name in PACKAGE_MODULES}
    visiting, done = set(), set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        assert node not in visiting, f"import cycle: {' -> '.join(path + (node,))}"
        if node in done:
            return
        visiting.add(node)
        for nxt in edges.get(node, set()):
            visit(nxt, path + (node,))
        visiting.discard(node)
        done.add(node)

    for node in edges:
        visit(node, ())


def test_preview_is_pure_text_accounting():
    """preview.py owns no I/O: no httpx, no os, no sibling imports at all."""
    imported = set()
    for node in ast.walk(_module_node(PACKAGE_DIR / "preview.py")):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert "httpx" not in imported, "preview.py imports httpx"
    assert "os" not in imported, "preview.py imports os"
    assert _package_imports(PACKAGE_DIR / "preview.py") == set(), "preview.py imports a package sibling"


def test_guard_fetches_nothing():
    """guard.py vets destinations; the clients that act on the verdict live elsewhere."""
    imported = _package_imports(PACKAGE_DIR / "guard.py")
    assert "client" not in imported and "transport" not in imported, "guard.py imports a fetcher"


def test_package_surface_is_a_rebind_not_a_copy():
    """Callers resolve these names on the package, so the package must bind the owning module's object."""
    surface = {
        "_download_registry": retention,
        "cleanup_successful_downloads": retention,
        "_download_logs_to_tempfile": tool,
        "MAX_RETAINED_DOWNLOADS": retention,
        "_download_error_details": tool,
    }
    for name, owner in surface.items():
        assert getattr(logs_module, name) is getattr(owner, name), f"logs.{name} is not retention/tool's own"
