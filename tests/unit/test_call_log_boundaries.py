"""Guards the call-log package's module boundaries (issue #628).

The reported defect was one 498-line module holding five concerns: the FastMCP
hook, what a row says about a call, the bounded projection, the identity
evidence, and the log file itself. A split that nothing defends collapses back
the first time someone needs one more helper in a hurry, so this test reads the
source rather than the behaviour: the package layout, what each module may
import, which module owns the disk, and the import path callers still use.

SPDX-License-Identifier: MIT
"""

import ast
from pathlib import Path

import pytest

from nextdns_mcp import call_log

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "src" / "nextdns_mcp" / "call_log"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# One concern per module, with the line budget each one carries. A split costs
# lines over the module it replaces (a docstring and an import block per
# module), so the total is the honest cost of the split file plus the SEC-8
# checked-open growth noted below, not a number to squeeze the code under.
PACKAGE_MODULES = {
    "__init__.py": 45,
    # 110 -> 125 for SEC-8 (#766): every read and append now goes through the
    # O_NOFOLLOW + S_ISREG checked open (`_checked_fd`); the check is the growth.
    "store.py": 125,
    "projection.py": 110,
    "evidence.py": 150,
    "record.py": 100,
    "middleware.py": 100,
}

# The public surface: `openapi.py` and the e2e harness import these from the
# package root, and the harness reuses `read_call_log_rows` deliberately so no
# second row parser can drift from the server's own reader.
PUBLIC_SURFACE = (
    ("middleware.py", "CallLogMiddleware"),
    ("store.py", "CALL_LOG_ENV"),
    ("store.py", "call_log_path"),
    ("store.py", "get_call_log"),
    ("store.py", "read_call_log_rows"),
    ("record.py", "result_error"),
)

# The row's columns. Deciding them is `record.py`'s one job, so no other module
# in the package may name one: that is how row semantics leak back into the
# hook that only has to observe a call.
ROW_COLUMNS = ('"ts":', '"tool":', '"profile_id":', '"args":', '"ok":', '"error":', '"result":', '"write":')

# Reading or writing the log file. `store.py` owns the ``0600`` fail-closed open
# and the lock that keeps concurrent records whole; a second module holding an
# `open()` is a second writer free to skip both.
DISK_MARKERS = ("open(", "os.open", "os.fdopen", "os.fchmod", "threading", "_write_lock")


def _source(module: str) -> str:
    return (PACKAGE_DIR / module).read_text(encoding="utf-8")


def _imported_modules(module: str) -> set[str]:
    """Modules one file imports: dotted for absolute, leading dots for relative."""
    tree = ast.parse(_source(module), filename=module)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add("." * node.level + node.module)
    return imported


def _sibling_imports(module: str) -> set[str]:
    """Sibling modules of the package one file imports by name."""
    return {name[1:] for name in _imported_modules(module) if name.startswith(".") and not name.startswith("..")}


def _defines(module: str, name: str) -> bool:
    """Whether one module binds `name` itself, rather than importing it."""
    tree = ast.parse(_source(module), filename=module)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return True
    return False


def test_call_log_is_a_package_not_a_module():
    """The god module stayed split: a package directory, one concern per module."""
    assert PACKAGE_DIR.is_dir()
    for name in PACKAGE_MODULES:
        assert (PACKAGE_DIR / name).is_file(), f"{name} missing from the call_log package"
    assert not (PACKAGE_DIR.parent / "call_log.py").exists(), "call_log.py came back beside the package"
    assert sorted(p.name for p in PACKAGE_DIR.glob("*.py")) == sorted(PACKAGE_MODULES), (
        "a module joined the package without a boundary decision"
    )


def test_each_module_stays_inside_its_line_budget():
    """The split is allowed to cost lines, up to the stated total - not past it."""
    total = 0
    for name, budget in PACKAGE_MODULES.items():
        lines = len(_source(name).splitlines())
        total += lines
        assert lines <= budget, f"{name} is {lines} lines, over its {budget}-line budget"
    # 560 -> 595 for SEC-8 (#766): the O_NOFOLLOW + S_ISREG checked open
    # (store.py), the iterative depth-capped identity walk (evidence.py) and
    # the bounded profile_id (record.py) cost 34 lines of real code.
    assert total <= 595, f"the package is {total} lines, over the 595-line total"


def test_middleware_is_the_only_module_that_knows_fastmcp():
    """The FastMCP hook stays one module: the rest of the log is plain Python.

    A `fastmcp` import back in `record.py` or `store.py` would make the row
    contract and the file contract untestable without a server, and would put
    the library's surface in front of every change to either.
    """
    assert any(name.split(".")[0] == "fastmcp" for name in _imported_modules("middleware.py"))
    for name in PACKAGE_MODULES:
        if name == "middleware.py":
            continue
        assert not any(mod.split(".")[0] == "fastmcp" for mod in _imported_modules(name)), (
            f"{name} imports fastmcp again"
        )


def test_store_is_the_only_module_that_touches_the_log_file():
    """One module owns the ``0600`` open, the mode check, and the write lock."""
    for marker in ("os.open", "os.fchmod", "_write_lock"):
        assert marker in _source("store.py"), f"store.py no longer holds {marker}"
    for name in PACKAGE_MODULES:
        if name == "store.py":
            continue
        found = [marker for marker in DISK_MARKERS if marker in _source(name)]
        assert not found, f"{name} reaches for the disk again: {found}"


