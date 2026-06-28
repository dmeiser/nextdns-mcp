# NextDNS MCP Test Suite Summary

## Status: ✅ ALL TESTS PASS

**Unit Tests:** 342 passed, 5 skipped  
**E2E Tests:** 92 calls, 0 failed (both `slim` and `alpine` variants)  
**Failed Tests:** 0  
**Last Run:** 2026-06-27  

## Verification Commands

```bash
# Run full test suite with coverage
uv run pytest --cov=src/nextdns_mcp --cov-report=term-missing -q

# Run E2E tests against both image variants
bash scripts/gateway_e2e_run.sh .env slim
bash scripts/gateway_e2e_run.sh .env alpine
```

## Recent Fixes

### 1. Restricted `manageLists` updates (`src/nextdns_mcp/server.py`)
- `manageLists` `update` is now rejected for list types that do not support per-entry `PATCH` (e.g., `security_tlds`, `privacy_blocklists`, `privacy_natives`).
- Added `_LIST_UPDATEABLE_TYPES` and an explicit error response with the supported list types.

### 2. Performance settings path (`src/nextdns_mcp/server.py`)
- Fixed `_SETTINGS_PATHS["performance"]` from `performance` to `settings/performance` to match the OpenAPI spec.

### 3. E2E hardening (`scripts/run_all_tools.sh`)
- Removed `set -e` so individual tool failures no longer abort the run before the summary/cleanup.
- Added JSON error-payload detection: calls that return `{"error": ...}` are now counted as failures even when the Docker MCP CLI exits 0.
- Fixed setup/cleanup exit-code masking caused by `|| echo ""` always returning success.
- Expanded read-only coverage:
  - All 11 `queryAnalytics` aggregate metrics.
  - All 10 supported time-series metrics.
  - All 9 supported `plotAnalytics` metrics (skipped when no series data is available).
- Expanded write coverage:
  - Full `replace -> update -> remove -> add -> remove` lifecycle for every list type.
  - Each settings category is exercised with both `get` and `update`.

### 4. Schema validation for grouped tools (`scripts/validate_schema.py`)
- Added `GROUPED_TOOL_OPERATIONS` mapping from grouped MCP tool names to the underlying OpenAPI operationIds.
- A response is valid if it matches any expected schema for the grouped tool.
- Synthetic `{"success": true}` responses are skipped instead of reported as invalid.

### 5. Unit-test coverage (`tests/unit/test_grouped_tools.py`)
- Added a test verifying that `manageLists` rejects `update` for unsupported list types.
