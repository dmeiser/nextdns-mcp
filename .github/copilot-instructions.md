````instructions
# NextDNS MCP Server - AI Agent Instructions

Essential knowledge for AI agents working on this FastMCP-based NextDNS API server.

## 0. CRITICAL GIT RULES

**NEVER push directly to main branch!**

- ❌ NEVER use `git push origin main`
- ❌ NEVER bypass pull requests
- ❌ NEVER approve PRs (`gh pr review --approve`)
- ❌ NEVER merge PRs (`gh pr merge`)
- ✅ ALWAYS work on feature branches
- ✅ ALWAYS create PRs for all changes
- ✅ Let humans review and approve PRs
- ✅ Let humans merge PRs after approval
- ✅ Let GitHub Actions validate changes before merging

Pushing to main bypasses branch protection, CI/CD validation, and code review.
PR approval and merging are human decisions that require judgment and accountability.

## 1. Architecture: OpenAPI-Driven Tool Generation

**Core Insight**: Server is auto-generated from `src/nextdns_mcp/nextdns-openapi.yaml` via `FastMCP.from_openapi()`. No hand-coded routes.

**To modify API functionality**:
1. Edit OpenAPI YAML (add paths/operations with `operationId`)
2. Server regenerates on next run

**Key file**: `server.py:create_mcp_server()` - loads YAML, creates authenticated `AccessControlledClient`, generates 68+ MCP tools from spec.

## 2. Profile Access Control (New Feature)

**Critical Pattern**: `AccessControlledClient` in `server.py` wraps `httpx.AsyncClient` to enforce per-profile read/write restrictions.

**How it works**:
- Extracts `profile_id` from URL paths (regex: `/profiles/{id}/...`)
- Checks `can_read_profile()` / `can_write_profile()` from `config.py`
- Returns 403 responses for denied access (no API call made)
- Global operations (`listProfiles`, `dohLookup`) bypass checks

**Environment variables** (`config.py`):
```bash
NEXTDNS_READABLE_PROFILES=abc123,def456  # or "ALL" or empty (deny all)
NEXTDNS_WRITABLE_PROFILES=test789        # or "ALL" or empty (deny all)
NEXTDNS_READ_ONLY=true                   # overrides all write permissions
```

**Testing access control**: Gateway E2E tests validate access control scenarios via Docker CLI.

## 3. Array-body Endpoints (FastMCP 3.x)

FastMCP 3.x supports array bodies natively via the `body` parameter. Array-body endpoints are generated directly from OpenAPI.

**Usage pattern**:
- Use `body=[{"id":"value"}]` for list replacement tools (e.g., `replaceDenylist`, `replaceAllowlist`).
- Do **not** use legacy `update*` custom tools (they no longer exist).

## 4. Code Quality Requirements (STRICT)

**ALL checks must pass before completion**. Iterate quality loop until 100% compliant:

1. **Format**: `uv run isort src/ tests/` → `uv run black src/ tests/`
2. **Type check**: `uv run mypy src/` (0 errors)
3. **Unit tests**: `uv run pytest tests/unit --cov=src/nextdns_mcp` (>95% coverage, ALL pass, no file <95%)
4. **Complexity**: `uv run radon cc src/ -a` (grade A), `radon cc src/ -nc` (no function >B)
5. **Integration**: `uv run pytest tests/integration/test_server_init.py` (ALL pass)
6. **Gateway E2E**: `cd scripts && ./gateway_e2e_run.{sh,ps1}` (100% pass rate REQUIRED)

**If ANY check fails**: Fix, rerun formatters, repeat from step 1. Never skip, never ignore failing tests.

**Last commit must be formatting** (isort/black/mypy) before validation.

## 5. Testing Strategy (2-Tier)

### Unit Tests (`tests/unit/`)
- Mock external calls (`AsyncMock` for httpx)
- Example: `test_access_control.py` mocks 403 responses to test access denial logic
- Run frequently during development
- Target: >95% coverage, ALL tests pass

### Integration Tests (`tests/integration/`)
- `test_server_init.py`: Server creation, tool registration with mocked dependencies
- Fast, no network calls, no live API access
- Tests server initialization logic only

