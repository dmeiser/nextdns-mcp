# Docker MCP Gateway Guide

Deploy and operate the NextDNS MCP Server with Docker MCP Gateway.

## Register and enable the server

```bash
# Build the image locally
docker build -t nextdns-mcp:latest .

# Import the catalog (registry key: "nextdns")
docker mcp catalog import ./catalog.yaml

# Enable the server
docker mcp server enable nextdns
```

## Configure secrets and config

```bash
# Set API key secret (required)
docker mcp secret set nextdns.api_key=YOUR_API_KEY

# Or via STDIN (safer)
echo "YOUR_API_KEY" | docker mcp secret set nextdns.api_key
# PowerShell
Write-Output "YOUR_API_KEY" | docker mcp secret set nextdns.api_key
```

Create a config file (config.yaml on your Desktop or anywhere convenient)
```yaml
servers:
  nextdns:
    env:
      NEXTDNS_DEFAULT_PROFILE: "a97d4e"
      NEXTDNS_HTTP_TIMEOUT: "45"
      NEXTDNS_READABLE_PROFILES: "ALL"
      NEXTDNS_WRITABLE_PROFILES: "ALL"
      NEXTDNS_READ_ONLY: "false"
```

Apply configuration
```bash
# Copy config to Docker MCP directory
# Linux/macOS
cp config.yaml ~/.docker/mcp/config.yaml

# Windows PowerShell
Copy-Item config.yaml "$env:USERPROFILE\.docker\mcp\config.yaml" -Force

# Verify it was copied correctly
cat ~/.docker/mcp/config.yaml  # Linux/macOS
Get-Content "$env:USERPROFILE\.docker\mcp\config.yaml"  # Windows
```

Notes
- The config file must be placed at `~/.docker/mcp/config.yaml` for Docker MCP Gateway to read it.
- Use `env:` with direct environment variable names (e.g., `NEXTDNS_READABLE_PROFILES`), not nested `config:` structure.
- Set `NEXTDNS_READABLE_PROFILES` and `NEXTDNS_WRITABLE_PROFILES` to `"ALL"` to allow all profiles.
- Set to `""` (empty string) to deny all, or comma-separated profile IDs like `"abc123,def456"`.
- You can also set environment variables when running without Gateway (see Configuration).


## Verify

```bash
# Tools should be listed
docker mcp tools ls

# Call a tool
docker mcp tools call listProfiles
```

## Example calls

```bash
# DoH lookup (uses default profile if set)
docker mcp tools call dohLookup domain=example.com record_type=A profile_id=YOUR_PROFILE_ID

# Get profile settings
docker mcp tools call getSettings profile_id=abc123

# Add individual entries to denylist
docker mcp tools call addToDenylist profile_id=abc123 id=ads.example.com
docker mcp tools call addToDenylist profile_id=abc123 id=tracker.net

# Bulk replace with custom tools (pass JSON array string)
docker mcp tools call updateDenylist '{"profile_id":"abc123","entries":"[\"ads.example.com\",\"tracker.net\"]"}'
docker mcp tools call updateAllowlist '{"profile_id":"abc123","entries":"[\"safe.com\",\"trusted.org\"]"}'
```

## Troubleshooting
- Missing tools: re-run catalog import and ensure the image tag matches catalog.yaml.
- 403 access denied: review [Configuration](configuration.md) access controls and profile_id.
- Secret not set: run `docker mcp secret ls` and set `nextdns.api_key`.
