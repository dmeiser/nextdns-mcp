# NextDNS MCP Server

A Model Context Protocol (MCP) server for the NextDNS API, built with FastMCP and generated from a bundled OpenAPI document.

## Overview

This project provides an MCP server that exposes NextDNS API operations as tools that can be used by AI assistants and other MCP clients. The bundled OpenAPI document is the tool contract: the server generates one tool per operation in it, named for the operation itself (`getAllowlist`, `addToDenylist`, `getAnalyticsDevices`) and taking that endpoint's own arguments, plus the two operations no endpoint describes — `plotAnalytics` and `dohLookup`.

By default the server discloses those tools progressively: `tools/list` returns only
the synthetic `search_tools` + `call_tool` pair, and a client discovers the catalog by
searching it and calls what the search returned. The operation tools stay callable
either way — the listing is what changes, not the access. Set
`NEXTDNS_TOOL_SEARCH=off` to advertise all 80 tools in `tools/list` instead (see
[configuration](docs/configuration.md)).

## Features

- **One tool per API operation** — 78 tools named for the operation itself, each taking that endpoint's own arguments, so the name says what the call does and there is no operation argument to choose
- **Progressive tool disclosure** — by default `tools/list` exposes only `search_tools` + `call_tool` and the model finds the operation tools by searching; `NEXTDNS_TOOL_SEARCH=off` advertises the full catalog
- **Profile Management**: Full CRUD operations - create, read, update, and delete profiles
- **Profile Access Control**: Fine-grained read/write restrictions per profile, read-only mode, and a protected-profile denylist that refuses writes to named profiles even under `ALL`
- **DNS-over-HTTPS Testing**: Perform DoH lookups to test DNS resolution through profiles
- **Settings Configuration**: Comprehensive settings management including logs, block page, and performance
- **Logs**: Query log retrieval, download, and clearing
- **Analytics**: Comprehensive DNS query analytics and statistics, including time-series and plotting
- **Content Lists**: Manage denylist, allowlist, privacy blocklists, native tracking, security TLDs, and parental control
- **Security**: Complete security settings and TLD blocking configuration
- **Privacy**: Privacy settings, blocklists, and native tracking protection management
- **Parental Control**: Settings management with safe search and YouTube restrictions
- **OpenAPI document as the tool contract**: The bundled [nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml) generates every published tool and drives the validation tooling
- **Docker Support**: Containerized deployment with proper OCI labels
- **Safety Mechanisms**: Write operation protections and validation

## Documentation

Complete documentation can be found in [docs/index.md](docs/index.md).

## 4.0.0 upgrade notes

- **One tool per operation.** The grouped tools of 3.x are gone. Every published tool is
  one operation of the bundled document, named for its `operationId`, taking that
  endpoint's own arguments; there is no operation to choose, and the two composite
  operations (`plotAnalytics`, `dohLookup`) are named for what they do.
- **Disclosure is on by default.** `NEXTDNS_TOOL_SEARCH` defaults to `bm25`, so
  `tools/list` answers with only `search_tools` + `call_tool`. A client that expects to
  see every tool in the listing needs `NEXTDNS_TOOL_SEARCH=off`.
- **Two error codes are gone.** `UNSUPPORTED_OPERATION` and `UNSUPPORTED_PARAMETER` were
  removed with the surface that produced them. Nothing the server can no longer do is
  addressable as a tool any more, and a rejected argument value answers
  `invalid_argument` (or `missing_required_argument` when a required one is absent) while
  an upstream refusal answers `http_error` with the upstream error document. Extra
  top-level arguments are not an error at all: they are dropped and named back in
  `ignored_arguments`. Client code branching on the removed names should branch on these.
- **Write scope has three independent knobs.** `NEXTDNS_READ_ONLY` denies every write,
  `NEXTDNS_WRITABLE_PROFILES` scopes which profiles are writable (`ALL` drops the
  scoping), and `NEXTDNS_PROTECTED_PROFILES` denies writes to the profiles it lists even
  under `ALL` — reads of them are unaffected. See [safety.md](docs/safety.md).

## Quick Start

### Prerequisites