### Gateway E2E Tests (`scripts/gateway_e2e_run.{sh,ps1}`)
- **CRITICAL**: Tests ALL 76+ tools via Docker MCP Gateway CLI (production-like)
- Commands: `docker mcp tools call <tool> <params>`
- Creates "Validation Profile [timestamp]" for isolation
- Requires `NEXTDNS_API_KEY` and `NEXTDNS_READABLE_PROFILES=ALL`, `NEXTDNS_WRITABLE_PROFILES=ALL`
- Produces machine-readable JSONL report in `scripts/artifacts/tools_report.jsonl`
- **Required pass rate: 100%** - ALL tools must pass, no failures accepted
- If tests fail, fix Docker CLI parameter encoding (use proper JSON format)

## 6. Configuration Pattern

**Fail-fast validation**: `config.py:validate_configuration()` runs on module import. Server exits if API key missing.

**API key loading order**:
1. `NEXTDNS_API_KEY` env var
2. `NEXTDNS_API_KEY_FILE` (Docker secrets)
3. Exit with error message

**Access control parsing**: `parse_profile_list()` handles `"ALL"`, `"abc,def"`, or empty (deny all).

## 7. Key Files

- `src/nextdns_mcp/nextdns-openapi.yaml`: 2300+ lines, defines all API operations
- `src/nextdns_mcp/server.py`: 650+ lines - `AccessControlledClient`, DoH tool, server creation
- `src/nextdns_mcp/config.py`: 322 lines - env vars, access control logic, constants
- `scripts/gateway_e2e_run.{sh,ps1}`: Gateway E2E test scripts - comprehensive tool validation via Docker CLI
- `scripts/artifacts/tools_report.jsonl`: E2E test results (machine-readable)
- `tests/integration/test_server_init.py`: Server initialization tests (no live API)
- `AGENT.md`: Complete quality standards and workflow rules

## 8. Safety Rules

- **NEVER delete profiles** without explicit user confirmation (except validation test profile)
- **Write operations** only against designated test profiles
- **Test via Docker MCP Gateway CLI** (`docker mcp tools call`), not direct Python calls
- **Run Gateway E2E validation** before claiming completion of API-touching features
- **Access control**: Set `NEXTDNS_READABLE_PROFILES=ALL` and `NEXTDNS_WRITABLE_PROFILES=ALL` for testing
- **E2E testing**: Set `ALLOW_LIVE_WRITES=true` to enable write operations (default: read-only)

## 9. Common Patterns

**Add new OpenAPI endpoint**:
1. Add to `nextdns-openapi.yaml` with unique `operationId`
2. Server auto-generates tool on next run
3. Add unit tests mocking the API call
4. Run Gateway E2E tests to validate (`cd scripts && ./gateway_e2e_run.{sh,ps1}`)

**Add custom tool** (for special logic not covered by OpenAPI):
1. Define `_toolName_impl()` with business logic
2. Add `@mcp.tool()` wrapper returning JSON string
3. Add to `EXCLUDED_ROUTES` in `config.py` if it shadows OpenAPI path
4. Follow pattern in `server.py` lines 250-527

**Debug access denial**:
1. Check logs for "Read/Write access denied for profile: {id}"
2. Verify `NEXTDNS_READABLE_PROFILES` / `NEXTDNS_WRITABLE_PROFILES` env vars
3. Check `config.py:can_read_profile()` / `can_write_profile()` logic
4. Use Gateway E2E tests to validate scenarios (`cd scripts && ./gateway_e2e_run.{sh,ps1}`)

**Debug E2E test failures**:
1. Check `scripts/artifacts/tools_report.jsonl` for failure details
2. Parse failures: `jq 'select(.exit_code != 0)' tools_report.jsonl`
3. Fix parameter type issues: encode arrays as JSON objects in `body` (e.g., `body=[{"id":"value"}]`)
4. Ensure idempotent test data (check-before-add pattern)
5. Verify all required parameters are passed per OpenAPI spec
6. Re-run until 100% pass rate achieved

````
