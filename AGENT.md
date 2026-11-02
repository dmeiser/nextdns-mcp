# AGENT.md — Rules and behaviors for AI agents

This file contains repository-specific agent rules. Agents should follow these when making changes.

## CRITICAL GIT RULES

- **NEVER push directly to main branch** - All changes MUST go through pull requests
- **NEVER use `git push origin main`** - This bypasses branch protection and CI/CD
- **NEVER approve PRs** - PR approval requires human judgment and review
- **NEVER merge PRs** - PR merging is a human decision with accountability
- Always work on feature branches and create PRs for review
- Let humans review, approve, and merge PRs
- Let GitHub Actions workflows validate changes before merging

## Project Guidelines

- Purpose: implement an MCP server for NextDNS API in Python using the `fastmcp` library, containerized with Docker.
- Keep changes minimal and self-contained. Prefer adding new files rather than editing many unrelated files.
- Use the `fastmcp` library to build the MCP server from the tools `spec_tools` generates out of the bundled OpenAPI document with FastMCP's OpenAPI parser. Do NOT use `FastMCP.from_openapi()`: its generated tools issue API calls through their own transport, bypassing our `AccessControlledClient` — the per-profile ACLs, URL redaction, and typed error payloads (issues #141/#146).
- Configuration should be environment-variable first. Example: `NEXTDNS_API_KEY` for API access.
- Provide a readiness endpoint at `/health`: it probes the NextDNS API with the configured API key and returns `200 OK` with `{ "status": "ok" }` only when that probe succeeds; otherwise it returns `503` with `{ "status": "error", "class": "auth" | "unreachable", "reason": "..." }`. The probe uses a 5 second timeout, bypasses profile access control so a local ACL denial cannot mask the upstream auth state, and its result is cached for 30 seconds.
- Tests: add pytest-based unit tests for new functionality and run them with `uv run pytest`.
  - See "Code Quality Standards" section below for coverage requirements and quality metrics
- Docker: provide a `Dockerfile` (primary, `python:3.14-slim`) and `Dockerfile.alpine` (Alpine variant) that produce small, runnable images.
- **Write Operation Safety Rules:**
  - Write scoping is enforced by the environment: `NEXTDNS_WRITABLE_PROFILES` limits writes to the listed profile ids (unset means writes are denied), the special value `ALL` removes per-profile scoping and grants writes to every profile in the account, production ones included, so it belongs only to a dev account, and `NEXTDNS_READ_ONLY=true` denies every write. See `docs/safety.md`.
  - Always verify the target profile ID before any write operation
- **Development Workflow:**
  - The server is built by `build_mcp_server()` (src/nextdns_mcp/server.py): `spec_tools` registers one tool per operation of the shipped OpenAPI document, and the two operations the document cannot describe (`plotAnalytics`, `dohLookup`) are registered beside them; `create_mcp_server()` (src/nextdns_mcp/openapi.py) creates the `FastMCP` instance whose lifespan (`_server_lifespan` in that module) closes the two long-lived singletons on shutdown — the shared authenticated API client (`client.close_api_client()`) and the DoH client (`tools.doh.close_doh_client()`) — so any new long-lived client must be closed there too.
  - Importing `src/nextdns_mcp/server.py` must stay side-effect free: no `load_dotenv()`, no environment mutation, and no module-level server instance. Call `configure()` (as `python -m nextdns_mcp.server` and the tests do) to build the shared server and disable FastMCP's update check. Enforced by `tests/unit/test_server_dotenv.py`.
  - `src/nextdns_mcp/nextdns-openapi.yaml` is the tool contract: `spec_tools` generates every published tool from it, and `scripts/validate_schema.py` and `scripts/run_container_e2e.py` derive the expected catalog and their verification sweeps from the same document. Why this is the design, and what the exceptions are, is recorded in `docs/architecture/0001-openapi-spec-generated-tools.md` — read it before adding a tool by hand, withholding an operation, or normalizing an argument in the generator.
  - `assert_operation_coverage()` (`scripts/validate_schema.py`) checks catalog-to-document drift in both directions, and the container E2E run calls it at startup and hard-fails on a mismatch: an operation the document gained without the catalog publishing it, or a catalog entry the document cannot generate, silently skips validation otherwise. `streamLogs` is the one documented operation that is deliberately not published (an SSE stream cannot answer a request/response tool call). See `scripts/README.md`.
  - The fastmcp library handles MCP protocol implementation, routing, and tool registration
  - Each hand-written tool in `src/nextdns_mcp/tools/` — the two composites, `plotAnalytics` and `dohLookup` — keeps a thin wrapper over a private `_impl` so the MCP-facing signature and docstring stay separate from the request logic. The wrapper must re-declare the exact parameter list of its `_impl` and forward every argument **by keyword**; a positional forward can bind to the wrong parameter silently. Both invariants are exercised by `tests/unit/test_plotting.py` and `tests/unit/test_doh_lookup.py`, so register any new hand-written tool there. The generated tools have no wrapper: the document is their signature.
  - A tool that resolves a profile id with `resolve_profile_id()` and then builds a request URL must fail closed: when resolution returns no id and no error, return an `internal_error` payload instead of narrowing with `assert`, which `python -O` strips and which would let a literal `/profiles/None/...` URL be built. The two composites are the sites that resolve an id today, and their `internal_error` paths are asserted by `tests/unit/test_plotting.py` and `tests/unit/test_doh_lookup.py`; add any new tool that narrows a resolved profile id to that pair. The neighbouring guards live at the transport and generator: `tests/unit/test_access_controlled_client.py` and `tests/unit/test_access_control.py` (a URL whose profile cannot be classified is denied rather than fetched), and `tests/unit/test_spec_tools.py` and `tests/unit/test_error_contract.py` (a missing or unsafe id on a generated tool is a typed payload, never a request).
