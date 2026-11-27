## What this solves

Helpers that crossed a module boundary are now defined and imported under public names, so a private rename in the owning module can no longer silently break its consumers. A source-level boundary test fails if any of the reported private imports is restored.

Closes #783

## What was wrong

Seven import sites reached across module boundaries for `_`-prefixed names, contradicting the public-name rule already enforced for `errors/` and `tools/dashboard/`:

- `src/nextdns_mcp/generative.py:31` (`_validate_chart_placement`)
- `src/nextdns_mcp/utils.py:17` (`_BOOL_FALSE_TOKENS`, `_BOOL_TRUE_TOKENS`)
- `src/nextdns_mcp/transport.py:46` (`_log_safe_error`, `_redacted`)
- `src/nextdns_mcp/lifespan.py:29` (`_download_registry`)
- `src/nextdns_mcp/spec_tools.py:41-52` (spec argument, description, series, validation and transport helpers)
- `src/nextdns_mcp/tool_search.py:47-64` (answer, ranking and call-shape helpers)
- `src/nextdns_mcp/call_log/record.py:18-19` (`_result_evidence`, `_project_result`, `_result_text`)

The private name in the importing module was the only record of the coupling; renaming the definition in the owning module would break the importer at import time.

## What changed

- Each reported helper is now defined under its public name in the owning module (for example `validate_chart_placement`, `api_request_payload`, `result_evidence`, `CatalogWords`); the reported importers and the tests that patched or called those names were moved to the public name. `_words` became `comparable_words` in `tool_search_rank` because `words` is already a local variable there.
- Private aliases remain only where an import site this change does not move still binds the private name: `client` (used by `tools/doh.py`, `tools/logs/tool.py`, `health.py`), `tool_search_rank` (used by `tool_search_shape.py`), `tools/logs/retention.py` (used by the `tools.logs` compatibility re-export), and `profile_access` (used by `config`'s compatibility re-export block). `config` and `tools.logs` additionally re-export the public names for the corrected consumers. No alias is an import, so the boundary rule is unaffected.
- `tests/unit/test_private_import_boundaries.py` parses the seven importers with `ast`, resolves relative imports (including deferred ones), and fails if one of the reported private names is imported from its reported source again. It also checks that each public name is the owning module's own object and is bound by the importer. A temporary restoration of a private import was confirmed to fail both tests.
- Docstrings that named the old transport helper were updated. The wider scan's other private import sites, and the compatibility re-export blocks at `config.py`, `tool_search.py` and `tools/logs/__init__.py`, were deliberately left alone.

## How it was verified

At the pushed head:

- `uv run ruff check src tests` — All checks passed.
- `uv run ruff format --check src tests` — 138 files already formatted.
- `uv run mypy src` — Success: no issues found in 64 source files.
- `uv run pytest tests/unit -q` — 2199 passed, 4 skipped.
- `uv run xenon --max-absolute A --max-modules A --max-average A src/ scripts/run_container_e2e.py` — passed.
- `git diff --check` — clean.
- Boundary guard negative check: temporarily restoring `from .chart_placement import _validate_chart_placement` in `generative.py` failed both boundary tests; restoring the public import made them pass again.

## Risk

This is an internal rename with no MCP behaviour change: tool registration, payloads, logging and error contracts are untouched. The retained aliases keep previously importable private names working for the call sites outside this change's scope; they can be removed when those call sites are migrated. A private import reintroduced at one of the seven sites is caught by the new test. Rollback is a revert of this commit.
