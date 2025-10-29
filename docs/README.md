# NextDNS MCP Server Documentation

Welcome to the NextDNS MCP Server documentation. This section covers how to configure, run, and use the server, along with a catalog of available tools.

- Start with Configuration to set environment variables and secrets
- See Usage for running locally, with Docker, and via Docker MCP Gateway
- Use the Tools page as a quick reference for all available MCP tools

## Index

- Configuration: [configuration.md](./configuration.md)
- Usage: [usage.md](./usage.md)
- Tools: [tools.md](./tools.md)

## Architecture Highlights

- OpenAPI-driven: Tools are generated from `src/nextdns_mcp/nextdns-openapi.yaml` via `FastMCP.from_openapi()`
- Custom tools: Added where FastMCP has limitations (array-body PUTs) and for DoH testing
- Access control: Environment-based, enforced by an access-controlled HTTP client
- Secrets: API key from environment or Docker secret file; configuration validated on import