- **Tool API error handling (issue #181):**
  - Tools must call `_api_request_payload()` (`src/nextdns_mcp/transport.py`); it awaits `_api_request()` and converts the typed `NextDNSError` hierarchy (auth 401/403, rate limit 429, server 5xx, each carrying `status_code`/`response_body`) into the standardized error payloads from `src/nextdns_mcp/errors.py`.
  - `_api_request()` RAISES for upstream failures and only RETURNS the access-control denial payload, so a new call site that awaits it directly would leak an exception instead of the error payload. A caller that needs the raw dict (e.g. `plots._fetch_series_payload`) must convert with `_handle_api_error()` and still detect the returned denial `code`.
  - Upstream reports failures as a plural `{"errors": [{"code": ...}]}` document, and returns it behind a 4xx for most endpoints but behind a **2xx** for some (observed: `POST /allowlist` on an existing entry). `_api_request()` normalizes both into the same typed failure, so a 2xx error document can never be returned as if it were a data payload. `http_error_payload()` copies the document's codes into `upstream_code`/`upstream_codes`, so callers branch on those values instead of parsing the inlined body.
  - `spec_tools` keeps the guardrails whose traps fail silently upstream rather than erroring in a way a caller can correct: time-shaped query arguments are rewritten into the form the API reads (`_QUERY_NORMALIZERS` — `from=-86400` answers 200-empty because it reads as 1969, `now-1d` answers 400), and a blank path value stays the template's placeholder instead of substituting, because an empty `profile_id` mid-URL 404s blaming the wrong thing. Add new silent-trap rewrites to the normalizers, not to tool prose.
  - `StripExtraFieldsMiddleware` (`src/nextdns_mcp/openapi.py`) deliberately drops unknown *top-level* tool arguments and coerces string-typed values, but it must never drop silently: the result carries `ignored_arguments` plus a `hint` naming what was not applied. A silent drop is how a model ends up believing a call that carried `privacy={...}` changed privacy settings. Keep any new relaxation reportable.
- **Replacing list contents:**
  - Full-list replacement is its own endpoint tool per collection (`replacePrivacyBlocklists` and friends): it takes `profile_id` and the entry array as the one `body` argument the document describes for that endpoint.
  - Generated tools take typed parameters — the endpoint's path, query and body fields flattened one level — not raw request bodies; an array-shaped body is the document's own one argument, with its schema.
- When in doubt, ask the repo owner for permission before making large design changes.
- API Key: Ensure that a valid API key is not in any files that will be committed to git.
- Logging: no log record at INFO or above may contain a query string or httpx's merged-URL exception text; use `_redacted()` / `_log_safe_error()` from `src/nextdns_mcp/client.py` at any new log site, and keep the `httpx`/`httpcore` loggers at WARNING. See docs/safety.md.

## Middleware order invariants

`create_mcp_server()` (`src/nextdns_mcp/openapi.py`) assembles a middleware chain whose order is load-bearing. FastMCP applies the chain so that **the middleware added first runs outermost**; the add order below therefore reads as the execution order, outside in.

1. `CallToolShapeMiddleware` (`src/nextdns_mcp/tool_search.py`, added only under disclosure) — repairs the `call_tool` call shape: resolves a name out of a whole search answer, normalizes a mistyped name, and lifts tool parameters that were sent beside `name` back into `arguments`.
2. `StripExtraFieldsMiddleware` (`src/nextdns_mcp/openapi.py`) — drops top-level arguments the target tool's published schema does not declare (reporting them as `ignored_arguments`) and coerces obvious string spellings of typed fields.
3. `CallLogMiddleware` (`src/nextdns_mcp/call_log.py`, added only when `NEXTDNS_CALL_LOG` is set) — records the call that actually executed.

Why exactly that order:

- Shape repair must be added **before** the extra-field filter. The misplaced parameters it exists to rescue are exactly the keys the filter would drop as unknown, so a filter running first would destroy the evidence the repair needs.
- Shape repair must be added **before** the call log, so the log records the resolved tool name and the repaired arguments rather than the malformed call the caller sent.
- The synthetic `call_tool` **re-enters the whole chain**: it dispatches through the server's own `call_tool`, which re-applies every middleware to the resolved target tool. That is why `call_log._SYNTHETIC_TOOLS` skips `search_tools`/`call_tool` — without the skip, one disclosed call would be recorded twice — and why the outer relaxation pass must be keyed on the resolved target rather than on the wrapper.
- The invariant a FastMCP bump must preserve: **exactly one call-log row per executed call**, under the real tool name, carrying the arguments the tool actually received. The disclosure-proxy row test in `tests/unit/test_call_log.py` is what fails first if FastMCP changes how `call_tool` dispatches. Treat that failure as a chain-order question, not a logging bug.

## FastMCP integration surface

These are the FastMCP internals this server leans on. `fastmcp` is version-pinned and Dependabot-bumped, so every bump should re-check this list; a rename in any of them fails at request time rather than at import, and mypy will not see most of it.

| Borrowed from | What we do with it | Invariant it carries |
|---|---|---|
| `BM25SearchTransform._search` | overridden to score the catalog our way (`src/nextdns_mcp/tool_search.py`) | The hook's signature, and that results arrive in the order it returns |
| `BM25SearchTransform._make_search_tool` / `_make_call_tool` | overridden to re-describe the synthetic pair | The synthetic `search_tools` + `call_tool` pair is still built through these hooks |
| `BaseSearchTransform._max_results` | read by our `_search` (declared on the subclass, set by the base `__init__`) | The attribute still exists when a search runs |
| `BaseSearchTransform.get_tool_catalog` | the shape middleware's view of the callable catalog, through `MiddlewareContext.fastmcp_context` | The catalog the proxy dispatches against is the one this returns |
| `search_result_serializer` | our `_compact_search_results` bounds one search answer | The hook is still consulted for every answer |
| Middleware list | first-added runs outermost; order is fixed in `create_mcp_server` | See "Middleware order invariants" above |
| `ctx.fastmcp.call_tool` | the synthetic proxy's dispatch, which re-enters the whole chain | One dispatch per executed call, so one call-log row and no double relaxation |
| `Tool.run` | `SpecApiTool` subclasses `Tool` and overrides `run` entirely (`src/nextdns_mcp/spec_tools.py`) | `run(arguments)` returns a `ToolResult` and never raises for an expected failure |
| `parse_openapi_to_http_routes`, `route.flat_param_schema`, `route.parameter_map` | the generator's whole view of the document | A flat one-level parameter schema, and a parameter map that reports an unknown argument as absent rather than guessed |
| `FastMCP(transforms=...)`, `add_middleware` | wiring disclosure, relaxation, and the call log | Transforms still apply to `tools/list`, and middleware still wraps every tool call |

When a bump touches any of these: run the unit suite and the container E2E, and re-read the
`# type: ignore` suppressions in `src/nextdns_mcp/tool_search.py` — they are suppressed
because the hooks are private, so a rename there is silent.

## Testing Strategy

1. **Unit Tests** (`tests/unit/`)
   - Fast, isolated tests with mocked dependencies
   - Test individual functions and modules
   - Run frequently during development
   - See "Code Quality Standards" section for coverage requirements
   - Must achieve 100% code coverage

2. **Container E2E** (`scripts/run_container_e2e.py`, run in CI by `.github/workflows/e2e-container.yml`)
   - The integration layer: builds and runs the container and exercises the real MCP endpoint
   - Verifies tool registration over the wire and the `/health` readiness endpoint
   - `tests/integration/` holds no tests; server creation and tool registration are covered by the unit suite (`tests/unit/test_mcp_server.py`). Spec-based tool generation is how this server is built, not an experiment: `spec_tools` loads the shipped document (`src/nextdns_mcp/nextdns-openapi.yaml`) at startup and registers one tool per operation from it, and the same document is what `scripts/validate_schema.py` and the container run verify the served catalog against. The decision and its limits are recorded in `docs/architecture/0001-openapi-spec-generated-tools.md`.

Merging policy: small, incremental PRs. Preserve existing README/CI content; when adding new top-level files, update README to reflect run/test/build instructions.

## Code Quality Standards

All code changes must meet the following quality metrics before work is considered complete. These standards ensure maintainability, reliability, and consistency across the codebase.

### 1. Code Formatting and Type Checking

**CRITICAL**: Code formatting and type checking must be the **final step** before validation:

1. Run `ruff` to check and fix lint issues:
   ```bash
   uv run ruff check --fix src/ tests/
   ```

2. Run `ruff format` to format code:
   ```bash
   uv run ruff format src/ tests/
   ```

3. Run `mypy` for type checking:
   ```bash
   uv run mypy src/
   ```

**Workflow Order**:
- Make code changes
- Write/update tests
- Run formatters: `ruff check --fix` → `ruff format`
- Run type checker: `mypy` (fix any errors)
- Run tests and validation
- **If any code changes are needed after validation, repeat the formatting steps**

The last commits before a successful validation run MUST be formatting/type-checking changes only.

### 2. Unit Test Coverage Requirements

**CRITICAL**: All unit tests must pass with 100% success rate. Failing tests are NEVER acceptable.

**Minimum Coverage Standards**:
- **Project-wide**: 100% code coverage
- **Per-file**: No single file may have <100% coverage
- **Branch**: The 100% target covers statements *and* branches (`[tool.coverage.run] branch = true` in `pyproject.toml`). A statement-only report cannot see an untested branch, and the `Cover` column is rounded to whole percent, so it can read `100%` while a branch is untaken: check that the `Miss` and `BrPart` columns are `0`, as the CI gate does.
- **Exceptions**: Only for truly untestable code (e.g., `if __name__ == "__main__"`, module-level `sys.exit()`). Every exception must be covered by an explicit `# pragma: no cover` with a comment explaining why, or `# pragma: no branch` for a branch that cannot be taken.

**Running Coverage**:
```bash
# Generate coverage report
uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term-missing --cov-report=html

# View HTML report
open htmlcov/index.html
```

**Coverage Validation**:
- Check the `Miss` and `BrPart` columns in the terminal output, not just the rounded `Cover` percentage (see the Branch standard above)
- Review HTML report for per-file coverage
- Ensure no file falls below 100% statements and branches
- Document any intentional gaps with inline comments explaining why they're untestable
- **All tests must pass** - zero failures, zero errors

### 3. Cyclomatic Complexity Standards

**The gate is `xenon`, and it requires grade A everywhere** — every block, every module, and the project average:

```bash
uv run xenon --max-absolute A --max-modules A --max-average A src/ scripts/run_container_e2e.py
```

- Grade A means cyclomatic complexity ≤5, so no block may sit at grade B or worse. The `complexity` job in `.github/workflows/unit-tests.yml` runs exactly that command, and `tests/unit/test_complexity_gate.py` executes the workflow's own step against throwaway trees so the documented contract and CI cannot drift.
- **What the contract covers:** `src/` and `scripts/run_container_e2e.py`. The rest of `scripts/` (`ai_e2e_harness.py`, `ai_e2e_common.py`, `validate_schema.py`) is **outside the gate** and contains blocks far above grade A. A green `complexity` job is not a claim that those files comply, and widening the gate's path list is a refactor project of its own, not a documentation change.
- `radon` stays the local diagnostic — it names and ranks the blocks the way the gate summarizes them:
  ```bash
  uv run radon cc src/ scripts/run_container_e2e.py -a    # average
  uv run radon cc src/ scripts/run_container_e2e.py -nb   # every block ranked B or worse
  ```
- If a block exceeds grade A:
  - Refactor into smaller functions
  - Table-drive repeated branches instead of adding another conditional
  - Use early returns to reduce nesting

**Complexity Grading Scale** (the radon grades the gate's `--max-absolute A` is stated in):
- A: 1-5 (simple, low risk) ✅ The only allowed grade
- B: 6-10 (more complex, moderate risk) ❌ Not allowed
- C: 11-20 (complex, high risk) ❌ Not allowed
- D: 21-30 (very complex, very high risk) ❌ Not allowed
- E: 31-40 (extremely complex, extreme risk) ❌ Not allowed
- F: 41+ (unmaintainable, extreme risk) ❌ Not allowed

### 4. Pre-Commit Quality Checklist

Before claiming work is complete:

- [ ] Run `uv run ruff check --fix src/ tests/`
- [ ] Run `uv run ruff format src/ tests/`
- [ ] Run `uv run mypy src/` (0 errors)
- [ ] Run `uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term` (100% statements and branches, **ALL tests pass**)
- [ ] Verify per-file coverage: all files 100% in `htmlcov/index.html` (statements and branches; see section 2)
- [ ] Run the complexity gate locally: `uv run xenon --max-absolute A --max-modules A --max-average A src/ scripts/run_container_e2e.py` (every block grade A; the rest of `scripts/` is outside this gate — see section 3)
- [ ] Optionally confirm the report with `uv run radon cc src/ scripts/run_container_e2e.py -nb` (it names the blocks the gate fails on)
- [ ] Commit formatting changes as final commit before validation

**CRITICAL**: If ANY check fails, fix the issues and restart from step 1. Continue iterating through all quality checks until every standard is met with zero failures.

### 5. Quality Tools Configuration

**ruff** (code formatting and linting):
- Line length: 120 characters
- Target version: Python 3.12
- Configuration in `pyproject.toml`:
  ```toml
  [tool.ruff]
  line-length = 120
  target-version = "py312"
  ```

**mypy** (type checking):
- Strict mode recommended
- Add type hints to all function signatures
- Use `typing` module for complex types

**xenon** (complexity gate) - the machine contract for complexity:
- Installed as a dev dependency
- CI runs `uv run xenon --max-absolute A --max-modules A --max-average A src/ scripts/run_container_e2e.py` in the `complexity` job of `.github/workflows/unit-tests.yml`: every block in those paths, every module, and the average must be grade A
- The gate's path list is part of the contract: it covers `src/` and `scripts/run_container_e2e.py` and nothing else under `scripts/` (see section 3)

**radon** (complexity analysis) - the same measurement, for reading locally:
- Installed as a dev dependency
- Use `cc` (cyclomatic complexity) command
- Use `-a` flag for average complexity
- Use `-nb` flag to list every block ranked B or worse (i.e. anything the gate would reject)
- Diagnostic only: CI fails on xenon's verdict, not on a radon report

### 6. Handling Quality Failures

**CRITICAL**: Code quality is non-negotiable. If any check fails, the quality pipeline must be rerun until ALL standards are met.

**Failure Response Process**:

1. **ruff failures**: Should auto-fix, re-run all subsequent checks
2. **mypy failures**: Add type hints, fix type errors, document `# type: ignore` if absolutely necessary, then restart quality checks
3. **Test failures (unit or integration)**: 
   - Debug and fix the failing test or code
   - **NEVER ignore, skip, or comment out failing tests**
   - Restart quality checks from step 1 after fixes
4. **Coverage <100%**: Add missing test cases, remove dead code, or document why code is untestable with the exclusion markers listed in section 2, then restart quality checks
5. **Complexity above grade A**: Refactor the block into smaller units, table-drive its branches, simplify the logic, then restart quality checks. Only `src/` and `scripts/run_container_e2e.py` are gated; if the violation is in another `scripts/` file, note it rather than silently widening the gate

**Iteration Loop**:
- Fix the issue identified
- Run `ruff` → `mypy`
- Run all unit tests with coverage
- Run all integration tests
- If any check fails, repeat the loop
- Continue until 100% of quality standards are met

**Never skip quality checks** — they catch bugs before production and ensure code maintainability. Failing tests indicate broken code that must be fixed, not ignored.
