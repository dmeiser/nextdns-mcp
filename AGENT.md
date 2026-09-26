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
- Docker: provide a `Dockerfile` (primary, `python:3.14-slim`) and `Dockerfile.alpine` (Alpine variant) that produce small, runnable images.
- Keep `TODO.md` progress indicators in sync with the current phase while executing tasks.
- **Write Operation Safety Rules:**
  - Write operations (create, update) are only allowed against designated test profiles
  - Always verify the target profile ID before any write operation
- **Development Workflow:**
  - Phase 1: Create complete and accurate OpenAPI/Swagger documentation for the NextDNS API
  - Phase 2: Use `fastmcp.from_openapi()` to generate the MCP server from the OpenAPI spec
  - All NextDNS API endpoints should be documented in the OpenAPI spec before server generation
  - The fastmcp library will handle MCP protocol implementation, routing, and tool registration
- **Array-body Endpoints (FastMCP 3.x):**
  - FastMCP 3.x supports array bodies natively via the `body` parameter.
  - Use `body=[{"id":"value"}]` for list replacement tools (e.g., `replaceDenylist`, `replaceAllowlist`).
  - Do not use legacy `update*` custom tools (they no longer exist).
- When in doubt, ask the repo owner for permission before making large design changes.
- API Key: Ensure that a valid API key is not in any files that will be committed to git.

## Testing Strategy

1. **Unit Tests** (`tests/unit/`)
   - Fast, isolated tests with mocked dependencies
   - Test individual functions and modules
   - Run frequently during development
   - See "Code Quality Standards" section for coverage requirements
   - Must achieve 100% code coverage

2. **Integration Tests** (`tests/integration/`)
   - Server initialization and creation tests
   - Verify tool registration, OpenAPI loading, and access control initialization

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
- **Exceptions**: Only for truly untestable code (e.g., `if __name__ == "__main__"`, module-level `sys.exit()`). Every exception must be covered by an explicit `# pragma: no cover` with a comment explaining why.

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
- Ensure no file falls below 100%
- Document any intentional gaps with inline comments explaining why they're untestable
- **All tests must pass** - zero failures, zero errors

### 3. Cyclomatic Complexity Standards

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

### 4. Pre-Commit Quality Checklist

Before claiming work is complete:

- [ ] Run `uv run ruff check --fix src/ tests/`
- [ ] Run `uv run ruff format src/ tests/`
- [ ] Run `uv run mypy src/` (0 errors)
- [ ] Run `uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term` (100% coverage, **ALL tests pass**)
- [ ] Verify per-file coverage: all files 100% in `htmlcov/index.html`
- [ ] Run `uv run radon cc src/ -a` (verify grade A)
- [ ] Run `uv run radon cc src/ -nc` (verify no functions exceed grade B)
- [ ] Commit formatting changes as final commit before validation

**CRITICAL**: If ANY check fails, fix the issues and restart from step 1. Continue iterating through all quality checks until every standard is met with zero failures.

### 5. Quality Tools Configuration

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

### 6. Handling Quality Failures

**CRITICAL**: Code quality is non-negotiable. If any check fails, the quality pipeline must be rerun until ALL standards are met.

**Failure Response Process**:

1. **isort/black failures**: Should auto-fix, re-run all subsequent checks
2. **mypy failures**: Add type hints, fix type errors, document `# type: ignore` if absolutely necessary, then restart quality checks
3. **Test failures (unit or integration)**: 
   - Debug and fix the failing test or code
   - **NEVER ignore, skip, or comment out failing tests**
   - Restart quality checks from step 1 after fixes
4. **Coverage <100%**: Add missing test cases, remove dead code, or document why code is untestable with `# pragma: no cover`, then restart quality checks
5. **Complexity >B**: Refactor function into smaller units, extract methods, simplify logic, then restart quality checks

**Iteration Loop**:
- Fix the issue identified
- Run `isort` → `black` → `mypy`
- Run all unit tests with coverage
- Run all integration tests
- If any check fails, repeat the loop
- Continue until 100% of quality standards are met

**Never skip quality checks** — they catch bugs before production and ensure code maintainability. Failing tests indicate broken code that must be fixed, not ignored.
