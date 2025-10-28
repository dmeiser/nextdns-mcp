# NextDNS MCP Server - AI Agent Instructions

This document provides essential guidance for AI agents working on the NextDNS MCP Server codebase.

## 1. Core Architecture & Purpose

- **Purpose**: This project is a Python-based Model Context Protocol (MCP) server for the NextDNS API. It exposes NextDNS functionality as "tools" for AI agents to use.
- **Core Technology**: The server is built using the `fastmcp` library.
- **Declarative Approach**: The server's tools are generated automatically from an OpenAPI specification file: `src/nextdns_mcp/nextdns-openapi.yaml`. **To add or change API functionality, you must edit this YAML file.** The server code in `src/nextdns_mcp/server.py` then uses `FastMCP.from_openapi()` to create the tools.
- **Configuration**: All configuration is managed via environment variables (e.g., `NEXTDNS_API_KEY`). See `src/nextdns_mcp/config.py` for details. Secrets should never be hardcoded.

## 2. Development Workflow

### Running the Server
- **Local Development**: Use Poetry to manage dependencies and run the server.
  ```bash
  # Install dependencies
  poetry install

  # Run the server
  poetry run python -m src.nextdns_mcp.server
  ```
- **Docker**: A multi-stage `Dockerfile` is provided for building a lean, production-ready image.
  ```bash
  docker build -t nextdns-mcp .
  ```

### Testing Strategy (Critical)
This project uses a three-tier testing approach. Understanding it is crucial for making safe and reliable changes.

1.  **Unit Tests (`tests/unit/`)**:
    - Fast, isolated tests using `pytest` and mocks.
    - **Run command**: `poetry run pytest tests/unit`
    - **Coverage**: Maintain >=85% coverage. Run with `poetry run pytest --cov=src/nextdns_mcp`.

2.  **Integration Tests (`tests/integration/`)**:
    - Verify server initialization and tool registration.
    - `test_server_init.py` is a key file here.
    - **Run command**: `poetry run pytest tests/integration`

3.  **Live API Validation (`tests/integration/test_live_api.py`)**:
    - **CRITICAL**: This is an end-to-end test against the *live* NextDNS API. It is the primary way to validate that all 55 MCP tools work correctly.
    - **Run command**:
      ```bash
      export NEXTDNS_API_KEY="your_key_here"
      poetry run python tests/integration/test_live_api.py
      ```
    - **Safety**: The script creates a temporary "Validation Profile" to avoid altering existing user configurations. It will prompt for deletion upon completion.
    - **Requirement**: Before reporting completion of any feature or fix that touches an API endpoint, you **must** run this script and ensure all tests pass.

## 3. Safety Rules (Non-Negotiable)

- **NEVER Delete Profiles Without Permission**: The `deleteProfile` tool is powerful and destructive. Do not use it unless explicitly instructed and confirmed by the user. The live API test script is permitted to delete the temporary profile it creates, but only after user confirmation.
- **Write Operations on Test Profiles Only**: When making changes that involve creating, updating, or deleting resources (e.g., adding a domain to a denylist), always target a designated test profile.
- **Invoke MCP Tools, Don't Call APIs Directly**: When writing tests, especially in `test_live_api.py`, do not use `httpx` to call the NextDNS API directly. Instead, import the server instance and use the MCP tools to ensure the entire stack is tested.
  ```python
  # Correct way to test in test_live_api.py
  from nextdns_mcp import server

  # Get tools
  tools = await server.mcp.get_tools()

  # Call a tool
  await tools['getProfile'].run(arguments={'profile_id': 'some_id'})
  ```

## 4. Key Files & Directories

- `src/nextdns_mcp/nextdns-openapi.yaml`: The source of truth for all API-based tools.
- `src/nextdns_mcp/server.py`: The main server entrypoint. Contains `FastMCP.from_openapi()` logic and any custom tool implementations (like `dohLookup`).
- `src/nextdns_mcp/config.py`: Handles loading of environment variables and API keys.
- `tests/integration/test_live_api.py`: The script for end-to-end validation of all tools.
- `AGENTS.md`: Contains the original, more detailed rules. Refer to it for specifics not covered here.
- `Dockerfile`: Defines the containerized build and deployment.
