# Scripts and Testing Prompts

This directory contains helper scripts and validation prompts for the NextDNS MCP server.

## Overview

- **`run_container_e2e.py`** - Executes the full NextDNS MCP tool suite against a running MCP server container over MCP (HTTP streamable transport), with response schema validation. It also validates the container's `/health` readiness probe over real HTTP.
- **`validate_schema.py`** - Validates tool responses against OpenAPI response schemas (see [Schema Validation](#schema-validation) below) and fails the E2E run when the served catalog drifts from the spec. The schema check is advisory: it reports drift, it does not enforce a strict response contract.

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
uv run python scripts/validate_schema.py listProfiles '{"data":[{"id":"abc123","name":"My Profile"}]}'
```

**What it checks.** A published tool *is* one OpenAPI operation, named by its `operationId`, so the
tool name is the schema lookup key: `get_operation_response_schema()` resolves it against the
operation's `application/json` or `text/csv` responses. A response is `VALID` when it matches that
schema, `INVALID` when it does not, and `SKIPPED` when there is no response body to check (synthetic
`{"success": true}` payloads). The tools the server performs itself rather than proxying answer with a
payload no endpoint returns, so they are the exception: `dohLookup` validates against a pinned literal
schema in `TOOL_RESPONSE_SCHEMAS`, and `plotAnalytics` answers with an interactive chart (`CONTENT_TOOLS`), which the
runner asserts as image content rather than a document.

Because the strategy is a union and most vendored schemas declare no `required` fields, this is a
drift smoke signal, not a strict conformance check; the E2E job summary labels it
"Schema Validation (advisory)" for that reason. The report records per-call `schema_validation`
values (`VALID` / `INVALID` / `SKIPPED`) and `INVALID` results fail the E2E job.

**When adding or renaming an operation.** `run_container_e2e.py` calls `assert_operation_coverage()` at
startup, which hard-fails the E2E run when the served catalog drifts from the spec in either direction:
- a published tool names no operation in the spec (and is not one of the local operations),
- a spec operation is published as no tool (unless withheld with a reason — `streamLogs`, the SSE
  stream, is the one), which is what a stale image produces,
- a local tool's answer is neither pinned in `TOOL_RESPONSE_SCHEMAS` nor content, or
- a published read resolves to no response schema, because a read whose schema cannot be found is
  validated as SKIPPED — neither pass nor fail.

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

Progressive disclosure is on by default (`bm25`), so the MCP server hides the real tool
catalog behind FastMCP's `search_tools` + `call_tool` pair and the actor has to discover
tools on demand instead of seeing them listed up front. `--tool-search off` (or
`NEXTDNS_TOOL_SEARCH=off` in the env file) restores the full listing, which is what the
container E2E job pins so its steps can name tools directly; `--tool-search bm25` sets
the default explicitly.

```bash
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --tool-search bm25
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --tool-search off
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
itself, by calling the MCP `createProfile`/`deleteProfile` tools (so the server's
ACLs and error contract apply to it too); every other precondition is the caller's
job — if the selection tests "remove X", include the step that adds X.

### Reading a run: failure modes

A verdict per step is not a plan. `--summarize` groups a report's failures by
what actually went wrong, counts **steps** per mode (not calls), and names the
steps so they can be re-run after one fix:

```bash
uv run python scripts/ai_e2e_harness.py --summarize artifacts/ai_e2e_report.jsonl
uv run python scripts/ai_e2e_harness.py --summarize run1.jsonl run2.jsonl   # pooled
```

It needs no API key and runs no model, and it reads the server's own call log
records inside the report, so the modes it reports are what the server said
(`schema rejected argument 'profile_id'`, `upstream returned 400`, `no call
reached the server`) rather than a guess from the step name. The intended loop
is: pick the largest mode, change the server, re-run exactly the steps listed
beside it, then re-summarize.

### How a step is judged

A step passes when three things hold:

1. **The server recorded a call to the endpoint this step verifies.** Every
   published tool is bound to one endpoint, so naming it —
   `POST /profiles/{profile_id}/denylist` — names the API call the step is
   about no matter which tool the actor found to make it. The MCP server writes
   every tool call it executes to a JSONL call log (`NEXTDNS_CALL_LOG`, see
   `nextdns_mcp.call_log`): tool, endpoint, whether it mutates state (from the
   HTTP method), the arguments, whether it succeeded, and the payload it
   answered with. The step must match that record, and the call must have
   answered with a payload. `--print-steps` shows each step's resolved endpoint.
   `plotAnalytics` and `dohLookup` have no endpoint; their rule is their own
   record plus the payload (image content, the DNS answer). The log is created
   mode 0600 and holds real account data — queried domains, device names,
   query-log rows — so a report written from it inherits that sensitivity; see
   "Call log" in `docs/safety.md`.
2. **No state-changing call came after it.** Confirming reads afterwards are
   normal and allowed; a later write is not, because it can undo the step's own
   work (observed live: a rewrite added and then deleted in one attempt).
3. **The REST end state is right.** The harness reads the account back over the
   raw REST API, the same transport the actor never touches.

Condition 3 alone is not enough, and that was a real bug: a REST assertion
proves the state, not who produced it. `profiles.get` passed a run whose
transcript was nothing but errors, because the profile had been created two
steps earlier. Reads are the sharper case — after `getDenylist()` the API looks
identical whether or not the call ever happened — so for a read the server's own
record, including the payload it answered with, is the only evidence there is.

See the module docstrings and `--help` for the full configuration reference.
