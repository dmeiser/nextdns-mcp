# AGENTS.md — Rules and behaviors for AI agents

This file contains repository-specific agent rules. Agents should follow these when making changes.

- Purpose: implement an MCP server for NextDNS API in Python using the `fastmcp` library, containerized with Docker.
- Keep changes minimal and self-contained. Prefer adding new files rather than editing many unrelated files.
- Use the `fastmcp` library to build the MCP server, specifically using the `from_openapi` function to generate the server from OpenAPI/Swagger documentation.
- Configuration should be environment-variable first. Example: `NEXTDNS_API_KEY` for API access.
- Provide a minimal health endpoint at `/health` returning 200 OK and JSON `{ "status": "ok" }`.
- Tests: add pytest-based unit tests for new functionality and run them with `poetry run pytest`.
  - See "Code Quality Standards" section below for coverage requirements and quality metrics
- Docker: provide a `Dockerfile` that produces a small, runnable image; prefer using official `python:3.13-slim` as base.
- Keep `TODO.md` progress indicators in sync with the current phase while executing tasks.
- **Write Operation Safety Rules:**
  - When running validation tests, you are permitted to run `tests/integration/test_live_api.py --auto-delete-profile`, which will delete the profile created for the  validation run.
  - Write operations (create, update) are only allowed against designated test profiles
  - Always verify the target profile ID before any write operation
  - Prefer mocked tests for write operations; use live API only when explicitly required
  - **Integration Test Safety:**
    - Integration tests create a dedicated "Validation Profile [timestamp]"
    - All write operations execute against this profile only
    - Profile deletion requires explicit user confirmation ("yes" only)
    - User can choose to keep the profile for manual inspection
    - Tests handle Ctrl+C gracefully and still offer cleanup
- **Integration Testing Requirements:**
  - **CRITICAL**: Integration tests MUST invoke MCP server functions via `server.py`, NOT direct API calls
  - Test script location: `tests/integration/test_live_api.py`
  - Integration tests verify all MCP tools work end-to-end
  - **Test Structure Requirements:**
    1. Create a "Validation Profile [timestamp]" for isolation
    2. Execute ALL operations against the validation profile using MCP server functions
    3. Ask user for confirmation before deleting the profile (require "yes" input)
    4. Only delete if explicitly approved by user
  - **How to Invoke MCP Tools:**
    - Import the MCP server: `from nextdns_mcp import server`
    - Access tools through `await server.mcp.get_tools()`
    - Call tools as `await tool.run(**params)` with proper parameters
    - Do NOT make direct HTTP calls to NextDNS API
  - **When to Run Integration Tests:**
    - When fixing bugs or adding features that affect any MCP tool
    - Before reporting completion of any write operation changes
    - After OpenAPI spec updates that add/modify operations
  - **Running Integration Tests:**
    ```bash
    export NEXTDNS_API_KEY="your_key"
    poetry run python tests/integration/test_live_api.py
    ```
  - If validation fails, fix the issues and re-run until all tests pass
  - Document validation failures and fixes in your response
- **Development Workflow:**
  - Phase 1: Create complete and accurate OpenAPI/Swagger documentation for the NextDNS API
  - Phase 2: Use `fastmcp.from_openapi()` to generate the MCP server from the OpenAPI spec
  - All NextDNS API endpoints should be documented in the OpenAPI spec before server generation
  - The fastmcp library will handle MCP protocol implementation, routing, and tool registration
- When in doubt, ask the repo owner for permission before making large design changes.
- API Key: Ensure that a valid API key is not in any files that will be committed to git.

## Testing Strategy

### Three-Tier Testing Approach

1. **Unit Tests** (`tests/unit/`)
   - Fast, isolated tests with mocked dependencies
   - Test individual functions and modules
   - Run frequently during development
   - See "Code Quality Standards" section for coverage requirements

2. **Integration Tests** (`tests/integration/`)
   - Test server initialization and MCP server creation
   - Verify tool registration and routing
   - Use mocked OpenAPI specs when possible
   - Fast enough to run in CI/CD

3. **Live API Validation** (`tests/integration/test_live_api.py`)
   - End-to-end testing against live NextDNS API
   - See "Integration Testing Requirements" section above for complete details
   - Run before major releases or when troubleshooting production issues

Merging policy: small, incremental PRs. Preserve existing README/CI content; when adding new top-level files, update README to reflect run/test/build instructions.

## Code Quality Standards

All code changes must meet the following quality metrics before work is considered complete. These standards ensure maintainability, reliability, and consistency across the codebase.

### 1. Code Formatting and Type Checking

**CRITICAL**: Code formatting and type checking must be the **final step** before validation:

1. Run `isort` to organize imports:
   ```bash
   poetry run isort src/ tests/
   ```

2. Run `black` to format code:
   ```bash
   poetry run black src/ tests/
   ```

3. Run `mypy` for type checking:
   ```bash
   poetry run mypy src/
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
poetry run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term-missing --cov-report=html

# View HTML report
open htmlcov/index.html
```

**Coverage Validation**:
- Check overall percentage in terminal output
- Review HTML report for per-file coverage
- Ensure no file falls below 95%
- Document any intentional gaps with inline comments explaining why they're untestable
- **All tests must pass** - zero failures, zero errors

### 3. Integration Test Requirements

**CRITICAL**: All integration tests must pass with 100% success rate. Failing tests are NEVER acceptable.

**Running Integration Tests**:
```bash
# Run all integration tests
poetry run pytest tests/integration

# Run live API validation
export NEXTDNS_API_KEY="your_key"
poetry run python tests/integration/test_live_api.py
```

**Integration Test Validation**:
- All tests must pass completely
- No errors, no failures, no skipped tests (unless intentionally marked)
- Fix any failures before proceeding
- See "Integration Testing Requirements" section for safety rules

### 4. Cyclomatic Complexity Standards

**Project Complexity**: Grade A
- Measured using `radon` tool
- Project average complexity must be grade A
- Run: `poetry run radon cc src/ -a`

**Function Complexity**: Maximum Grade B
- No individual function may exceed grade B (cyclomatic complexity ≤11)
- Check with: `poetry run radon cc src/ -nc`
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

Before running integration tests or claiming work is complete:

- [ ] Run `poetry run isort src/ tests/`
- [ ] Run `poetry run black src/ tests/`
- [ ] Run `poetry run mypy src/` (0 errors)
- [ ] Run `poetry run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term` (>95% coverage, **ALL tests pass**)
- [ ] Verify per-file coverage: all files >95% in `htmlcov/index.html`
- [ ] Run `poetry run radon cc src/ -a` (verify grade A)
- [ ] Run `poetry run radon cc src/ -nc` (verify no functions exceed grade B)
- [ ] Run integration tests: `poetry run pytest tests/integration` (**ALL tests pass**)
- [ ] Run live API validation: `poetry run python tests/integration/test_live_api.py` (**ALL tests pass**)
- [ ] Commit formatting changes as final commit before validation

**CRITICAL**: If ANY check fails, fix the issues and restart from step 1. Continue iterating through all quality checks until every standard is met with zero failures.

### 6. Quality Tools Configuration

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
