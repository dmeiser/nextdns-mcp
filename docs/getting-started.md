# Getting Started

Set up the NextDNS MCP Server and make your first tool call in minutes.

## Prerequisites
- Docker 24+ OR Python 3.12+ and uv
- NextDNS API key

## Option A: Run with Docker

1) Build the image:

```bash
docker build -t nextdns-mcp:latest .
```

2) Run the container:

```bash
docker run -i --rm \
  -e NEXTDNS_API_KEY=YOUR_API_KEY \
  nextdns-mcp:latest
```

Then configure your MCP client (e.g., Claude Desktop) to run the above Docker command.

## Option B: Run locally with uv

```bash
# Install dependencies
uv sync

# Run the server
uv run python -m nextdns_mcp.server
```

Tip: For local development, set environment variables in your shell before launching (see [Configuration](configuration.md)).
