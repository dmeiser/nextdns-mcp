# AGENTS.md — Rules and behaviors for AI agents

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
- Use the `fastmcp` library to build the MCP server, specifically using the `from_openapi` function to generate the server from OpenAPI/Swagger documentation.
- Configuration should be environment-variable first. Example: `NEXTDNS_API_KEY` for API access.
- Provide a minimal health endpoint at `/health` returning 200 OK and JSON `{ "status": "ok" }`.
- Tests: add pytest-based unit tests for new functionality and run them with `uv run pytest`.
  - See "Code Quality Standards" section below for coverage requirements and quality metrics
- Docker: provide a `Dockerfile` that produces a small, runnable image; prefer using official `python:3.13-slim` as base.
- Keep `TODO.md` progress indicators in sync with the current phase while executing tasks.
- **Write Operation Safety Rules:**
  - When running Gateway E2E tests, set `NEXTDNS_WRITABLE_PROFILES=ALL` (or specific test profile ID)
  - Write operations (create, update) are only allowed against designated test profiles
  - Always verify the target profile ID before any write operation
  - **Gateway E2E Test Safety:**
    - E2E tests can create a dedicated validation profile or use existing test profile
    - All write operations execute via Docker MCP Gateway CLI
    - Set `ALLOW_LIVE_WRITES=true` to enable write operations (default: read-only)
    - Profile cleanup is optional and user-controlled
    - Tests produce JSONL reports in `scripts/artifacts/tools_report.jsonl`
- **Gateway E2E Testing Requirements:**
  - **CRITICAL**: E2E tests MUST use Docker MCP Gateway CLI, NOT direct Python calls
  - Test script locations: `scripts/gateway_e2e_run.{sh,ps1}`
  - E2E tests verify all MCP tools work through the Docker MCP Gateway
  - **Test Structure:**
    1. Build Docker image with latest code
    2. Import MCP server into Docker MCP Gateway
    3. Start gateway container with environment configuration
    4. Execute all tools via `docker mcp tools call` (or in-container `mcp` binary)
    5. Produce machine-readable JSONL report
    6. Optional: Clean up validation profile
  - **How to Run E2E Tests:**
    - Set environment variables in `.env` file (see `.env.example`)
    - Required: `NEXTDNS_API_KEY`, `NEXTDNS_READABLE_PROFILES`, `NEXTDNS_WRITABLE_PROFILES`
    - Optional: `ALLOW_LIVE_WRITES=true` (default: read-only mode)
  - **When to Run E2E Tests:**
    - When fixing bugs or adding features that affect any MCP tool
    - Before reporting completion of any API changes
    - After OpenAPI spec updates that add/modify operations
    - Before major releases or production deployment
  - **Running E2E Tests:**
    ```bash
    # PowerShell (Windows)
    cd scripts
    .\gateway_e2e_run.ps1
    
    # Bash (Linux/macOS)
    cd scripts
    ./gateway_e2e_run.sh
    ```
  - **Analyzing Results:**
    - Check `scripts/artifacts/tools_report.jsonl` for per-tool results
    - Parse JSONL to identify failures: `jq 'select(.exit_code != 0)' tools_report.jsonl`
    - Review stdout/stderr for error details
    - **Required: 100% pass rate** - ALL tools must pass
  - If validation fails, fix the issues and re-run until all tests pass
  - Document validation failures and fixes in your response
  - Fix Docker CLI parameter handling issues (use proper JSON encoding for typed parameters)
- **Development Workflow:**
  - Phase 1: Create complete and accurate OpenAPI/Swagger documentation for the NextDNS API
  - Phase 2: Use `fastmcp.from_openapi()` to generate the MCP server from the OpenAPI spec
  - All NextDNS API endpoints should be documented in the OpenAPI spec before server generation
  - The fastmcp library will handle MCP protocol implementation, routing, and tool registration
- When in doubt, ask the repo owner for permission before making large design changes.
- API Key: Ensure that a valid API key is not in any files that will be committed to git.

## Testing Strategy

### Two-Tier Testing Approach

1. **Unit Tests** (`tests/unit/`)
   - Fast, isolated tests with mocked dependencies
   - Test individual functions and modules
   - Run frequently during development
   - See "Code Quality Standards" section for coverage requirements
   - Must achieve >95% code coverage

2. **Gateway E2E Tests** (`scripts/gateway_e2e_run.{sh,ps1}`)
   - End-to-end testing via Docker MCP Gateway CLI
   - Tests actual user workflow: `docker mcp tools call <tool> <params>`
   - Verifies CLI parameter parsing, quoting, and execution
   - Produces machine-readable JSONL reports
   - See "Gateway E2E Testing Requirements" section above for complete details
   - Run before major releases or when troubleshooting production issues
   - **Required pass rate: 100%** - ALL tools must pass E2E validation

