"""Guards the error package's module boundaries (issue #629).

The reported shape was one module holding seven concerns: three independent
readers of the same failed response (the body cap, the upstream error document,
and the rate-limit headers) sharing one namespace, so a helper from one could be
called from another and nothing would say so - and importing ``ErrorCode``, the
name ``client``, ``utils``, ``spec_tools``, ``charts`` and five tool modules need,
executed the body-slicing and header-parsing code with it. This test reads the
source rather than the behaviour: the package layout, which modules are leaves,
which module may import the three parsers, where each published name is actually
defined, and that a ``_``-prefixed name never crosses a module boundary.

SPDX-License-Identifier: MIT
"""

import ast
from pathlib import Path

from nextdns_mcp import errors as errors_package

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "nextdns_mcp"
PACKAGE_DIR = SRC_DIR / "errors"

# One concern per module, the shape the dashboard package already uses.
PACKAGE_MODULES = ("__init__.py", "codes.py", "body.py", "upstream.py", "rate_limit.py", "http.py", "exceptions.py")

# The modules that own one reading each and need nothing from beside them.
LEAF_MODULES = ("codes", "body", "upstream", "rate_limit", "exceptions")

# The three independent parsers over the same response: bounding the body,
# diagnosing the upstream document, and reading the rate-limit headers.
PARSER_MODULES = ("body", "upstream", "rate_limit")

# Every module that can hold a definition, i.e. everything but the package root.
DEFINING_MODULES = (*LEAF_MODULES, "http")

# The surface callers import from ``nextdns_mcp.errors``, unchanged by the split.
# ``cap_with_marker`` is on it because ``utils.sanitize_label`` borrows the shared
# cap from the package root: a name that crosses out of the package is public.
PUBLISHED_SURFACE = (
    "BODY_SLICE_BYTES",
    "ErrorCode",
    "MAX_RESPONSE_BODY_CHARS",
    "NextDNSAuthError",
    "NextDNSError",
    "NextDNSRateLimitError",
    "NextDNSResponseTooLargeError",
    "NextDNSServerError",
    "TRUNCATION_MARKER",
    "cap_with_marker",
    "error_payload",
    "http_error_payload",
    "response_body_text",
    "upstream_error_codes",
    "upstream_error_details",
)

# The private helpers that lived at the module root before the split. Each is now
# reachable only in the module that owns it.
MODULE_INTERNAL_HELPERS = (
    "_BODY_BOUNDS",
    "_add_upstream_diagnosis",
    "_attach_error_body",
    "_attach_rate_limit_headers",
    "_attach_x_ratelimit_metadata",
    "_bound_structured_fields",
    "_bound_value",
    "_cap_with_marker",
    "_parse_http_date_delay",
    "_parse_retry_after",
    "_read_response_body_prefix",
    "_structured_error_payload",
    "_upstream_error_items",
    "_upstream_error_summary",
)


def _tree(module_file: Path) -> ast.Module:
    return ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))


def _import_nodes(module_file: Path) -> list[ast.Import | ast.ImportFrom]:
    """Every import statement in one source file."""
    return [node for node in ast.walk(_tree(module_file)) if isinstance(node, (ast.Import, ast.ImportFrom))]


def _dotted_name(module_file: Path) -> str:
    """The dotted module name of a file under ``src/nextdns_mcp``."""
    parts = list(module_file.relative_to(SRC_DIR.parent).parts)
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_source(node: ast.ImportFrom, module_file: Path) -> str:
    """The module an ``import from`` pulls its names from, as a dotted name.

    Relative imports are resolved against the importing file, so
    ``from .body import ...`` inside the error package reads as
    ``nextdns_mcp.errors.body`` - the same name an absolute import spells.
    """
    base = _dotted_name(module_file).split(".")
    if module_file.name != "__init__.py":
        base.pop()
    if node.level > 1:
        del base[len(base) - (node.level - 1) :]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _imported_modules(module_file: Path) -> set[str]:
    """Absolute dotted names of the modules one file's imports reach."""
    reached: set[str] = set()
    for node in _import_nodes(module_file):
        if isinstance(node, ast.Import):
            reached.update(alias.name for alias in node.names)
        elif node.module is None:
            # ``from . import body``: the names are modules or attributes of the
            # package, and reaching either counts here.
            source = _resolved_source(node, module_file)
            reached.add(source)
            reached.update(f"{source}.{alias.name}" for alias in node.names)
        else:
            reached.add(_resolved_source(node, module_file))
    return reached


def _defined_names(module_file: Path) -> set[str]:
    """Names a file itself defines at module level: classes, functions, bindings."""
    names: set[str] = set()
    for node in _tree(module_file).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_errors_is_a_package_not_a_module():
    """The seven-module layout stayed, and the single module did not come back."""
    assert PACKAGE_DIR.is_dir()
    for name in PACKAGE_MODULES:
        assert (PACKAGE_DIR / name).is_file(), f"{name} missing from the errors package"
    assert not (PACKAGE_DIR.parent / "errors.py").exists(), "errors.py came back beside the package"
    assert errors_package.__all__, "the package root publishes no surface"


