# Getting Started

Set up the NextDNS MCP Server and make your first tool call in minutes.

## Prerequisites
- Docker 24+ (for Docker MCP Gateway) OR Python 3.12+ and Poetry
- NextDNS API key

## Option A: Docker MCP Gateway (recommended)

1) Build the image

```bash
# From the repo root
docker build -t nextdns-mcp:latest .
```

2) Import the catalog and enable the server

```bash
# Register the server in Docker MCP Gateway
docker mcp catalog import ./catalog.yaml

# Enable the server (registry key: "nextdns")
docker mcp server enable nextdns
```

3) Set the API key secret

```bash
# Simple
docker mcp secret set nextdns.api_key=YOUR_API_KEY

# More secure (avoids shell history)
# Bash/Zsh
echo "YOUR_API_KEY" | docker mcp secret set nextdns.api_key
# PowerShell
Write-Output "YOUR_API_KEY" | docker mcp secret set nextdns.api_key
```

4) (Optional) Configure server settings via Gateway

Create gateway-config.yaml
```yaml
servers:
  nextdns:
    config:
      nextdns:
        default_profile: "abc123"
        http_timeout: "45"
        readable_profiles: "ALL"
        writable_profiles: "test789"
        read_only: "true"
```

Apply and verify
```bash
# Apply from file
docker mcp config write .\gateway-config.yaml
# View current config
docker mcp config read
```

5) Verify tools are available

```bash
docker mcp tools ls
```

6) Make your first calls

```bash
# List profiles (requires API key)
docker mcp tools call listProfiles '{}'

# Test DNS resolution via DoH (set default_profile first or pass --profile_id)
docker mcp tools call dohLookup '{"domain":"google.com","record_type":"A","profile_id":"YOUR_PROFILE_ID"}'
```

## Option B: Run with Docker (without Gateway)

```bash
docker run -i --rm \
  -e NEXTDNS_API_KEY=YOUR_API_KEY \
  nextdns-mcp:latest
```

Then configure your MCP client (e.g., Claude Desktop) to run the above Docker command.

## Option C: Run locally with Poetry

```bash
# Install dependencies
poetry install

# Run the server
poetry run python -m nextdns_mcp.server
```

Tip: For local development, set environment variables in your shell before launching (see [Configuration](configuration.md)).
