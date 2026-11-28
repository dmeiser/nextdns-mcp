"""Fails if a reported cross-module private import is restored (issue #783).

The checklist finding PRIN-3 named seven import sites that reached into a
sibling module's ``_``-prefixed names, contradicting the public-name boundary
rule the error package and the dashboard package already enforce
(``test_error_boundaries.py``, ``test_dashboard_boundaries.py``). A private
rename in the owning module breaks such a consumer silently, because the
consumer's import is the only record of the coupling.

The reported dependencies now cross under public names: each owning module
defines the helper without the underscore, and the consumer imports that. This
test reads the source rather than the behaviour, in the shape of the two
precedents: it parses each reported importer - deferred imports included - and
fails if one of the reported private names is imported from its reported
source again, and it checks the public name is really the owning module's own
definition, not an alias parked beside a surviving private one.

Out of scope here, per the finding: the compatibility re-export blocks
(``config``, ``tool_search``, ``tools/logs``) and the private imports the
wider scan surfaced but the finding did not name.

SPDX-License-Identifier: MIT
"""

import ast
import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "nextdns_mcp"

# The reported sites: importer (relative to the package root) -> source module
# (resolved, dotted) -> the private names it was reported importing.
REPORTED_PRIVATE_IMPORTS: dict[str, dict[str, frozenset[str]]] = {
    "generative.py": {"chart_placement": frozenset({"_validate_chart_placement"})},
    "utils.py": {"config": frozenset({"_BOOL_FALSE_TOKENS", "_BOOL_TRUE_TOKENS"})},
    "transport.py": {"client": frozenset({"_log_safe_error", "_redacted"})},
    "lifespan.py": {"tools.logs": frozenset({"_download_registry"})},
    "spec_tools.py": {
        "spec_arguments": frozenset({"_PATH_PLACEHOLDER", "_malformed_profile_id_payload", "_unsafe_path_arguments"}),
        "spec_descriptions": frozenset({"_required"}),
        "spec_series": frozenset(
            {
                "_capped_transport_kwargs",
                "_ceiling_measured_on_the_wire",
                "_series_budget_for_generated_call",
                "_series_refusal",
            }
        ),
        "spec_validation": frozenset({"_normalized_for_validation", "_schema_validation_payload"}),
        "transport": frozenset({"_api_request_payload"}),
    },
    "tool_search.py": {
        "tool_search_answer": frozenset({"_budget_results", "_compact_entry", "_fallback_answer"}),
        "tool_search_rank": frozenset(
            {
                "_CatalogWordMemos",
                "_CatalogWords",
                "_catalog_words_memo",
                "_named_tools",
                "_rank_by_name_overlap",
                "_strip_scope_mentions",
                "_variant_words",
                "_words",
            }
        ),
        "tool_search_shape": frozenset({"_is_write_tool"}),
    },
    "call_log/record.py": {
        "call_log.evidence": frozenset({"_result_evidence"}),
        "call_log.projection": frozenset({"_project_result", "_result_text"}),
    },
}

# The public name each reported private import crossed under, defined in the
# module that owns the helper.
PUBLIC_DEFINITIONS: dict[str, frozenset[str]] = {
    "chart_placement": frozenset({"validate_chart_placement"}),
    "profile_access": frozenset({"BOOL_FALSE_TOKENS", "BOOL_TRUE_TOKENS"}),
    "client": frozenset({"log_safe_error", "redacted"}),
    "tools.logs.retention": frozenset({"download_registry"}),
    "spec_arguments": frozenset({"PATH_PLACEHOLDER", "malformed_profile_id_payload", "unsafe_path_arguments"}),
    "spec_descriptions": frozenset({"required"}),
    "spec_series": frozenset(
        {
            "capped_transport_kwargs",
            "ceiling_measured_on_the_wire",
            "series_budget_for_generated_call",
            "series_refusal",
        }
    ),
    "spec_validation": frozenset({"normalized_for_validation", "schema_validation_payload"}),
    "transport": frozenset({"api_request_payload"}),
    "tool_search_answer": frozenset({"budget_results", "compact_entry", "fallback_answer"}),
    "tool_search_rank": frozenset(
        {
            "CatalogWordMemos",
            "CatalogWords",
            "catalog_words_memo",
            "comparable_words",
            "named_tools",
            "rank_by_name_overlap",
            "strip_scope_mentions",
            "variant_words",
        }
    ),
    "tool_search_shape": frozenset({"is_write_tool"}),
    "call_log.evidence": frozenset({"result_evidence"}),
    "call_log.projection": frozenset({"project_result", "result_text"}),
}


def _resolved_module(importer: Path, node: ast.ImportFrom) -> str:
    """The dotted module an ImportFrom resolves to, relative to the package root."""
    parts = list(importer.relative_to(SRC_DIR).with_suffix("").parts[:-1])
    for _ in range(node.level - 1):
        parts = parts[:-1]
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


def test_no_reported_private_import_is_restored():
    """A name that crosses a module boundary is that module's API: no underscore.

    Deferred (function-level) imports count too - a lazy import is the same
    coupling with a delay on it.
    """
    for importer_rel, sources in REPORTED_PRIVATE_IMPORTS.items():
        importer = SRC_DIR / importer_rel
        tree = ast.parse(importer.read_text(encoding="utf-8"), filename=str(importer))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            forbidden = sources.get(_resolved_module(importer, node))
            if not forbidden:
                continue
            for alias in node.names:
                assert alias.name not in forbidden, (
                    f"{importer_rel} imports private {node.module}.{alias.name} again; "
                    "cross-module helpers cross under public names (issue #783)"
                )


def test_every_reported_helper_is_defined_publicly_in_its_owning_module():
    """The public name is the owning module's own definition, and the reported
    importer binds that same object - not a copy, and not an alias over a
    surviving private definition."""
    for source, names in PUBLIC_DEFINITIONS.items():
        module = importlib.import_module(f"nextdns_mcp.{source}")
        for name in names:
            assert hasattr(module, name), f"nextdns_mcp.{source}.{name} is missing"
    for importer_rel, sources in REPORTED_PRIVATE_IMPORTS.items():
        importer = importlib.import_module(f"nextdns_mcp.{importer_rel.removesuffix('.py').replace('/', '.')}")
        for source in sources:
            defining = importlib.import_module(f"nextdns_mcp.{source}")
            for private_name in sources[source]:
                public_name = private_name.lstrip("_")
                if public_name == "words":
                    public_name = "comparable_words"  # `words` would shadow locals in the owning module
                assert getattr(importer, public_name, None) is getattr(defining, public_name), (
                    f"{importer_rel} does not bind nextdns_mcp.{source}.{public_name}"
                )
