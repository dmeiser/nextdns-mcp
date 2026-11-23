# NextDNS MCP Server

A Model Context Protocol (MCP) server that exposes the NextDNS API as tools for AI assistants and other MCP clients. Use it via Docker or run it locally with Python/uv.

## What you can do
- Manage profiles (create, read, update, delete)
- Test DNS resolution through your profiles with DNS-over-HTTPS (DoH)
- Inspect logs and analytics
- Configure security, privacy, and parental controls

## Runtimes
- Docker CLI (stdin/stdout container)
- Local (Python 3.12+ with uv)

## Quick links
- Getting Started: [getting-started.md](getting-started.md)
- Configuration Reference: [configuration.md](configuration.md)
- Usage and Examples: [usage.md](usage.md)
- Migrating from 3.x: [migration.md](migration.md)
- Safety Guidelines: [safety.md](safety.md)
- Generative UI trusted-host contract: [generative-ui.md](generative-ui.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
- FAQ: [faq.md](faq.md)
- Architecture decision records:
  - [ADR 0001 — OpenAPI-spec-generated tools](architecture/0001-openapi-spec-generated-tools.md)
  - [ADR 0002 — Generative UI: an opt-in provider that runs model-written code](architecture/0002-generative-ui-opt-in-provider.md)
  - [ADR 0003 — Progressive tool disclosure by BM25 search](architecture/0003-bm25-progressive-tool-disclosure.md)
  - [ADR 0004 — Lazy process singletons and one shutdown point](architecture/0004-lazy-singletons-and-server-lifespan.md)
  - [ADR 0005 — The dashboard package over neutral chart modules](architecture/0005-dashboard-package-split.md)

## Requirements
- NextDNS API key from https://my.nextdns.io/account
- Internet access to api.nextdns.io and dns.nextdns.io

## Notes
- MCP servers use stdio by default; the Docker container reads/writes on stdin/stdout. A streamable-HTTP transport is also supported - see the README's HTTP Transport section and [Configuration](configuration.md).
- Write operations can be restricted or fully disabled; see [Configuration](configuration.md) and [Safety](safety.md).
