# Scripts and Testing Prompts

This directory contains helper scripts and validation prompts for the NextDNS MCP server.

## Overview

- **`validate_schema.py`** - Validates JSON responses against OpenAPI schema definitions to ensure responses conform to expected schemas for the grouped tools.
- **`ai_agent_e2e_prompt.md`** - End-to-end testing prompt for an AI agent (e.g. Claude Desktop, Cursor) to exercise the full NextDNS MCP server tool surface against a dedicated test profile.

## Usage

### Schema Validation

```bash
uv run python scripts/validate_schema.py <tool_name> <json_response>
```

Example:
```bash
uv run python scripts/validate_schema.py manageProfiles '{"data":[{"id":"abc123","name":"My Profile"}]}'
```

### AI Agent E2E Validation

See [ai_agent_e2e_prompt.md](ai_agent_e2e_prompt.md) for instructions on using an AI agent to validate tools against a live test profile.