@pytest.mark.parametrize("module", ["projection.py", "evidence.py"])
def test_projection_and_evidence_import_neither_store_nor_middleware(module: str):
    """Bounding and evidence are pure functions of a value.

    They take the payload and return facts: importing the log file or the hook
    would make the budget for a row depend on where that row happens to be
    written, and would close the import cycle the split exists to open.
    """
    assert not _sibling_imports(module) & {"store", "middleware", "record"}, f"{module} imports the write side"


def test_projection_uses_the_shared_bounder():
    """The walk is ``json_bounds``' (issue #614); only the caps and marker key are ours.

    Re-inlining a private list/dict walk here is the two conventions drifting
    apart again — the failure the shared bounder exists to prevent — and it
    would silently fork the call-log caps from the ones ``errors`` inlines
    with.
    """
    assert "..json_bounds" in _imported_modules("projection.py"), "projection.py no longer uses the shared bounder"
    for name in ("_project_list", "_project_dict"):
        assert f"def {name}(" not in _source("projection.py"), f"projection.py re-inlined {name}"


def test_record_is_pure_apart_from_the_clock():
    """`record.py` decides what a row says, with no server and no filesystem."""
    imported = _imported_modules("record.py")
    assert not _sibling_imports("record.py") & {"store", "middleware"}, "record.py imports the write side"
    assert "asyncio" not in imported and not any(mod.split(".")[0] == "fastmcp" for mod in imported), (
        "record.py reached for the runtime it must stay free of"
    )
    assert "time" in imported, "record.py no longer stamps its own row"


def test_no_module_outside_record_names_a_row_column():
    """Row assembly left the hook: only `record.py` spells the columns."""
    for name in PACKAGE_MODULES:
        if name == "record.py":
            continue
        found = [column for column in ROW_COLUMNS if column in _source(name)]
        assert not found, f"{name} spells row columns again: {found}"


def test_package_root_re_exports_and_holds_no_logic():
    """`__init__.py` is the public surface, not a place behaviour can hide."""
    tree = ast.parse(_source("__init__.py"), filename="__init__.py")
    stray: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # the module docstring: prose is not logic
        stray.append(type(node).__name__)
    assert not stray, f"__init__.py contains logic again: {stray}"


@pytest.mark.parametrize(("module", "name"), PUBLIC_SURFACE)
def test_public_name_is_defined_in_its_own_module_and_reachable_from_the_package(module: str, name: str) -> None:
    """The import path callers use did not move, and the name has one home.

    `openapi.py` imports `CallLogMiddleware` and `call_log_path` from the
    package root, and `tests/unit/test_call_log.py` monkeypatches
    `nextdns_mcp.openapi.call_log_path`: an import that reaches past the
    package into `call_log.store` leaves that fake path unpatched while the
    suite stays green against the real environment variable.
    """
    assert _defines(module, name), f"{name} is no longer defined in {module}"
    bound = getattr(call_log, name)
    if hasattr(bound, "__module__"):
        assert bound.__module__ == f"nextdns_mcp.call_log.{module.removesuffix('.py')}"
    assert name in call_log.__all__, f"{name} is not in the package's public surface"


def _reader_import_forms() -> tuple[list[str], list[str]]:
    """How the e2e harness modules import the server's own row reader.

    Scans every ``scripts/ai_e2e_*.py`` module rather than the one file that
    happens to hold the reader today: that file was split apart once (WORK-285)
    and will be split again, and a probe that pins a filename goes red when a
    refactor moves a correct import instead of when a wrong one appears.

    Returns ``(root_imports, submodule_imports)``: the modules holding the
    package-root form ``from nextdns_mcp.call_log import read_call_log_rows``,
    and how any module reaches the reader through a submodule instead - either
    ``from nextdns_mcp.call_log.<module> import read_call_log_rows``, or a
    submodule imported from the package root and then dotted into. Function-local
    imports count: the harness keeps its reader inside the step that needs it.
    """
    root_imports: list[str] = []
    submodule_imports: list[str] = []
    submodules = {name.removesuffix(".py") for name in PACKAGE_MODULES if name != "__init__.py"}
    for path in sorted(SCRIPTS_DIR.glob("ai_e2e_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        aliased: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module == "nextdns_mcp.call_log":
                for alias in node.names:
                    if alias.name == "read_call_log_rows":
                        root_imports.append(path.name)
                    elif alias.name in submodules:
                        aliased.add(alias.asname or alias.name)
            elif node.module.startswith("nextdns_mcp.call_log.") and any(
                alias.name == "read_call_log_rows" for alias in node.names
            ):
                submodule_imports.append(f"{path.name}: from {node.module} import read_call_log_rows")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "read_call_log_rows"
                and isinstance(node.value, ast.Name)
                and node.value.id in aliased
            ):
                submodule_imports.append(f"{path.name}: read through the imported {node.value.id} submodule")
    return root_imports, submodule_imports


def test_openapi_and_the_harness_keep_the_package_root_import():
    """The two consumers whose import lines this split may not touch.

    The harness reads rows with the server's own reader on purpose; a second
    parser is free to drift from what the server actually writes. Which harness
    module holds that import is the harness's business, so the probe reads the
    import itself from every harness module, and still fails when no module makes
    it - otherwise a refactor that drops the reader would pass vacuously.
    """
    openapi = (REPO_ROOT / "src" / "nextdns_mcp" / "openapi.py").read_text(encoding="utf-8")
    assert "from .call_log import CallLogMiddleware, call_log_path" in openapi
    root_imports, submodule_imports = _reader_import_forms()
    assert not submodule_imports, "the harness reached past the package for the row reader: " + "; ".join(
        submodule_imports
    )
    assert root_imports, (
        "no scripts/ai_e2e_*.py module imports read_call_log_rows: the harness must reuse the "
        "server's own row reader rather than keep a second parser of its own"
    )
