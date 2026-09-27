# Scripts and Testing Prompts

This directory contains helper scripts and validation prompts for the NextDNS MCP server.

## Overview

- **`run_container_e2e.py`** - Executes the full NextDNS MCP tool suite against a running MCP server container over MCP (HTTP streamable transport), with response schema validation. It also validates the container's `/health` readiness probe over real HTTP.
- **`validate_schema.py`** - Validates JSON responses against OpenAPI schema definitions to ensure responses conform to expected schemas for the grouped tools.
- **`generate_usage_doc.py`** - Generates `docs/usage.md` from `src/nextdns_mcp/usage.py` to keep prompt and static documentation synchronized.
- **`ai_agent_e2e_prompt.md`** - End-to-end testing prompt for an AI agent (e.g. Claude Desktop, Cursor) to exercise the full NextDNS MCP server tool surface against a dedicated test profile.

## Usage

### Container E2E Validation

```bash
uv run python scripts/run_container_e2e.py --endpoint http://127.0.0.1:8000/mcp --variant slim
```

Options:
- `--endpoint`: MCP server HTTP endpoint (default: `http://127.0.0.1:8000/mcp`)
- `--variant`: Docker image variant being tested: `slim` or `alpine` (default: `slim`)
- `--allow-live-writes`: Enable profile creation and write operations (default: `false`)
- `--plot-profile`: Profile ID with analytics history for plotting tests
- `--artifacts-dir`: Directory for report output (default: `artifacts/`)
- `--health-url`: `/health` URL to validate (default: derived from `--endpoint`)
- `--api-key`: Credential the server was configured with; only used to assert it never appears in a `/health` response
- `--expect-health`: Assert `/health` returns `200` (`ok`) or `503` with a failure class (`not-ok`) (default: `ok`)
- `--health-only`: Validate only the `/health` endpoint and skip the MCP tool suite (default: `false`)

#### Validating the `/health` readiness probe

`/health` reports whether the server's configured NextDNS credentials actually work, so the
E2E suite asserts the contract a client receives. Against a container started with a
deliberately invalid key, no NextDNS credential and no live write are required:

```bash
uv run python scripts/run_container_e2e.py --health-only --expect-health not-ok \
  --health-url http://127.0.0.1:8001/health --api-key "<the invalid key>"
```

That asserts a `503` carrying a `class` of `auth` (or `unreachable`, when that is what the
failure actually is) and a non-empty `reason`, and that the body leaks no key material. The
`200 {"status": "ok"}` path is asserted on the credentialed container run, so a server whose
credentials are rejected can no longer pass by always answering `ok`. The `E2E Container` CI
job wires both cases up.

### Schema Validation

```bash
uv run python scripts/validate_schema.py <tool_name> <json_response>
```

Example:
```bash
uv run python scripts/validate_schema.py manageProfiles '{"data":[{"id":"abc123","name":"My Profile"}]}'
```

### Usage Documentation Generation

```bash
uv run python scripts/generate_usage_doc.py
```

Use `--check` to verify that `docs/usage.md` matches `nextdns_usage_guide()` without modifying the file:
```bash
uv run python scripts/generate_usage_doc.py --check
```

### AI Agent E2E Validation

See [ai_agent_e2e_prompt.md](ai_agent_e2e_prompt.md) for instructions on using an AI agent to validate tools against a live test profile.
