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
PACKAGE_DOTTED = "nextdns_mcp.tools.logs"

# The fetchers: the authenticated API client and the transport helpers that read
# a response body. guard.py vets destinations and owns no request of its own, so
# neither name may appear in its imports, at any import level.
FETCHER_MODULES = ("nextdns_mcp.client", "nextdns_mcp.transport")

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


def _resolved_imports(module_file: Path) -> set[str]:
    """Every module one source file imports, relative imports resolved to dotted names.

    Level-aware on purpose: ``from ...client import …`` (level 3) is exactly how a
    module of this package reaches ``nextdns_mcp.client``, and a check that reads
    only ``level == 1`` cannot see it - the rule would pass whatever the module
    imported. Resolution walks up from this package's own dotted name.
    """
    resolved: set[str] = set()
    parts = PACKAGE_DOTTED.split(".")
    for node in ast.walk(_module_node(module_file)):
        if isinstance(node, ast.Import):
            resolved.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            if node.module:
                resolved.add(node.module)
            continue
        base = ".".join(parts[: len(parts) - (node.level - 1)])
        if node.module:
            resolved.add(f"{base}.{node.module}" if base else node.module)
        else:  # ``from . import guard`` / ``from ... import client``
            resolved.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return resolved


def _package_imports(module_file: Path) -> set[str]:
    """The siblings of this package one source file imports, however it spells them.

    ``from . import guard``, ``from .guard import x`` and the out-and-back
    ``from ..logs import guard`` all resolve to the same sibling name; importing
    the package itself counts as importing ``__init__``.
    """
    found: set[str] = set()
    prefix = PACKAGE_DOTTED + "."
    for name in _resolved_imports(module_file):
        if name == PACKAGE_DOTTED:
            found.add("__init__")
        elif name.startswith(prefix):
            found.add(name[len(prefix) :].split(".")[0])
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
    roots = {name.split(".")[0] for name in _resolved_imports(PACKAGE_DIR / "preview.py")}
    assert "httpx" not in roots, "preview.py imports httpx"
    assert "os" not in roots, "preview.py imports os"
    assert _package_imports(PACKAGE_DIR / "preview.py") == set(), "preview.py imports a package sibling"


def test_guard_fetches_nothing():
    """guard.py vets destinations; the clients that act on the verdict live elsewhere.

    Checked against the resolved module names, not the level-1 sibling set: the
    only way this dependency could be written is ``from ... import client`` /
    ``from ...transport import …``, which resolves outside the package, so a
    check restricted to package siblings would pass no matter what guard.py did.
    """
    offending = sorted(
        name
        for name in _resolved_imports(PACKAGE_DIR / "guard.py")
        if name in FETCHER_MODULES or name.startswith(tuple(f"{m}." for m in FETCHER_MODULES))
    )
    assert not offending, f"guard.py imports a fetcher: {offending}"


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
