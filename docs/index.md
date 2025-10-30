# NextDNS MCP Server

A Model Context Protocol (MCP) server that exposes the NextDNS API as tools for AI assistants and other MCP clients. Use it via Docker MCP Gateway (recommended) or run it locally with Python/Poetry.

## What you can do
- Manage profiles (create, read, update, delete)
- Test DNS resolution through your profiles with DNS-over-HTTPS (DoH)
- Inspect logs and analytics
- Configure security, privacy, and parental controls

## Runtimes
- Docker MCP Gateway (recommended)
- Docker CLI (stdin/stdout program)
- Local (Python 3.12+ with Poetry)

## Quick links
- Getting Started: [getting-started.md](getting-started.md)
- Configuration Reference: [configuration.md](configuration.md)
- Docker MCP Gateway Guide: [docker-mcp-gateway.md](docker-mcp-gateway.md)
- Usage and Examples: [usage.md](usage.md)
- Safety Guidelines: [safety.md](safety.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
- FAQ: [faq.md](faq.md)

## Requirements
- NextDNS API key from https://my.nextdns.io/account
- Internet access to api.nextdns.io and dns.nextdns.io

## Notes
- MCP servers use stdio (no HTTP port). The Docker container reads/writes on stdin/stdout.
- Write operations can be restricted or fully disabled; see [Configuration](configuration.md) and [Safety](safety.md).
