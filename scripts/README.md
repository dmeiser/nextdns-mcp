# NextDNS MCP Gateway E2E Testing Scripts

This directory contains shell scripts for end-to-end validation of the NextDNS MCP Gateway via the Docker MCP Gateway CLI.

## Overview

The E2E testing approach is **CLI-driven** and **shell-first**, meaning:
- There is **no HTTP API** to call tools directly
- All integration testing uses the `docker mcp` CLI or in-container `mcp` binary via `docker exec`
- Tests verify the CLI parsing and quoting behavior that operators will use in production

## Scripts

### E2E Drivers

- **`gateway_e2e_run.sh`** - Bash E2E driver
- **`gateway_e2e_run.ps1`** - PowerShell E2E driver

These scripts perform the complete E2E workflow:
1. Load configuration from `.env` file
2. Build the Docker image
3. Import `catalog.yaml` into the gateway
4. Start the Docker MCP Gateway container
5. Wait for gateway readiness
6. Run all tools via `run_all_tools.*` scripts
7. Clean up (stop container) and optionally delete validation profile

### Tool Execution Scripts

- **`run_all_tools.sh`** - Bash tool enumeration and invocation
- **`run_all_tools.ps1`** - PowerShell tool enumeration and invocation

These scripts:
- Enumerate all available tools from the Docker MCP Gateway (`docker mcp tools list`)
- Perform preflight validation (bash only) to ensure tools exist before invocation
- Execute each tool with appropriate test parameters
- Skip write operations unless `ALLOW_LIVE_WRITES=true`
- Produce machine-readable JSONL reports in `artifacts/tools_report.jsonl`

## Quick Start

### Prerequisites

- Docker installed and running
- NextDNS API key from https://my.nextdns.io/account
- PowerShell (Windows) or Bash (Linux/macOS)

### Setup

1. Copy the environment file template:

   **PowerShell:**
   ```powershell
   Copy-Item .env.example .env
   ```

   **Bash:**
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

   **Note:** When writes are disabled (default), only read-only tools are executed. Write operations create an isolated validation profile that can be safely deleted after testing.

### Running the E2E Test

**PowerShell:**
```powershell
.\scripts\gateway_e2e_run.ps1
```

**Bash:**
```bash
./scripts/gateway_e2e_run.sh
```

Or specify a custom environment file:

**PowerShell:**
```powershell
.\scripts\gateway_e2e_run.ps1 -EnvFile custom.env
```

**Bash:**
```bash
./scripts/gateway_e2e_run.sh custom.env
```

### Running Tools Only (Without E2E Setup)

If you already have a running Docker MCP Gateway container:

**PowerShell:**
```powershell
.\scripts\run_all_tools.ps1 -ContainerName "nextdns-mcp-gateway" -AllowWrites $false
```

**Bash:**
```bash
./scripts/run_all_tools.sh nextdns-mcp-gateway "" false
```

## Configuration

All scripts use environment variables loaded from `.env` files:

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `NEXTDNS_API_KEY` | Your NextDNS API key | - | Yes |
| `DOCKER_MCP_GATEWAY_PORT` | Gateway port | 3000 | No |
| `DOCKER_MCP_GATEWAY_CONTAINER` | Container name | nextdns-mcp-gateway | No |
| `ALLOW_LIVE_WRITES` | Enable write operations | false | No |
| `GATEWAY_READINESS_TIMEOUT` | Readiness timeout (seconds) | 60 | No |
| `GATEWAY_READINESS_INTERVAL` | Readiness check interval (seconds) | 2 | No |
| `NEXTDNS_READABLE_PROFILES` | Profiles allowed for reads | ALL | No |
| `NEXTDNS_WRITABLE_PROFILES` | Profiles allowed for writes | ALL | No |
| `NEXTDNS_READ_ONLY` | Enable read-only mode | false | No |

## Safety Features

### Profile Isolation

When `ALLOW_LIVE_WRITES=true`, the scripts:
1. Create an isolated "Validation Profile [timestamp]" for testing
2. Record the profile ID in `artifacts/validation_profile_id.txt`
3. Restrict all write/delete operations to only this profile
4. Prompt before deleting the profile during cleanup

**No other profiles will be modified or deleted.**

### Write Operation Gating

With `ALLOW_LIVE_WRITES=false` (default):
- Only read-only tools are executed
- All write operations are skipped
- Report marks skipped tools with reason: "Write operations disabled (ALLOW_LIVE_WRITES=false)"

### Preflight Validation (Bash Only)

The bash script performs preflight checks before executing any tools:
- Verifies the container is running
- Validates tool enumeration works
- Exits with helpful error messages if setup is incorrect

