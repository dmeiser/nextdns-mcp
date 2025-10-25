# AGENTS.md — Rules and behaviors for AI agents

This file contains repository-specific agent rules. Agents should follow these when making changes.

- Purpose: implement an MCP server for NextDNS API in Python using the `fastmcp` library, containerized with Docker.
- Keep changes minimal and self-contained. Prefer adding new files rather than editing many unrelated files.
- Use the `fastmcp` library to build the MCP server, specifically using the `from_openapi` function to generate the server from OpenAPI/Swagger documentation.
- Configuration should be environment-variable first. Example: `NEXTDNS_API_KEY` for API access.
- Provide a minimal health endpoint at `/health` returning 200 OK and JSON `{ "status": "ok" }`.
- Tests: add pytest-based unit tests for new functionality and run them with `poetry run pytest`.
- Docker: provide a `Dockerfile` that produces a small, runnable image; prefer using official `python:3.11-slim` as base.
- Keep `TODO.md` progress indicators in sync with the current phase while executing tasks.
- **Write Operation Safety Rules:**
  - **NEVER delete a NextDNS profile** - Profile deletion is strictly forbidden, even though the API supports it
  - Write operations (create, update) are only allowed against designated test profiles
  - When testing write operations, use only the profile specifically designated for testing
  - Always verify the target profile ID before any write operation
  - Prefer mocked tests for write operations; use live API only when explicitly required
  - After write operation tests, restore the test profile to its original state when possible
- **Validation Testing Requirements:**
  - When fixing bugs or adding features that affect write operations, **ALWAYS update and run the comprehensive validation script** to verify fixes before reporting completion
  - If `.env` contains `NEXTDNS_TEST_PROFILE`, you should use that profile to run validation tests
  - Do not rely solely on user feedback loops - verify fixes work end-to-end yourself
  - Only report a fix as complete after running validation and seeing all tests pass
  - If validation fails, fix the issues and re-run until all tests pass
  - Document any validation failures and fixes in your response to the user
- **Development Workflow:**
  - Phase 1: Create complete and accurate OpenAPI/Swagger documentation for the NextDNS API
  - Phase 2: Use `fastmcp.from_openapi()` to generate the MCP server from the OpenAPI spec
  - All NextDNS API endpoints should be documented in the OpenAPI spec before server generation
  - The fastmcp library will handle MCP protocol implementation, routing, and tool registration
- When in doubt, ask the repo owner for permission before making large design changes.
- API Key: Ensure that a valid API key is not in any files that will be committed to git.

Merging policy: small, incremental PRs. Preserve existing README/CI content; when adding new top-level files, update README to reflect run/test/build instructions.