Merging policy: small, incremental PRs. Preserve existing README/CI content; when adding new top-level files, update README to reflect run/test/build instructions.

## Code Quality Standards

All code changes must meet the following quality metrics before work is considered complete. These standards ensure maintainability, reliability, and consistency across the codebase.

### 1. Code Formatting and Type Checking

**CRITICAL**: Code formatting and type checking must be the **final step** before validation:

1. Run `isort` to organize imports:
   ```bash
   uv run isort src/ tests/
   ```

2. Run `black` to format code:
   ```bash
   uv run black src/ tests/
   ```

3. Run `mypy` for type checking:
   ```bash
   uv run mypy src/
   ```

**Workflow Order**:
- Make code changes
- Write/update tests
- Run formatters: `isort` → `black`
- Run type checker: `mypy` (fix any errors)
- Run tests and validation
- **If any code changes are needed after validation, repeat the formatting steps**

The last commits before a successful validation run MUST be formatting/type-checking changes only.

### 2. Unit Test Coverage Requirements

**CRITICAL**: All unit tests must pass with 100% success rate. Failing tests are NEVER acceptable.

**Minimum Coverage Standards**:
- **Project-wide**: >95% code coverage
- **Per-file**: No single file may have <95% coverage
- **Exceptions**: Only for truly untestable code (e.g., `if __name__ == "__main__"`, module-level `sys.exit()`)

**Running Coverage**:
```bash
# Generate coverage report
uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term-missing --cov-report=html

# View HTML report
open htmlcov/index.html
```

**Coverage Validation**:
- Check overall percentage in terminal output
- Review HTML report for per-file coverage
- Ensure no file falls below 95%
- Document any intentional gaps with inline comments explaining why they're untestable
- **All tests must pass** - zero failures, zero errors

### 3. Gateway E2E Test Requirements

**CRITICAL**: Gateway E2E tests validate production-like behavior via Docker MCP CLI. **ALL tests must pass with 100% success rate.**

**Running E2E Tests**:
```bash
# PowerShell
cd scripts
.\gateway_e2e_run.ps1

# Bash
cd scripts
./gateway_e2e_run.sh
```

**E2E Test Validation**:
- **Required pass rate: 100%** - ALL tools must pass
- Review `scripts/artifacts/tools_report.jsonl` for detailed results
- NO failures are acceptable - fix Docker CLI parameter handling issues
- If tests fail due to parameter type conversion, fix the E2E scripts to use proper JSON encoding
- See "Gateway E2E Testing Requirements" section for safety rules

### 4. Cyclomatic Complexity Standards

**Project Complexity**: Grade A
- Measured using `radon` tool
- Project average complexity must be grade A
- Run: `uv run radon cc src/ -a`

**Function Complexity**: Maximum Grade B
- No individual function may exceed grade B (cyclomatic complexity ≤11)
- Check with: `uv run radon cc src/ -nc`
- If a function exceeds grade B:
  - Refactor into smaller functions
  - Extract complex conditional logic
  - Use early returns to reduce nesting

**Complexity Grading Scale** (Radon):
- A: 1-5 (simple, low risk)
- B: 6-11 (more complex, moderate risk)
- C: 11-20 (complex, high risk) ❌ Not allowed
- D: 21-50 (very complex, very high risk) ❌ Not allowed
- F: 51+ (extremely complex, extreme risk) ❌ Not allowed

### 5. Pre-Commit Quality Checklist

Before running E2E tests or claiming work is complete:

- [ ] Run `uv run isort src/ tests/`
- [ ] Run `uv run black src/ tests/`
- [ ] Run `uv run mypy src/` (0 errors)
- [ ] Run `uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term` (>95% coverage, **ALL tests pass**)
- [ ] Verify per-file coverage: all files >95% in `htmlcov/index.html`
- [ ] Run `uv run radon cc src/ -a` (verify grade A)
- [ ] Run `uv run radon cc src/ -nc` (verify no functions exceed grade B)
- [ ] Run Gateway E2E tests: `cd scripts && ./gateway_e2e_run.{sh,ps1}` (**100% pass rate required**)
- [ ] Review `scripts/artifacts/tools_report.jsonl` - zero failures allowed
- [ ] Commit formatting changes as final commit before validation

**CRITICAL**: If ANY check fails, fix the issues and restart from step 1. Continue iterating through all quality checks until every standard is met with zero failures.

### 6. Gateway E2E Test Troubleshooting

**E2E Tests MUST Achieve 100% Pass Rate**