## Artifacts

All scripts produce machine-readable artifacts in the `artifacts/` directory:

### `tools_report.jsonl`

NDJSON (newline-delimited JSON) file with one entry per tool execution:

```json
{
  "tool": "getProfile",
  "status": "success",
  "args": "{\"profile_id\":\"abc123\"}",
  "exit_code": 0,
  "stdout": "{\"id\":\"abc123\",\"name\":\"My Profile\"}",
  "stderr": "",
  "duration": 0.452,
  "skip_reason": "",
  "timestamp": "2025-10-31T12:34:56Z"
}
```

### `validation_profile_id.txt`

Created when a validation profile is generated. Contains the profile ID for cleanup.

### Viewing Reports

**PowerShell:**
```powershell
# View all results
Get-Content artifacts/tools_report.jsonl | ForEach-Object { $_ | ConvertFrom-Json }

# Filter by status
Get-Content artifacts/tools_report.jsonl | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.status -eq 'failed' }
Get-Content artifacts/tools_report.jsonl | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.status -eq 'skipped' }
```

**Bash:**
```bash
# View all results
jq . artifacts/tools_report.jsonl

# Filter by status
jq 'select(.status == "failed")' artifacts/tools_report.jsonl
jq 'select(.status == "skipped")' artifacts/tools_report.jsonl
```

## Troubleshooting

### Container Not Running

**Error:** `Container 'nextdns-mcp-gateway' is not running`

**Solution:**
1. Check if container exists: `docker ps -a`
2. Start the container: `docker start nextdns-mcp-gateway`
3. Or run the E2E script to create and start it

### Tool Enumeration Failed

**Error:** `Failed to enumerate tools from container`

**Solutions:**
1. Check container logs: `docker logs nextdns-mcp-gateway`
2. Verify catalog was imported: `docker exec nextdns-mcp-gateway mcp tools list`
3. Restart the container and re-import catalog

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

### JSON Quoting Issues

The scripts use `docker exec` with stdin for complex JSON to avoid shell quoting problems:

**Bash:**
```bash
echo '{"profile_id":"abc123"}' | docker exec -i container mcp tools call getProfile --stdin
```

**PowerShell:**
```powershell
'{"profile_id":"abc123"}' | docker exec -i container mcp tools call getProfile --stdin
```

This pattern is safe across both shells and avoids escaping issues.

## Advanced Usage

### Custom Profile Testing

To test with a specific profile (without creating a validation profile):

**PowerShell:**
```powershell
.\scripts\run_all_tools.ps1 -ProfileId "abc123" -AllowWrites $true
```

**Bash:**
```bash
./scripts/run_all_tools.sh nextdns-mcp-gateway "abc123" true
```

**Warning:** Only use test profiles! Never use production profiles with write operations enabled.

### Manual Cleanup

If the E2E script is interrupted, manually clean up:

```bash
# Stop and remove container
docker stop nextdns-mcp-gateway
docker rm nextdns-mcp-gateway

# Delete validation profile (if created)
PROFILE_ID=$(cat artifacts/validation_profile_id.txt)
docker exec nextdns-mcp-gateway mcp tools call deleteProfile --args "{\"profile_id\":\"$PROFILE_ID\"}"
```

### Continuous Integration

For CI/CD pipelines:

```bash
# Set required environment variables
export NEXTDNS_API_KEY=your-api-key
export NEXTDNS_READABLE_PROFILES=ALL
export NEXTDNS_WRITABLE_PROFILES=ALL
export ALLOW_LIVE_WRITES=true

# Run E2E test
./scripts/gateway_e2e_run.sh

# Exit code indicates success (0) or failure (non-zero)
```

## Architecture Notes

### Why Shell-First?

1. **Matches operator workflow** - Operators import catalogs and use `docker mcp` CLI in production
2. **Verifies CLI behavior** - Tests the same command-line parsing that real users experience
3. **Cross-platform** - PowerShell (Windows) and Bash (Linux/macOS) scripts work identically
4. **Portable** - No Python/Node.js dependencies, just Docker and shell

### Why No HTTP API?

The Docker MCP Gateway is CLI-driven. There is no HTTP REST API to call tools directly. All interaction must go through:
- `docker mcp` commands from the host
- `mcp` binary inside the container via `docker exec`

This design ensures tool invocation matches the documented MCP protocol.

## See Also

- [Docker MCP Gateway Documentation](../docs/docker-mcp-gateway.md)
- [NextDNS Validation Documentation](../NextDNS%20Validation.md)
- [Project README](../README.md)
