# Integration Tests

This directory holds no tests. The integration layer lives in the unit suite and in
`scripts/`, and this file says which is which, so nobody adds a live-API test here by
accident or looks for coverage that has moved.

## Where the integration coverage lives

| Concern | Where it runs |
|---------|---------------|
| Server creation, disclosure wiring, tool registration | `tests/unit/test_mcp_server.py` — mocked, no live API calls |
| The spec-generated surface: one tool per `operationId`, the endpoint registry, the transport that keeps the ACL in the path | `tests/unit/test_spec_tools.py` |
| The typed error contract, asserted through a generated tool | `tests/unit/test_error_contract.py` |
| Profile access control, including the `NEXTDNS_PROTECTED_PROFILES` denylist | `tests/unit/test_access_control.py`, `tests/unit/test_access_controlled_client.py` |
| The real MCP wire over a built container, plus `/health` | `scripts/run_container_e2e.py`, run by `.github/workflows/e2e-container.yml` |
| Live model-driven validation against the account | `scripts/ai_e2e_harness.py` (see `scripts/README.md`) |

## The tool surface those tests describe

The server publishes one tool per operation of the shipped OpenAPI document
(`src/nextdns_mcp/nextdns-openapi.yaml`): 78 of the document's 79 operations, each named
for its own `operationId` (`getProfile`, `addToDenylist`, `getAnalyticsStatusSeries`)
and taking that endpoint's own arguments. There is no grouped CRUD surface and no
operation argument.

Three operations are not plain generated proxies, and the tests treat them accordingly:

- `streamLogs` is withheld: it is a Server-Sent Events stream, and a tool call is
  request/response. It is published as no tool at all.
- `downloadLogs` is generated under its own name but delegated to the guarded downloader
  in `src/nextdns_mcp/tools/logs.py`. It answers with a local `file_path` and a bounded
  preview rather than an API document, which is why the schema validator treats it as an
  exception.
- `plotAnalytics` and `dohLookup` are hand-written composites registered beside the
  generated ones. They have no endpoint, so the call-log row records no endpoint for
  them and they are scored as reads.

Under the default disclosure mode (`NEXTDNS_TOOL_SEARCH=bm25`) a client sees only
`search_tools` + `call_tool` in `tools/list`; the container E2E job pins
`NEXTDNS_TOOL_SEARCH=off` so its steps can name tools directly.

## Running the tests

```bash
uv sync

# The suites above (no API key, no network)
uv run pytest tests/unit/test_mcp_server.py tests/unit/test_spec_tools.py \
  tests/unit/test_error_contract.py tests/unit/test_access_control.py \
  tests/unit/test_access_controlled_client.py -v

# Coverage over the same code paths
uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term-missing
```

The container E2E run needs a built image and an API key and is driven by CI; run it
with `uv run python scripts/run_container_e2e.py --help` for its own options.

## Adding tests

- New behaviour in the generated surface: add to `tests/unit/test_spec_tools.py` (or
  `test_error_contract.py` when the point is the payload shape). Use mocks; no live calls.
- New local/composite tool: cover it in its own unit suite, and add its exception to
  `scripts/validate_schema.py` so the catalog-to-document check still holds.
- Anything that needs the real API belongs in `scripts/run_container_e2e.py` (deterministic
  wire checks) or `scripts/ai_e2e_harness.py` (model-driven), not in this directory.

## Notes

- These unit suites need no NextDNS API key and make no network calls.
- They verify server construction, the generated catalog, the ACL, and the error
  payloads — the live behaviour is the container job's and the harness's job.
