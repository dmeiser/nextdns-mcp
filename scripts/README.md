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

## Quick Start

### Prerequisites

- Docker installed and running
- NextDNS API key from https://my.nextdns.io/account
- Bash (Linux/macOS)

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
  "tool": "getProfile",
  "status": "OK",
  "args": "profile_id=abc123",
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

### Running Tools with a Specific Profile

The `run_all_tools.sh` script automatically discovers or creates a test profile. If you need to test against a specific profile, set `NEXTDNS_DEFAULT_PROFILE` in your `.env` file or export it before running:

```bash
export NEXTDNS_DEFAULT_PROFILE=abc123
./scripts/run_all_tools.sh true slim
```

**Warning:** Only use test profiles! Never use production profiles with write operations enabled.

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
3. **Portable** - No Python/Node.js dependencies, just Docker and shell

### Why No HTTP API?

The Docker MCP Gateway is CLI-driven. There is no HTTP REST API to call tools directly. All interaction must go through `docker mcp` commands from the host.

This design ensures tool invocation matches the documented MCP protocol.

## See Also

- [Docker MCP Gateway Documentation](../docs/docker-mcp-gateway.md)
- [Project README](../README.md)
