# Quick Test Reference

## Unit Tests (Fast, Mocked)

```bash
# All unit tests
poetry run pytest tests/unit -v

# Specific test files
poetry run pytest tests/unit/test_access_control.py -v
poetry run pytest tests/unit/test_access_controlled_client.py -v

# With coverage report
poetry run pytest tests/unit --cov=src/nextdns_mcp --cov-report=html
```

## Integration Tests (Live API, Requires NEXTDNS_API_KEY)

### Unified Runner (Recommended)

```bash
# Run ALL integration tests
poetry run python tests/integration/run_integration_tests.py

# With auto-cleanup
poetry run python tests/integration/run_integration_tests.py --auto-cleanup

# Run specific suites only
poetry run python tests/integration/run_integration_tests.py --only-api
poetry run python tests/integration/run_integration_tests.py --only-access-control
```

### Individual Tests

```bash
# API validation (76 tools)
poetry run python tests/integration/test_live_api.py

# Access control (all scenarios)
poetry run python tests/integration/test_live_access_control.py

# Server initialization
poetry run pytest tests/integration/test_server_init.py -v
```

## Expected Results

**Unit Tests:**
- 41 tests pass
- Runs in ~1-2 seconds

**Integration Tests:**
- Server init: 3-5 tests pass
- API validation: 76 tools pass
- Access control: 34 tests pass (all scenarios)
- Total runtime: ~2-5 minutes
