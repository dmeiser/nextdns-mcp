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

Create a config file (gateway-config.yaml)
```yaml
servers:
  nextdns:
    config:
      nextdns:
        # Default profile used when profile_id is omitted
        default_profile: "abc123"
        # HTTP timeout in seconds (string)
        http_timeout: "45"
        # Access control lists (strings): "ALL", "" (deny all), or comma-separated IDs
        readable_profiles: "ALL"
        writable_profiles: "test789"
        # Read-only mode disables all writes (use "true" or "false")
        read_only: "true"
```

Apply and verify
```bash
# Apply configuration from file
docker mcp config write .\gateway-config.yaml
# View current configuration
docker mcp config read
```

Notes
- All values are strings (Gateway stores strings); use "true"/"false" for booleans.
- If a key is omitted, the server falls back to its built-in default.
- You can also set environment variables when running without Gateway (see Configuration).


## Verify

```bash
# Tools should be listed
docker mcp tools ls

# Call a tool
docker mcp tools call listProfiles '{}'
```

## Example calls

```bash
# DoH lookup (uses default profile if set)
docker mcp tools call dohLookup '{"domain":"example.com","record_type":"A","profile_id":"YOUR_PROFILE_ID"}'

# Get profile settings
docker mcp tools call getSettings '{"profile_id":"abc123"}'

# Bulk update denylist (JSON array string parameter)
# Bash/Zsh
docker mcp tools call updateDenylist '{"profile_id":"abc123","entries":"[\"ads.example.com\",\"tracker.net\"]"}'
# PowerShell
docker mcp tools call updateDenylist '{"profile_id":"abc123","entries":"[\"ads.example.com\",\"tracker.net\"]"}'
```

## Troubleshooting
- Missing tools: re-run catalog import and ensure the image tag matches catalog.yaml.
- 403 access denied: review [Configuration](configuration.md) access controls and profile_id.
- Secret not set: run `docker mcp secret ls` and set `nextdns.api_key`.
