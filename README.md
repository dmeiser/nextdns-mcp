# NextDNS MCP Server

A Model Context Protocol (MCP) server for the NextDNS API, built with FastMCP and generated from OpenAPI specifications.

## Overview

This project provides an MCP server that exposes NextDNS API operations as tools that can be used by AI assistants and other MCP clients. The server is automatically generated from a comprehensive OpenAPI specification using the FastMCP library.

## Features

- **Domain-grouped CRUD tools** exposing the full NextDNS API surface through ~8 high-level tools
- **Profile Management**: Full CRUD operations - create, read, update, and delete profiles
- **Profile Access Control**: Fine-grained read/write restrictions per profile, with read-only mode support
- **DNS-over-HTTPS Testing**: Perform DoH lookups to test DNS resolution through profiles
- **Settings Configuration**: Comprehensive grouped settings management including logs, block page, and performance
- **Logs**: Query log retrieval, download, and clearing
- **Analytics**: Comprehensive DNS query analytics and statistics, including time-series and plotting
- **Content Lists**: Manage denylist, allowlist, privacy blocklists, native tracking, security TLDs, and parental control
- **Security**: Complete security settings and TLD blocking configuration
- **Privacy**: Privacy settings, blocklists, and native tracking protection management
- **Parental Control**: Settings management with safe search and YouTube restrictions
- **OpenAPI-backed**: Tool behaviors are driven by [nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml)
- **Docker MCP Gateway**: Full integration with Docker's MCP Gateway for secure, isolated deployment
- **Docker Support**: Containerized deployment with proper OCI labels
- **Safety Mechanisms**: Write operation protections and validation

## Documentation

Complete documentation can be found in [docs/index.md](docs/index.md).

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
   NEXTDNS_TEST_PROFILE=test_profile_id     # For write operation tests
   
   # Optional: Profile access control (see Profile Access Control section)
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

   Note: MCP servers use stdio (standard input/output) for communication, not HTTP ports.

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

## Architecture

This server uses a modern, declarative approach:

1. **OpenAPI Specification** ([nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml)): Complete NextDNS API documentation
2. **FastMCP Foundation**: Server initialized using `FastMCP.from_openapi()`, with atomic tools removed and replaced by grouped CRUD tools
3. **HTTP Client**: Authenticated `httpx.AsyncClient` with profile-level access control for NextDNS API calls
4. **MCP Protocol**: Tools, resources, and prompts exposed via Model Context Protocol

### Key Components

- `src/nextdns_mcp/nextdns-openapi.yaml`: OpenAPI 3.0 specification for NextDNS API
- `src/nextdns_mcp/server.py`: FastMCP server implementation
- `catalog.yaml`: Docker MCP Gateway catalog entry with server metadata
- `Dockerfile`: Container definition with OCI labels for MCP Gateway
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
