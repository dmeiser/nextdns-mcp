# NextDNS MCP Server

A Model Context Protocol (MCP) server for the NextDNS API, built with FastMCP and generated from OpenAPI specifications.

## Overview

This project provides an MCP server that exposes NextDNS API operations as tools that can be used by AI assistants and other MCP clients. The server is automatically generated from a comprehensive OpenAPI specification using the FastMCP library.

## Features

- **32 NextDNS API operations** exposed as MCP tools
- **Profile Management**: List and view NextDNS profiles
- **Settings Configuration**: Get and update profile settings
- **Analytics**: Comprehensive DNS query analytics and statistics
- **Content Lists**: Manage denylist, allowlist, and parental control
- **Security**: TLD blocking configuration
- **Privacy**: Blocklist and native tracking protection management
- **OpenAPI-Generated**: Server automatically generated from [nextdns-openapi.yaml](nextdns-openapi.yaml)
- **Docker Support**: Containerized deployment with Docker
- **Safety Mechanisms**: Write operation protections and validation

## Quick Start

### Prerequisites

- Python 3.13+
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
   ```

### Running with Docker

1. Build the Docker image:
   ```bash
   docker build -t nextdns-mcp:latest .
   ```

2. Run the container:
   ```bash
   docker run -d \
     --name nextdns-mcp \
     --env-file .env \
     -p 8000:8000 \
     nextdns-mcp:latest
   ```

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

1. **OpenAPI Specification** ([nextdns-openapi.yaml](nextdns-openapi.yaml)): Complete NextDNS API documentation
2. **FastMCP Generation**: Server automatically generated using `FastMCP.from_openapi()`
3. **HTTP Client**: Authenticated `httpx.AsyncClient` for NextDNS API calls
4. **MCP Protocol**: Tools, resources, and prompts exposed via Model Context Protocol

### Key Components

- `nextdns-openapi.yaml`: OpenAPI 3.0 specification for NextDNS API
- `src/nextdns_mcp/server.py`: FastMCP server implementation
- `Dockerfile`: Container definition for deployment
- `AGENTS.md`: Development guidelines and safety rules

## Available Tools

The server exposes 32 tools organized into categories:

### Profile Management
- `listProfiles`: List all profiles
- `getProfile`: Get profile details

### Settings
- `getSettings`: Get profile settings
- `updateSettings`: Update profile settings

### Analytics (11 tools)
- Query logs, status analytics, DNSSEC stats, encryption stats
- IP version analytics, protocol analytics, top destinations
- Device analytics, top root domains, GAFAM analytics, geographic stats

### Content Lists (9 tools)
- Denylist: get, add, remove
- Allowlist: get, add, remove
- Parental Control: get, add, remove

### Security (3 tools)
- Security TLDs: get, add, remove

### Privacy (6 tools)
- Privacy Blocklists: get, add, remove
- Native Tracking Protection: get, add, remove

## Safety Mechanisms

Per `AGENTS.md` guidelines:

- **Profile Deletion Forbidden**: Profile deletion is not exposed
- **Test Profile Only**: Write operations must use designated test profiles
- **Profile ID Verification**: All write operations verify the target profile
- **Automatic Restoration**: Tests restore original state when possible

## Development

### Project Structure

```
nextdns-mcp/
├── src/nextdns_mcp/
│   ├── __init__.py
│   └── server.py          # FastMCP server implementation
├── nextdns-openapi.yaml   # OpenAPI specification
├── pyproject.toml         # Poetry dependencies
├── Dockerfile             # Container definition
├── .env                   # Configuration (not in git)
├── AGENTS.md             # Development guidelines
└── README.md             # This file
```

### Updating the API

To add or modify NextDNS API operations:

1. Edit `nextdns-openapi.yaml` to add/update endpoints
2. Rebuild the Docker image or restart the local server
3. FastMCP will automatically regenerate tools from the updated spec

### Testing

```bash
# Run tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=nextdns_mcp

# Run specific test file
poetry run pytest tests/test_server.py
```

## Docker MCP Gateway

To register with Docker MCP Gateway:

```bash
# Enable the server
docker mcp server enable nextdns-mcp

# List available tools
docker mcp tools ls

# Call a tool
docker mcp tools call listProfiles
```

## LMStudio Integration

Connect LMStudio to the running container:

```json
{
  "name": "NextDNS MCP Server",
  "type": "http",
  "baseUrl": "http://localhost:8000",
  "endpoints": {
    "tools": "/tools",
    "invoke": "/invoke/{function_name}"
  }
}
```

## License

See repository license for details.

## Contributing

1. Follow guidelines in `AGENTS.md`
2. Maintain OpenAPI spec accuracy
3. Test all changes with designated test profiles
4. Never commit `.env` or API keys