All Gateway E2E tests must pass. If tests fail, fix the underlying issues - do not accept failures as "known limitations."

**Common E2E Failure Patterns and Fixes**:

1. **Parameter Type Errors** (Boolean/Integer as String)
   - Symptom: `HTTP 400: /enabled must be boolean`
   - Cause: Docker CLI passes `enabled=true` as string `"true"` instead of boolean
   - **Fix Required**: Update E2E scripts to encode parameters as proper JSON
   - Example: Instead of `enabled=true`, pass `--param '{"enabled": true}'` or use JSON file
   - Status: **MUST BE FIXED** - not acceptable

2. **Duplicate Errors** (HTTP 400: duplicate)
   - Symptom: `{'errors': [{'code': 'duplicate'}]}`
   - Cause: Profile already has the item being added
   - **Fix Required**: 
     - Use fresh validation profile for each test run
     - Clear lists before adding items (check if exists, remove first)
     - Use unique test data (e.g., timestamped domains)
   - Status: **MUST BE FIXED** - tests must be idempotent

3. **Missing Required Parameters**
   - Symptom: `HTTP 400: missing required parameter`
   - Cause: E2E script doesn't pass all required parameters
   - **Fix Required**: Review OpenAPI spec, update script to include all required params
   - Status: **MUST BE FIXED** - ensure complete parameter coverage

4. **Output Validation Errors**
   - Symptom: Tool succeeds but Docker reports schema mismatch
   - Cause: MCP tool returns different format than expected
   - **Fix Required**: 
     - Verify MCP tool output format matches OpenAPI spec
     - Update OpenAPI response schema if needed
     - Fix MCP tool implementation if output is incorrect
   - Status: **MUST BE FIXED** - output must match spec

**Fixing Parameter Type Issues**:

The E2E scripts currently pass parameters as simple key=value strings. To achieve 100% pass rate, update parameter handling:

```bash
# WRONG (causes type errors)
docker mcp tools call updateBlockPageSettings profile_id=abc123 enabled=true

# RIGHT (proper JSON encoding)
docker mcp tools call updateBlockPageSettings --param '{"profile_id": "abc123", "enabled": true}'

# OR use JSON file
echo '{"profile_id": "abc123", "enabled": true}' > params.json
docker mcp tools call updateBlockPageSettings --param-file params.json
```

**Action Items for 100% Pass Rate**:
1. Update `run_all_tools.{sh,ps1}` to encode all parameters as JSON
2. Handle type conversion for booleans, integers, and arrays
3. Implement idempotent test data (check-before-add pattern)
4. Verify all required parameters are passed for each tool
5. Ensure output formats match OpenAPI spec definitions

**Quality Gate**: Zero failures allowed. All 76+ tools must pass E2E validation.

### 7. Quality Tools Configuration

**isort** (import sorting):
- Configured in `pyproject.toml` under `[tool.isort]` (if present)
- Use defaults if no configuration exists
- Ensures consistent import organization

**black** (code formatting):
- Line length: 100 characters
- Target version: Python 3.13
- Configuration in `pyproject.toml`:
  ```toml
  [tool.black]
  line-length = 100
  target-version = ["py313"]
  ```

**mypy** (type checking):
- Strict mode recommended
- Add type hints to all function signatures
- Use `typing` module for complex types

**radon** (complexity analysis):
- Installed as dev dependency
- Use `cc` (cyclomatic complexity) command
- Use `-a` flag for average complexity
- Use `-nc` flag to show only functions above grade B

### 7. Handling Quality Failures

**CRITICAL**: Code quality is non-negotiable. If any check fails, the quality pipeline must be rerun until ALL standards are met.

**Failure Response Process**:

1. **isort/black failures**: Should auto-fix, re-run all subsequent checks
2. **mypy failures**: Add type hints, fix type errors, document `# type: ignore` if absolutely necessary, then restart quality checks
3. **Test failures (unit or integration)**: 
   - Debug and fix the failing test or code
   - **NEVER ignore, skip, or comment out failing tests**
   - Restart quality checks from step 1 after fixes
4. **Coverage <95%**: Add missing test cases, remove dead code, or document why code is untestable, then restart quality checks
5. **Complexity >B**: Refactor function into smaller units, extract methods, simplify logic, then restart quality checks

**Iteration Loop**:
- Fix the issue identified
- Run `isort` → `black` → `mypy`
- Run all unit tests with coverage
- Run all integration tests
- If any check fails, repeat the loop
- Continue until 100% of quality standards are met

**Never skip quality checks** — they catch bugs before production and ensure code maintainability. Failing tests indicate broken code that must be fixed, not ignored.