def test_leaf_modules_import_no_sibling():
    """Each concern stands alone, so importing it executes none of the others."""
    siblings = {f"nextdns_mcp.errors.{name}" for name in (*LEAF_MODULES, "http")}
    for module_name in LEAF_MODULES:
        module_file = PACKAGE_DIR / f"{module_name}.py"
        reached = _imported_modules(module_file) & siblings
        assert not reached, f"{module_name}.py imports sibling {sorted(reached)}"


def test_http_is_the_only_importer_of_the_three_parsers():
    """The three parsers meet in one composer, and nowhere else in the package.

    The package root re-exports every module, which is what keeps the published
    surface where callers already import it; ``http`` is the only module that can
    call across, so the coupling stays one edge instead of six.
    """
    composer = PACKAGE_DIR / "http.py"
    parsers = {f"nextdns_mcp.errors.{name}" for name in PARSER_MODULES}
    for module_file in sorted(SRC_DIR.rglob("*.py")):
        if module_file in (composer, PACKAGE_DIR / "__init__.py"):
            continue
        reached = _imported_modules(module_file) & parsers
        assert not reached, (
            f"{module_file.relative_to(REPO_ROOT)} imports {sorted(reached)} directly; "
            "compose a payload in errors/http.py or read it from the package root"
        )


def test_no_module_imports_a_private_name_from_a_sibling():
    """A name that crosses a module boundary is that module's API: no underscore.

    A private rename in the owning module otherwise breaks its siblings silently,
    which is the coupling the single module made invisible.
    """
    for module_file in sorted(PACKAGE_DIR.glob("*.py")):
        for node in _import_nodes(module_file):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            for alias in node.names:
                assert not alias.name.startswith("_"), (
                    f"{module_file.name} imports private .{node.module or ''}.{alias.name}"
                )


def test_no_module_outside_the_package_borrows_a_private_error_name():
    """Consumers take the package's API, never its internals.

    ``utils.sanitize_label`` imported ``errors._cap_with_marker`` - the one private
    name that crossed out of the package - so a rename inside the owning module
    would break a consumer that module cannot see. The shared cap crosses as
    ``cap_with_marker`` now, and this fails if a private name is borrowed again.
    """
    for module_file in sorted(SRC_DIR.rglob("*.py")):
        if PACKAGE_DIR in module_file.parents:
            continue
        for node in _import_nodes(module_file):
            if not isinstance(node, ast.ImportFrom) or _resolved_source(node, module_file) != "nextdns_mcp.errors":
                continue
            for alias in node.names:
                assert not alias.name.startswith("_"), (
                    f"{module_file.relative_to(REPO_ROOT)} imports private nextdns_mcp.errors.{alias.name}"
                )


def test_every_published_name_is_defined_in_exactly_one_module():
    """``__all__`` re-exports a definition; it does not collect duplicates or aliases."""
    defined_in: dict[str, list[str]] = {}
    for module_name in DEFINING_MODULES:
        for name in _defined_names(PACKAGE_DIR / f"{module_name}.py"):
            defined_in.setdefault(name, []).append(module_name)

    for name in errors_package.__all__:
        owners = defined_in.get(name, [])
        assert len(owners) == 1, f"{name} is defined in {owners or 'no module'} of {DEFINING_MODULES}"
        defining_module = f"nextdns_mcp.errors.{owners[0]}"
        bound = getattr(errors_package, name)
        origin = getattr(bound, "__module__", None)
        if origin is not None:
            assert origin == defining_module, f"{name} resolves from {origin}, not {defining_module}"


def test_package_root_publishes_names_and_defines_none():
    """``__init__.py`` holds the surface, not a second copy of any behaviour."""
    defined = _defined_names(PACKAGE_DIR / "__init__.py")
    assert defined == {"__all__"}, f"the package root defines {sorted(defined - {'__all__'})}"


def test_published_surface_still_imports_from_the_package_root():
    """Ten modules and the behaviour suites read these names from the package root."""
    for name in PUBLISHED_SURFACE:
        assert hasattr(errors_package, name), f"{name} is no longer reachable from nextdns_mcp.errors"
        assert name in errors_package.__all__, f"{name} is not in the package root's __all__"


def test_module_internal_helpers_are_not_reachable_from_the_package_root():
    """The parsers' internals stopped being package-root names.

    Before the split the root re-exported them by accident of being one file -
    ``from nextdns_mcp.errors import _parse_retry_after`` worked, and a test did
    exactly that. Each helper is now named in the module that owns it, which is
    what makes the leaf/importer rules above mean something.
    """
    for name in MODULE_INTERNAL_HELPERS:
        assert not hasattr(errors_package, name), f"{name} is reachable from the package root again"