- Python 3.12+
- uv (for development)
- Docker (for containerized deployment)
- NextDNS API key ([get one here](https://my.nextdns.io/account))

### Configuration

1. Copy the `.env.example` file to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set your NextDNS API key:
   ```env
   NEXTDNS_API_KEY=your_api_key_here
   NEXTDNS_DEFAULT_PROFILE=your_profile_id  # Optional

   # Optional: Profile access control (see docs/configuration.md).
   # There is no separate test-profile variable for writes: list a dedicated
   # sandbox profile in NEXTDNS_WRITABLE_PROFILES, or block writes entirely
   # with NEXTDNS_READ_ONLY.
   # NEXTDNS_READABLE_PROFILES=profile1,profile2
   # NEXTDNS_WRITABLE_PROFILES=test_profile
   # NEXTDNS_READ_ONLY=false
   ```

### Running with Docker

1. Build the Docker image:
   ```bash
   docker build -t nextdns-mcp:latest .
   ```

2. Run the container with environment variables:

   **Option A: Direct environment variables (simple)**
   ```bash
   docker run -i --rm \
     -e NEXTDNS_API_KEY=your_api_key_here \
     -e NEXTDNS_DEFAULT_PROFILE=your_profile_id \
     nextdns-mcp:latest
   ```

   **Option B: Docker secrets (recommended for production)**
   ```bash
   # Create secret
   echo "your_api_key_here" | docker secret create nextdns_api_key -

   # Run with Docker Swarm
   docker service create \
     --name nextdns-mcp \
     --secret nextdns_api_key \
     -e NEXTDNS_API_KEY_FILE=/run/secrets/nextdns_api_key \
     nextdns-mcp:latest
   ```

   Or for non-swarm (using mounted file):
   ```bash
   # Create a secret file
   echo "your_api_key_here" > /tmp/api_key.txt
   chmod 600 /tmp/api_key.txt

   # Run with mounted secret
   docker run -i --rm \
     -v /tmp/api_key.txt:/run/secrets/nextdns_api_key:ro \
     -e NEXTDNS_API_KEY_FILE=/run/secrets/nextdns_api_key \
     nextdns-mcp:latest
   ```

   **Option C: Environment file (development)**
   ```bash
   docker run -i --rm \
     --env-file .env \
     nextdns-mcp:latest
   ```

   Note: the commands above use stdio (standard input/output), the default transport. For the opt-in streamable-HTTP transport, see [HTTP Transport](#http-transport-network-access) below.

   **Alpine variant**

   An Alpine Linux image is also available. To build it locally, use `Dockerfile.alpine`:

   ```bash
   docker build -f Dockerfile.alpine -t nextdns-mcp:alpine .
   ```

   The published Alpine tags use the `-alpine` suffix (e.g. `nextdns-mcp:alpine`, `nextdns-mcp:2.0-alpine`). The `python:3.14-slim` image remains the recommended default.

### Running Locally (Development)

1. Install dependencies:
   ```bash
   uv sync
   ```

2. Run the server:
   ```bash
   uv run python -m nextdns_mcp.server
   ```

## HTTP Transport (network access)

By default the server uses **stdio**, which is what MCP clients (Claude Desktop, CLI tools) expect. You can switch to a **streamable-HTTP** transport for network-based clients, but the HTTP endpoint has **no built-in authentication**.

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | Set to `http` to serve over streamable-HTTP. Any other value fails startup; see [configuration](docs/configuration.md) |
| `MCP_HOST` | `127.0.0.1` | Interface to bind. Loopback-only by default |
| `MCP_PORT` | `8000` | Port to listen on |

Because there is no authentication, the default bind is **loopback-only** (`127.0.0.1`), so the HTTP endpoint is only reachable from the same host. That keeps it off the network, but on its own it does not stop a browser on that host from being tricked into reaching it (DNS rebinding) — the header validation below is what addresses that.

FastMCP's Host/Origin header validation (`FASTMCP_HTTP_HOST_ORIGIN_PROTECTION`, which mitigates DNS rebinding against that unauthenticated endpoint) is **not** enabled by default for application runs, because on a loopback bind it also enforces a Host allowlist and would reject the name your proxy or clients present. Behind a TLS-terminating proxy, enabling it takes three steps: make the server trust that proxy's forwarded headers (the `FORWARDED_ALLOW_IPS` environment variable, which defaults to `127.0.0.1` and so already covers a same-host proxy — set it to the proxy's address otherwise, and never to `*`), then set the public host in `FASTMCP_HTTP_ALLOWED_HOSTS='["mcp.example.com"]'` (a JSON array, not a bare hostname), and only then set `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=auto`. Skipping the allowlist answers `421 Misdirected Request`; skipping the trusted forwarded headers makes the server compare an `http` origin against the browser's `https` `Origin` and answer `403`. See [configuration](docs/configuration.md).

In HTTP mode the server also serves `GET /health`, a readiness check that probes the NextDNS API with the configured key: it returns 200 only when the credentials work, and 503 with a failure class (`auth` or `unreachable`) when they do not. See the [FAQ](docs/faq.md).

### Binding to a non-loopback interface (explicit opt-in)

To make the endpoint reachable from other hosts you must **explicitly** set `MCP_HOST` to a non-loopback address (e.g. `0.0.0.0` or a specific IP). The server logs a prominent `SECURITY` warning on startup when this happens, because it exposes every tool — and the full reachability of your NextDNS API key — to anyone who can reach the port.

A non-loopback bind is **only** production-suitable when it is fronted by a reverse proxy (or gateway) that provides **authentication and/or TLS**, such that unauthenticated remote callers cannot reach the `/mcp` endpoint. Recommended setup:

- Terminate TLS at the reverse proxy.
- Require authentication (e.g. a shared secret, OAuth2/OIDC, or IP allow-listing) before forwarding to the MCP server.
- Bind the MCP server itself to a private interface or `127.0.0.1` behind the proxy where possible.

```bash
# Loopback-only (default; see the Host/Origin validation note above):
MCP_TRANSPORT=http uv run python -m nextdns_mcp.server

# Non-loopback bind — MUST be placed behind an authenticating reverse proxy:
MCP_TRANSPORT=http MCP_HOST=0.0.0.0 uv run python -m nextdns_mcp.server
```

## Architecture

This server uses a modern, declarative approach:

1. **OpenAPI Specification** ([nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml)): The tool contract — every published tool is generated from it
2. **FastMCP Foundation**: `spec_tools` generates one tool per spec operation with FastMCP's OpenAPI parser; requests run through the server's own access-controlled HTTP client rather than `FastMCP.from_openapi()`'s transport (issues #141/#146)
3. **HTTP Client**: Authenticated `httpx.AsyncClient` with profile-level access control for NextDNS API calls
4. **MCP Protocol**: Tools, resources, and prompts exposed via Model Context Protocol

### Key Components

- `src/nextdns_mcp/nextdns-openapi.yaml`: OpenAPI 3.0 specification for the NextDNS API — the document every published tool is generated from; a Spectral lint workflow ([openapi-lint.yml](.github/workflows/openapi-lint.yml)) enforces explicit per-operation security declarations via [spectral.yaml](spectral.yaml), runnable locally with `npx @stoplight/spectral lint -r spectral.yaml src/nextdns_mcp/nextdns-openapi.yaml`
- `src/nextdns_mcp/server.py`: FastMCP server implementation
- `Dockerfile`: Container definition with OCI labels for container metadata
- `AGENT.md`: Development guidelines and safety rules

### Docker Tags

This project publishes official Docker images with a standardized tagging policy. The default image is based on `python:3.14-slim`; an Alpine Linux variant is also available and tagged with an `-alpine` suffix.

#### Primary (`python:3.14-slim`) tags

- `:latest`: Floating tag that tracks the most recent successful build from the `main` branch. This tag is rebuilt on changes to `main` and via scheduled rebuilds.
- `:<major>`: Floating tag for the most recent build in a given major series (e.g., `:2`). This tag is updated whenever a new image for that major line is published and may include unreleased changes if the corresponding build comes from a branch head.
- `:<major>.<minor>`: Floating tag for the most recent build in a given minor series (e.g., `:2.0`). Like `:<major>`, it is updated when new images are built for that series and may include unreleased changes.
- `:<major>.<minor>.<patch>`: Tags for specific application releases (e.g., `:2.0.3`). These are intended to be immutable once published via the release workflow.

#### Alpine tags

- `:alpine`: Floating tag for the most recent Alpine build from `main`.
- `:<major>-alpine`: Floating tag for the most recent Alpine build in a major series (e.g., `:2-alpine`).
- `:<major>.<minor>-alpine`: Floating tag for the most recent Alpine build in a minor series (e.g., `:2.0-alpine`).
- `:<major>.<minor>.<patch>-alpine`: Specific Alpine release tag (e.g., `:2.0.3-alpine`).

All floating tags (`:latest`, `:<major>`, `:<major>.<minor>` and their `-alpine` counterparts) are rebuilt regularly to include the latest OS security updates and any application changes present in the source commit used for that build. Consumers who require strict version pinning should use the full `:<major>.<minor>.<patch>` or `:<major>.<minor>.<patch>-alpine` tags.

## License

This project is released under the [MIT License](LICENSE).

## Contributing

1. See `AGENT.md` for guidelines and architecture
2. Note that NextDNS does not provide an OpenAPI specification. This is based on their documentation and may not reflect the current state of the API.
