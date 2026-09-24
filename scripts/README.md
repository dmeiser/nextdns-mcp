# NextDNS MCP Gateway E2E Testing Scripts

This directory contains Bash scripts for end-to-end validation of the NextDNS MCP Gateway via the Docker MCP Gateway CLI.

## Overview

The E2E testing approach is **CLI-driven** and **shell-first**, meaning:
- There is **no HTTP API** to call tools directly
- All integration testing uses the `docker mcp` CLI
- Tests verify the CLI parsing and quoting behavior that operators will use in production

## Scripts

### E2E Driver

- **`gateway_e2e_run.sh`** - Bash E2E driver

This script performs the complete E2E workflow:
1. Load configuration from `.env` file
2. Build the Docker image (`slim` by default, or `alpine` when requested)
3. Import `catalog.yaml` into the gateway
4. Start the Docker MCP Gateway container
5. Wait for gateway readiness
6. Run all tools via `run_all_tools.sh`
7. Clean up and optionally delete the validation profile

### Tool Execution Script

- **`run_all_tools.sh`** - Bash tool enumeration and invocation

This script:
- Enumerates all available tools from the Docker MCP Gateway (`docker mcp tools ls`)
- Performs preflight validation to ensure tools exist before invocation
- Executes each tool with appropriate test parameters
- Skips write operations unless `ALLOW_LIVE_WRITES=true`
- Produces machine-readable JSONL reports in `artifacts/tools_report_<variant>.jsonl`

### MCP-Native, API-Verified E2E Harness

- **`ai_e2e_harness.py`** - Python. An *MCP-native LLM harness* is the actor;
  the script measures.
- **`ai_e2e_common.py`** - Python. The shared, pure (network-free) helpers used
  by the harness and its unit tests (result model, the server-surface constant
  tables, and the REST read-back helpers).

The intent, per the design ruling: the LLM actor must connect to the MCP server
*natively* (stdio) and do the tool calling itself — a harness without MCP
support cannot be the driver. The script therefore has two deliberately
separate halves:

