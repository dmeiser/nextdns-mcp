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

## What your client will see first

By default the server discloses its tools progressively: `tools/list` shows only
`search_tools` and `call_tool`, and the 80 operation tools are found by searching
— ask `search_tools` for what you want to do, then call the name it returned
through `call_tool`. The hidden tools are still callable; the listing is what
changes. Set `NEXTDNS_TOOL_SEARCH=off` to have the server advertise all of them
up front instead (see [Configuration](configuration.md)).

If you are coming from a 3.x server, see the [4.0.0 upgrade
notes](../README.md#400-upgrade-notes) in the README: the grouped tools are gone,
each operation is its own tool, and two error codes were removed.
