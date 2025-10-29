# NextDNS MCP Server - AI Agent Instructions

This document provides essential guidance for AI agents working on the NextDNS MCP Server codebase.

## 1. Architecture: OpenAPI-Driven Tool Generation

**The Key Insight**: This server uses a declarative, specification-first approach. The MCP server is NOT hand-coded—it's auto-generated from `src/nextdns_mcp/nextdns-openapi.yaml` using `FastMCP.from_openapi()`.

**To add/modify API functionality**:
1. Edit the OpenAPI YAML file (add paths, operations, schemas)
2. The server regenerates automatically on next run
3. Exception: Array-body PUT endpoints (see "Custom Tools" below)

**Why this matters**: Don't look for traditional route handlers or controllers. The mapping happens in `server.py:create_mcp_server()` via `FastMCP.from_openapi()`. Tool names are derived from `operationId` fields in the OpenAPI spec.

## 2. Custom Tools Pattern (Array-Body Workaround)

**Problem**: FastMCP doesn't support raw JSON arrays as request bodies (needs objects with named fields).

**Solution**: Seven custom tools in `server.py` that accept JSON array strings:
- `updateDenylist`, `updateAllowlist`, `updateParentalControlServices`, etc.
- Pattern: `entries: str` parameter → parse JSON → validate is array → PUT to API
- Helper: `_bulk_update_helper()` centralizes this pattern

**When to add custom tools**:
- Array-body endpoints (excluded via `EXCLUDED_ROUTES` in `config.py`)
- Special logic like `dohLookup` (DNS testing without API key)
- Follow existing pattern: separate `_impl()` function + `@mcp.tool()` decorator

## 3. Configuration: Docker Secrets + Env Vars

**API Key Loading** (`config.py:get_api_key()`):
1. Check `NEXTDNS_API_KEY` env var
2. Fallback to `NEXTDNS_API_KEY_FILE` (Docker secrets path)
3. Fails fast on module import if missing

**Critical**: `validate_configuration()` runs on import—server won't start without valid API key.

## 4. Testing Strategy (3-Tier Pyramid)

### Unit Tests (`tests/unit/`)
- **Target**: 85%+ coverage
- **Run**: `poetry run pytest tests/unit --cov=src/nextdns_mcp`
- **Pattern**: Mock external calls (use `unittest.mock.AsyncMock` for httpx)
- **Example**: `test_doh_lookup.py` mocks `httpx.AsyncClient` to test DNS logic

### Integration Tests (`tests/integration/`)
- **Purpose**: Server initialization, tool registration
- **Run**: `poetry run pytest tests/integration`
- **Fast**: Uses mocked OpenAPI specs, no network calls

### Live API Validation (`tests/integration/test_live_api.py`)
- **CRITICAL**: End-to-end test of ALL 76 tools against real NextDNS API
- **Run**: `poetry run python tests/integration/test_live_api.py`
- **Safety**: Creates isolated "Validation Profile [timestamp]"
- **Must invoke via MCP server**: `from nextdns_mcp import server; tools = await server.mcp.get_tools()`
- **When required**: Before completing ANY feature touching API endpoints
- **Cleanup**: Prompts user before deleting test profile (use `--auto-delete-profile` flag to skip)

## 5. Development Commands

```bash
# Local development
poetry install
poetry run python -m nextdns_mcp.server

# Run unit tests with coverage
poetry run pytest tests/unit --cov=src/nextdns_mcp --cov-report=html

# Run integration tests
poetry run pytest tests/integration

# Live API validation (requires NEXTDNS_API_KEY)
export NEXTDNS_API_KEY="your_key"
poetry run python tests/integration/test_live_api.py

# Docker build (multi-stage, Python 3.13-slim)
docker build -t nextdns-mcp .
```

## 6. Key Files Reference

- `src/nextdns_mcp/nextdns-openapi.yaml`: Source of truth for 68+ API operations
- `src/nextdns_mcp/server.py`: FastMCP server + 8 custom tools (lines 250-527)
- `src/nextdns_mcp/config.py`: Env vars, excluded routes, DNS constants
- `tests/integration/test_live_api.py`: 76-tool validation script (1200+ lines)
- `catalog.yaml`: Docker MCP Gateway integration metadata

## 7. Safety Rules (Non-Negotiable)

- **NEVER delete profiles** without explicit user confirmation (except test profile in validation script)
- **Write operations** only against designated test profiles
- **Test via MCP tools**, not direct API calls (ensures protocol layer testing)
- **Run live validation** before claiming completion of API-touching features
