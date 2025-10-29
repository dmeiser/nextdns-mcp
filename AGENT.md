# AGENTS.md — Rules and behaviors for AI agents

This file contains repository-specific agent rules. Agents should follow these when making changes.

- Purpose: implement an MCP server for NextDNS API in Python using the `fastmcp` library, containerized with Docker.
- Keep changes minimal and self-contained. Prefer adding new files rather than editing many unrelated files.
- Use the `fastmcp` library to build the MCP server, specifically using the `from_openapi` function to generate the server from OpenAPI/Swagger documentation.
- Configuration should be environment-variable first. Example: `NEXTDNS_API_KEY` for API access.
- Provide a minimal health endpoint at `/health` returning 200 OK and JSON `{ "status": "ok" }`.
- Tests: add pytest-based unit tests for new functionality and run them with `poetry run pytest`.
  - **Unit Test Coverage Requirements:**
    - Maintain minimum 85% code coverage for all business logic
    - Run coverage reports with: `poetry run pytest --cov=src/nextdns_mcp --cov-report=term-missing`
    - Test all critical paths: API key loading, HTTP client creation, DoH lookup, settings operations
    - Use mocks for external dependencies (HTTP calls, file I/O)
    - Acceptable uncovered lines: module-level initialization, `if __name__ == "__main__"` blocks
    - Coverage report available at: `htmlcov/index.html` after running tests with `--cov-report=html`
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
   - Target: 85%+ coverage of business logic
   - Run frequently during development

2. **Integration Tests** (`tests/integration/`)
   - Test server initialization and MCP server creation
   - Verify tool registration and routing
   - Use mocked OpenAPI specs when possible
   - Fast enough to run in CI/CD

3. **Live API Validation** (`tests/integration/test_live_api.py`)
   - Comprehensive end-to-end testing against live NextDNS API
   - Invokes ALL 55 MCP tools through server.py
   - Creates isolated "Validation Profile" for safety
   - Requires valid NextDNS API key
   - User confirmation required before cleanup
   - Run before major releases or when troubleshooting production issues

### Test Coverage Expectations

- **Core Functions**: 100% coverage required
  - `get_api_key()` - API key loading
  - `load_openapi_spec()` - Spec file loading
  - `create_nextdns_client()` - HTTP client setup
  - `_dohLookup_impl()` - Custom DoH lookup

- **Acceptable Gaps**:
  - Module-level `sys.exit()` calls (inherently difficult to test)
  - `if __name__ == "__main__"` blocks (entry points)
  - Trivial wrapper functions that delegate to implementations

- **Current Coverage**: 87% (92 statements, 12 uncovered)

Merging policy: small, incremental PRs. Preserve existing README/CI content; when adding new top-level files, update README to reflect run/test/build instructions.
