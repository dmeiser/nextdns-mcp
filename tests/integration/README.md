# Integration Tests

This directory contains integration tests for the NextDNS MCP server.

## Test Files

Server initialization and `create_mcp_server` tests live in the unit suite
(`tests/unit/test_mcp_server.py`) and make no live API calls.

## Running Tests

```bash
# Run the server-creation unit tests
uv run pytest tests/unit/test_mcp_server.py -v
```

## What These Tests Cover

### `tests/unit/test_mcp_server.py`
- MCP server creation with valid configuration
- Tool registration (grouped CRUD tools only)
- Access control client initialization

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

The server-creation tests verify:
- ✅ MCP server can be initialized
- ✅ Tools are registered properly (grouped CRUD tools only)
- ✅ Access control mechanisms work
- ✅ Configuration validation functions correctly

## Development

### Running Tests During Development

```bash
# Run with verbose output
uv run pytest tests/unit/test_mcp_server.py -v

# Run with coverage
uv run pytest tests/unit/test_mcp_server.py --cov=src/nextdns_mcp
```

### Adding New Tests

When adding new MCP server functionality:

1. Add tests to `tests/unit/test_mcp_server.py` to verify server initialization
2. Use mocked dependencies (see existing tests for examples)
3. Do NOT make live API calls in integration tests

## Notes

- These tests do NOT require a NextDNS API key
- These tests do NOT make network calls
- These tests verify server initialization logic only
