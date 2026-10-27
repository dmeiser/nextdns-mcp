# Scripts and Testing Prompts

This directory contains helper scripts and validation prompts for the NextDNS MCP server.

## Overview

- **`run_container_e2e.py`** - Executes the full NextDNS MCP tool suite against a running MCP server container over MCP (HTTP streamable transport), with response schema validation. It also validates the container's `/health` readiness probe over real HTTP.
- **`validate_schema.py`** - Validates tool responses against OpenAPI response schemas (see [Schema Validation](#schema-validation) below). The check is advisory: it reports drift, it does not enforce a strict response contract.
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

**What it checks.** `GROUPED_TOOL_OPERATIONS` in `validate_schema.py` maps each grouped tool to the
OpenAPI `operationId`s whose response schemas apply to it; `get_operation_response_schema()` resolves
each id against `application/json` or `text/csv` responses. A response is `VALID` when it matches
*any one* of a tool's candidate schemas, `INVALID` when it matches none, and `SKIPPED` when there is
no response body to check (synthetic `{"success": true}` payloads). Tools with no OpenAPI operation to
derive a schema from (`dohLookup`, which queries dns.nextdns.io directly) validate against a pinned
literal schema in `TOOL_RESPONSE_SCHEMAS`.

Because the strategy is a union and most vendored schemas declare no `required` fields, this is a
drift smoke signal, not a strict conformance check; the E2E job summary labels it
"Schema Validation (advisory)" for that reason. The report records per-call `schema_validation`
values (`VALID` / `INVALID` / `SKIPPED`) and `INVALID` results fail the E2E job.

**When adding or renaming a tool.** `run_container_e2e.py` calls `assert_operation_coverage()` at
startup, which hard-fails the E2E run when:
- a registered tool is missing from `GROUPED_TOOL_OPERATIONS` (or a mapping entry names a tool the
  server no longer serves),
- a mapped `operationId` resolves to no response schema in the vendored spec, or
- a mapped tool ends up with no candidate schema at all (no resolvable `operationId` and no
  `TOOL_RESPONSE_SCHEMAS` entry), which would silently skip every one of its responses.

So a new tool needs a mapping entry with at least one `operationId` whose response schema resolves,
or an explicit `TOOL_RESPONSE_SCHEMAS` entry. Write operations that return a synthetic
`{"success": true}` payload are intentionally left out of the mapping.

### Usage Documentation Generation

```bash
uv run python scripts/generate_usage_doc.py
```

Use `--check` to verify that `docs/usage.md` matches `nextdns_usage_guide()` without modifying the file:
```bash
uv run python scripts/generate_usage_doc.py --check
```

### MCP-Native, API-Verified E2E Harness

- **`ai_e2e_harness.py`** - Python. An MCP-native LLM harness is the actor; the script measures.
- **`ai_e2e_common.py`** - Python. Shared, pure (network-free) helpers (result model, constant tables, REST read-back).

The harness provisions its own isolated test profile, drives the live MCP server
through an MCP-native actor (default `opencode`, configurable), then independently
verifies the resulting state against the NextDNS REST API and owns cleanup.

Usage:

```bash
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --config my_run.json
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --model ollama-cloud/kimi-k2.7-code
NEXTDNS_API_KEY=... NEXTDNS_E2E_MODEL=llama3 uv run python scripts/ai_e2e_harness.py
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --profile-name "TEST_PROFILE_{date}"
uv run python scripts/ai_e2e_harness.py            # -> SKIP (no key)
```

The test profile is named `TEST_PROFILE_YYYYMMDD` by default (overridable via
`NEXTDNS_E2E_PROFILE_NAME` / `--profile-name`, with `{date}` / `{timestamp}`
placeholders). The model can be chosen with `--model`, `NEXTDNS_E2E_MODEL`, or
left to opencode's own configured default.

To exercise progressive tool disclosure, add `--tool-search regex|bm25` (or set
`NEXTDNS_TOOL_SEARCH` in the env file). This makes the MCP server hide the real
tool catalog behind FastMCP's `search_tools` + `call_tool` pair, so the actor
must discover tools on demand instead of seeing them listed up front.

```bash
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --tool-search bm25
```

To run only part of the ~89-step checklist, print the step catalog and pass a
`--steps` selection: a comma-separated list of step ids, catalog positions, or
inclusive `A-B` ranges. `--start-step` / `--end-step` are the single-range
shorthand for the same thing.

```bash
uv run python scripts/ai_e2e_harness.py --print-steps
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --steps 20-21,lists.privacy_natives.replace-36,profiles.delete
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --start-step lists.allowlist.add --end-step rewrites.add.cname
```

Progress keeps the full-run numbering (`[31/89]`), so a targeted report can be
compared with a full run step by step. A selection that leaves out `profiles.create`
or `profiles.delete` makes the harness do that part of the profile lifecycle
itself, by calling the MCP `manageProfiles` tool (so the server's ACLs and error
contract apply to it too); every other precondition is the caller's job — if the
selection tests "remove X", include the step that adds X.

See the module docstrings and `--help` for the full configuration reference.

### AI Agent E2E Validation

See [ai_agent_e2e_prompt.md](ai_agent_e2e_prompt.md) for instructions on using an AI agent to validate tools against a live test profile.