1. **The actor is an MCP-native harness.** The default is
   [opencode](https://opencode.ai) run in non-interactive mode
   (`opencode run --standalone --format json "<task>"`), which has built-in MCP
   support. The harness writes an `opencode.json` into the actor's working
   directory declaring the in-tree MCP server as a *local* stdio server
   (`<python> -m nextdns_mcp.server`, with `NEXTDNS_API_KEY` in its
   environment); opencode launches that process over stdio and exposes its 8
   grouped tools to the model. The model decides which tool to call, with which
   arguments, and in which order, against the **live** server. The script never
   drives the MCP tools itself and the actor never acts through a REST
   side-channel — MCP over stdio is the only actor path.
2. **The script measures.** After the harness exits, the harness talks to the
   NextDNS REST API directly with the API key — independently of the MCP path —
   and verifies the *resulting state* against the fixed targets embedded in the
   task: the test profile was provisioned **and renamed**, every settings
   category holds the target values, every list type contains the target entry,
   and the target rewrite record exists. It also records **tool coverage**
   (which of the 8 grouped tools appeared in the harness's event stream; a
   best-effort, non-verdicting signal, reported `skipped` when unobserved).
   The harness then owns cleanup: it deletes the test profile via REST and
   verifies the deletion (404 + absent-by-name read-back). Each measurement is a
   `passed` / `skipped` / `failed` check in a JSONL report with a PASS/FAIL
   verdict.

The harness is a **configurable command template**: `opencode` is the
documented default, but any harness that mounts the MCP server can substitute
it. The command is a template with placeholders (see below), and a run can be
fully described by a JSON config file.

Characteristics:
- **MCP-native actor** — the harness process connects to the MCP server over
  stdio and the LLM does the tool calling; no REST side-channel on the actor
  path, no bridging shim.
- **Configurable harness** — opencode by default; override the command line or
  supply a JSON config. The MCP server (argv, cwd, env) is also configurable.
- **Deterministic, harness-agnostic measurement** — the verdict is REST state
  read-back of the task's fixed targets, so it holds for *any* actor harness.
- **Harness-owned cleanup** — the actor is told not to delete the profile; the
  measuring half deletes it and verifies the deletion.
- **Environment-gated** — without `NEXTDNS_API_KEY` it prints a clean `SKIP`
  report and exits 0 (no network contact).

Usage:

```bash
# Default actor: opencode (its configured default model), in-tree MCP server.
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py

# Pin a model (fills the {model} placeholder in the default command template):
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py \
    --model ollama-cloud/kimi-k2.7-code

# Substitute any MCP-capable harness with a command template:
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py \
    --harness-cmd "opencode run --standalone --format json --model ollama-cloud/kimi-k2.7-code {prompt}"

# Full control via a JSON config file:
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --config my_run.json

# No key -> clean SKIP report, exit 0 (no network contact).
uv run python scripts/ai_e2e_harness.py
```

`my_run.json` (all keys optional; the opencode defaults are used for anything
unset):

```json
{
  "command": ["opencode", "run", "--standalone", "--format", "json",
              "--model", "{model}", "{prompt}"],
  "model": "ollama-cloud/kimi-k2.7-code",
  "prompt": "optional: fully override the built-in task prompt",
  "workdir": "/tmp/nextdns-e2e-run",
  "timeout_s": 600,
  "server": { "command": "/path/to/python", "args": ["-m", "nextdns_mcp.server"],
              "cwd": "/path/to/repo", "env": { "NEXTDNS_READABLE_PROFILES": "ALL" } }
}
```

**Command template placeholders.** `{prompt}` — the task prompt; `{model}` —
the model (`--model` flag / `model` config key; an empty value drops a trailing
`--model`-style flag pair); `{workdir}` — the directory the harness is launched
in; `{config_file}` — the generated `opencode.json` path; `{server_command}` /
`{server_env}` — the MCP server argv/env. A substituted harness must mount the
MCP server itself — that is what makes it MCP-native.

**Example: a second harness (Claude Code).** Claude Code is MCP-capable and
loads its MCP servers from a `--mcp-config` JSON file in its own
`mcpServers` schema. Mount the same in-tree server and hand it the task:

```jsonc
// claude_mcp.json  (Claude Code's mcpServers schema)
{
  "mcpServers": {
    "nextdns": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "nextdns_mcp.server"],
      "cwd": "/path/to/repo",
      "env": { "PYTHONPATH": "/path/to/repo/src", "NEXTDNS_API_KEY": "..." }
    }
  }
}
```

```bash
NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py \
  --harness-cmd "claude -p --output-format stream-json --permission-mode bypassPermissions --mcp-config /path/to/claude_mcp.json {prompt}"
```

The measuring half is harness-agnostic: it does not parse the harness's event
stream for the verdict, only for coverage, so any substitution with MCP support
works unchanged.

The JSONL report defaults to `artifacts/ai_e2e_report.jsonl` (`--report` to
override, `--report -` for stdout). Pure helpers are unit-tested in
`tests/unit/test_ai_e2e_harness.py` (loaded from `ai_e2e_common.py`).

## Quick Start

### Prerequisites

- Docker installed and running
- NextDNS API key from https://my.nextdns.io/account
- Bash (Linux/macOS)
- [uv](https://docs.astral.sh/uv/) for patching the catalog YAML during E2E setup

### Setup

1. Copy the environment file template:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set your `NEXTDNS_API_KEY`:

   ```bash
   NEXTDNS_API_KEY=your-actual-api-key-here
   NEXTDNS_READABLE_PROFILES=ALL
   NEXTDNS_WRITABLE_PROFILES=ALL
   ```

3. (Optional) Enable write operations:

   ```bash
   ALLOW_LIVE_WRITES=true
   ```

   **Note:** When writes are disabled (default), only read-only tools are executed. Write operations create an isolated validation profile that is deleted after testing.

### Running the E2E Test

Default (slim) variant:
```bash
./scripts/gateway_e2e_run.sh
```

Alpine variant:
```bash
./scripts/gateway_e2e_run.sh .env alpine
```

Or specify a custom environment file:
```bash
./scripts/gateway_e2e_run.sh custom.env
./scripts/gateway_e2e_run.sh custom.env alpine
```

### Running Tools Only (Without E2E Setup)

If you already have a running Docker MCP Gateway container:

```bash
./scripts/run_all_tools.sh <allow_writes> <variant>
```

Examples:
```bash
# Read-only, slim variant (default)
./scripts/run_all_tools.sh false slim

# With writes, alpine variant
./scripts/run_all_tools.sh true alpine
```

## Configuration

All scripts use environment variables loaded from `.env` files:

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `NEXTDNS_API_KEY` | Your NextDNS API key | - | Yes |
| `ALLOW_LIVE_WRITES` | Enable write operations | false | No |
| `NEXTDNS_READABLE_PROFILES` | Profiles allowed for reads | ALL | No |
| `NEXTDNS_WRITABLE_PROFILES` | Profiles allowed for writes | ALL | No |
| `NEXTDNS_READ_ONLY` | Enable read-only mode | false | No |

### E2E Harness Environment

`ai_e2e_harness.py` additionally honors:

| Variable | Description | Default |
|----------|-------------|---------|
| `NEXTDNS_E2E_HARNESS_CMD` | Override the actor harness command (shell-ish string) | the opencode default template |
| `NEXTDNS_E2E_MODEL` | Model for the `{model}` placeholder | the harness's configured default |
| `NEXTDNS_MCP_PYTHON` | Python used to launch the MCP server | the running interpreter |
| `NEXTDNS_MCP_ARGS` | Args for the MCP server (space-separated) | `-m nextdns_mcp.server` |
| `NEXTDNS_MCP_CWD` | Working directory for the MCP server | the repo root |

## Safety Features

### Profile Isolation

When `ALLOW_LIVE_WRITES=true`, the scripts:
1. Create an isolated "E2E Test Profile [timestamp]" for testing
2. Restrict all write/delete operations to only this profile
3. Delete the profile during cleanup

**No other profiles will be modified or deleted.**

### Write Operation Gating

With `ALLOW_LIVE_WRITES=false` (default):
- Only read-only tools are executed
- All write operations are skipped
- Report marks skipped tools with reason: "Write operations disabled (ALLOW_LIVE_WRITES=false)"

### Preflight Validation

The script performs preflight checks before executing any tools:
- Verifies tools can be enumerated
- Exits with helpful error messages if setup is incorrect

## Artifacts

All scripts produce machine-readable artifacts in the `artifacts/` directory:

### `tools_report_<variant>.jsonl`

NDJSON (newline-delimited JSON) file with one entry per tool execution:

```json
{
  "tool": "manageProfiles",
  "status": "OK",
  "args": "operation=get profile_id=abc123",
  "duration": "12s",
  "timestamp": "2025-10-31T12:34:56Z"
}
```

For the default slim variant the file is `artifacts/tools_report_slim.jsonl`; for Alpine it is `artifacts/tools_report_alpine.jsonl`.

### `test_profile_id.txt`

Created when a validation profile is generated. Contains the profile ID for cleanup.

### Viewing Reports

```bash
# View all results
jq . artifacts/tools_report_slim.jsonl

# Filter by status
jq 'select(.status == "FAILED")' artifacts/tools_report_slim.jsonl
jq 'select(.status == "SKIPPED")' artifacts/tools_report_slim.jsonl
```

## Troubleshooting

### Container Not Running

**Error:** `Failed to enumerate tools from Docker MCP`

**Solution:**
1. Check if container exists: `docker ps -a`
2. Check gateway logs: `docker mcp logs`
3. Or run the E2E script to create and start it

### Tool Enumeration Failed

**Error:** `No tools found`

**Solutions:**
1. Check gateway logs: `docker mcp logs`
2. Verify catalog was imported: `docker mcp catalog ls`
3. Restart the gateway and re-import catalog

### API Key Not Set

**Error:** `NEXTDNS_API_KEY is not set or is the default placeholder`

**Solution:**
1. Edit your `.env` file
2. Set `NEXTDNS_API_KEY=your-actual-api-key-here`
3. Get your API key from https://my.nextdns.io/account

### Docker Permissions (Linux)

**Error:** `permission denied while trying to connect to the Docker daemon`

**Solution:**
```bash
sudo usermod -aG docker $USER
# Log out and log back in for changes to take effect
```

## Advanced Usage

### Continuous Integration

For CI/CD pipelines:

```bash
# Set required environment variables
export NEXTDNS_API_KEY=your-api-key
export NEXTDNS_READABLE_PROFILES=ALL
export NEXTDNS_WRITABLE_PROFILES=ALL
export ALLOW_LIVE_WRITES=true

# Run E2E tests for both variants
./scripts/gateway_e2e_run.sh
./scripts/gateway_e2e_run.sh .env alpine

# Exit code indicates success (0) or failure (non-zero)
```

## Architecture Notes

### Why Shell-First?

1. **Matches operator workflow** - Operators import catalogs and use `docker mcp` CLI in production
2. **Verifies CLI behavior** - Tests the same command-line parsing that real users experience
3. **Portable** - Just Docker, Bash, and `uv` for catalog patching; no runtime Python/Node.js dependencies for the test execution itself

### Why No HTTP API?

The Docker MCP Gateway is CLI-driven. There is no HTTP REST API to call tools directly. All interaction must go through `docker mcp` commands from the host.

This design ensures tool invocation matches the documented MCP protocol.

## See Also

- [Docker MCP Gateway Documentation](../docs/docker-mcp-gateway.md)
- [Project README](../README.md)
