# Integration Tests

This directory contains integration tests for the NextDNS MCP server.

## Test Files

- **`test_server_init.py`** - Server initialization and creation tests (no live API calls)

## Running Tests

```bash
# Run server initialization tests
uv run pytest tests/integration/test_server_init.py -v
```

## End-to-End Testing

For complete end-to-end testing with the live NextDNS API via Docker MCP Gateway, use the Gateway E2E scripts instead:

```bash
# PowerShell (Windows)
cd scripts
.\gateway_e2e_run.ps1

# Bash (Linux/macOS)
cd scripts
./gateway_e2e_run.sh
```

See `scripts/README.md` for complete Gateway E2E testing documentation.

## What These Tests Cover

### `test_server_init.py`
- MCP server module loading
- Server creation with valid configuration
- Tool registration and counting
- OpenAPI spec loading
- Access control client initialization
- Error handling for missing configuration

These tests use mocked dependencies and do NOT make live API calls.

## Prerequisites

1. **Python Environment**
   ```bash
   uv sync
   ```

2. **Environment Configuration**
   ```bash
   # Copy example environment file
   cp .env.example .env
   
   # Set your NextDNS API key in .env
   NEXTDNS_API_KEY=your_api_key_here
   ```

## Test Coverage

The integration tests verify:
- ✅ MCP server can be initialized
- ✅ OpenAPI spec loads correctly
- ✅ Tools are registered properly
- ✅ Access control mechanisms work
- ✅ Configuration validation functions correctly

For complete API endpoint testing, use the Gateway E2E scripts in `scripts/`.

## Development

### Running Tests During Development

```bash
# Run with verbose output
uv run pytest tests/integration/test_server_init.py -v

# Run with coverage
uv run pytest tests/integration/test_server_init.py --cov=src/nextdns_mcp

# Run specific test
uv run pytest tests/integration/test_server_init.py::TestServerInitialization::test_create_mcp_server -v
```

### Adding New Tests

When adding new MCP server functionality:

1. Add tests to `test_server_init.py` to verify server initialization
2. Use mocked dependencies (see existing tests for examples)
3. Do NOT make live API calls in integration tests
4. For live API testing, add to Gateway E2E scripts in `scripts/`

## Notes

- These tests do NOT require a NextDNS API key
- These tests do NOT make network calls
- These tests verify server initialization logic only
- For full end-to-end validation, use Gateway E2E scripts
