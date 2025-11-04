# NextDNS MCP Server

A Model Context Protocol (MCP) server for the NextDNS API, built with FastMCP and generated from OpenAPI specifications.

## Overview

This project provides an MCP server that exposes NextDNS API operations as tools that can be used by AI assistants and other MCP clients. The server is automatically generated from a comprehensive OpenAPI specification using the FastMCP library.

## Features

- **70+ NextDNS operations** exposed as MCP tools
- **Profile Management**: Full CRUD operations - create, read, update, and delete profiles
- **Profile Access Control**: Fine-grained read/write restrictions per profile, with read-only mode support
- **DNS-over-HTTPS Testing**: Perform DoH lookups to test DNS resolution through profiles
- **Settings Configuration**: Comprehensive settings management including logs, block page, and performance
- **Logs**: Query log retrieval and clearing
- **Analytics**: Comprehensive DNS query analytics and statistics (11 endpoints)
- **Content Lists**: Manage denylist, allowlist, and parental control
- **Security**: Complete security settings and TLD blocking configuration
- **Privacy**: Privacy settings, blocklists, and native tracking protection management
- **Parental Control**: Settings management with safe search and YouTube restrictions
- **OpenAPI-Generated**: Server automatically generated from [nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml)
- **Docker MCP Gateway**: Full integration with Docker's MCP Gateway for secure, isolated deployment
- **Docker Support**: Containerized deployment with proper OCI labels
- **Safety Mechanisms**: Write operation protections and validation

## Documentation

- Configuration: docs/configuration.md
- Usage: docs/usage.md


For a guided overview, see docs/index.md.

## Quick Start

### Prerequisites

- Python 3.12+
- Poetry (for development)
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

### Running Locally (Development)

1. Install dependencies:
   ```bash
   poetry install
   ```

2. Run the server:
   ```bash
   poetry run python -m nextdns_mcp.server
   ```

## Architecture

This server uses a modern, declarative approach:

1. **OpenAPI Specification** ([nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml)): Complete NextDNS API documentation
2. **FastMCP Generation**: Server automatically generated using `FastMCP.from_openapi()`
3. **HTTP Client**: Authenticated `httpx.AsyncClient` for NextDNS API calls
4. **MCP Protocol**: Tools, resources, and prompts exposed via Model Context Protocol

### Key Components

- `src/nextdns_mcp/nextdns-openapi.yaml`: OpenAPI 3.0 specification for NextDNS API
- `src/nextdns_mcp/server.py`: FastMCP server implementation
- `catalog.yaml`: Docker MCP Gateway catalog entry with server metadata
- `Dockerfile`: Container definition with OCI labels for MCP Gateway
- `AGENT.md`: Development guidelines and safety rules

## License

This project is released under the [MIT License](LICENSE).

## Contributing

1. See `AGENT.md` for guidelines and architecture
2. Note that NextDNS does not provide an OpenAPI specification. This is based on their documentation and may not reflect the current state of the API.
