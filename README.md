# NextDNS MCP Server

A Model Context Protocol (MCP) server for the NextDNS API, built with FastMCP and generated from OpenAPI specifications.

## Overview

This project provides an MCP server that exposes NextDNS API operations as tools that can be used by AI assistants and other MCP clients. The server is automatically generated from a comprehensive OpenAPI specification using the FastMCP library.

## Features

- **55 NextDNS operations** exposed as MCP tools (54 API + 1 custom)
- **Profile Management**: Full CRUD operations - create, read, update, and delete profiles
- **Profile Access Control**: Fine-grained read/write restrictions per profile, with read-only mode support
- **DNS-over-HTTPS Testing**: Perform DoH lookups to test DNS resolution through profiles
- **Settings Configuration**: Comprehensive settings management including logs, block page, performance, and Web3
- **Logs**: Query log retrieval, real-time streaming, download, and clearing
- **Analytics**: Comprehensive DNS query analytics and statistics (11 endpoints)
- **Content Lists**: Manage denylist, allowlist, and parental control
- **Security**: Complete security settings and TLD blocking configuration
- **Privacy**: Privacy settings, blocklists, and native tracking protection management
- **Parental Control**: Settings management with safe search and YouTube restrictions
- **OpenAPI-Generated**: Server automatically generated from [nextdns-openapi.yaml](src/nextdns_mcp/nextdns-openapi.yaml)
- **Docker MCP Gateway**: Full integration with Docker's MCP Gateway for secure, isolated deployment
- **Docker Support**: Containerized deployment with proper OCI labels
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
- `AGENTS.md`: Development guidelines and safety rules

## Available Tools

The server exposes 55 tools organized into categories:

### DNS Testing (1 custom tool)
- `dohLookup`: Perform DNS-over-HTTPS lookups through a NextDNS profile
  - Test domain resolution and blocking behavior
  - Support for all DNS record types (A, AAAA, CNAME, MX, TXT, NS, etc.)
  - Useful for debugging and testing profile configurations
  - Returns detailed DNS response with human-readable status codes

### Profile Management (5 tools)
- `listProfiles`: List all profiles
- `getProfile`: Get profile details
- `createProfile`: Create a new profile
- `updateProfile`: Update profile name and configuration
- `deleteProfile`: Delete a profile (use with extreme caution)

### Settings (10 tools)
- `getSettings`, `updateSettings`: Main profile settings
- `getLogsSettings`, `updateLogsSettings`: Log retention configuration
- `getBlockPageSettings`, `updateBlockPageSettings`: Block page customization
- `getPerformanceSettings`, `updatePerformanceSettings`: EDNS Client Subnet and caching
- `getWeb3Settings`, `updateWeb3Settings`: Blockchain DNS resolution

### Logs (4 tools)
- `getLogs`: Retrieve query logs with filtering
- `streamLogs`: Real-time log streaming (SSE)
- `downloadLogs`: Download logs in CSV/JSON format
- `clearLogs`: Clear all query logs

### Analytics (11 tools)
- `getAnalyticsStatus`: Query status breakdown
- `getAnalyticsQueries`: DNS query logs
- `getAnalyticsDNSSEC`: DNSSEC validation stats
- `getAnalyticsEncryption`: Encryption status
- `getAnalyticsIPVersions`: IPv4/IPv6 distribution
- `getAnalyticsProtocols`: DNS protocol usage (DoH, DoT, DoQ, UDP, TCP)
- `getAnalyticsDestinations`: Top queried destinations
- `getAnalyticsDevices`: Device statistics
- `getAnalyticsRootDomains`: Top root domains
- `getAnalyticsGAFAM`: GAFAM company analytics
- `getAnalyticsCountries`: Geographic statistics

### Content Lists (9 tools)
- Denylist: `getDenylist`, `addToDenylist`, `removeFromDenylist`
- Allowlist: `getAllowlist`, `addToAllowlist`, `removeFromAllowlist`
- Parental Control: `getParentalControl`, `addToParentalControl`, `removeFromParentalControl`

### Security (5 tools)
- `getSecuritySettings`, `updateSecuritySettings`: Threat protection configuration
- `getSecurityTLDs`: Get blocked TLDs
- `addSecurityTLD`: Block a top-level domain
- `removeSecurityTLD`: Unblock a TLD

### Privacy (8 tools)
- `getPrivacySettings`, `updatePrivacySettings`: Privacy protection configuration
- `getPrivacyBlocklists`, `addPrivacyBlocklist`, `removePrivacyBlocklist`: Blocklist management
- `getPrivacyNatives`, `addPrivacyNative`, `removePrivacyNative`: Native tracking protection

### Parental Control (2 tools)
- `getParentalControlSettings`: Get parental control configuration
- `updateParentalControlSettings`: Update safe search and YouTube restrictions

## Usage Examples

### DNS-over-HTTPS Testing

Test how your NextDNS profile resolves domains:

```bash
# Using Docker MCP Gateway
docker mcp tools call nextdns dohLookup \
  --domain "adwords.google.com" \
  --profile_id "abc123" \
  --record_type "A"

# Check if a domain is blocked
docker mcp tools call nextdns dohLookup \
  --domain "ads.example.com" \
  --record_type "A"

# IPv6 lookup
docker mcp tools call nextdns dohLookup \
  --domain "google.com" \
  --record_type "AAAA"

# MX records for email
docker mcp tools call nextdns dohLookup \
  --domain "gmail.com" \
  --record_type "MX"
```

**Use cases:**
- **Test blocking**: Verify if ad/tracking domains are blocked by your profile
- **Debug DNS**: Troubleshoot DNS resolution issues
- **Verify allowlist**: Confirm allowlisted domains resolve correctly
- **Compare profiles**: Test the same domain across different profiles

**Response includes:**
- DNS status code with human-readable description
- Answer records (IP addresses, CNAMEs, etc.)
- Query metadata (profile ID, domain, type)
- Full DoH endpoint URL for manual testing

## Profile Access Control

The MCP server supports fine-grained access control to restrict which profiles can be read from or written to. This is useful for multi-tenant environments, delegated administration, or implementing read-only access.

### Configuration

Access control is configured via environment variables:

```env
# Restrict read access to specific profiles (comma-separated list)
# Empty = all profiles can be read (default)
NEXTDNS_READABLE_PROFILES=abc123,def456

# Restrict write access to specific profiles (comma-separated list)
# Empty = all profiles can be written to (default)
NEXTDNS_WRITABLE_PROFILES=test789

# Enable read-only mode (disables ALL write operations)
NEXTDNS_READ_ONLY=true
```

### Access Control Rules

1. **Empty Lists = All Allowed**: When a list is empty or not set, all profiles are accessible
2. **Write Implies Read**: Profiles in the writable list are automatically readable (when both lists are set)
3. **Read-Only Mode**: Overrides all write permissions when enabled
4. **Global Operations**: Some operations like `listProfiles` are always allowed regardless of restrictions

### Usage Examples

**Read-only access (no modifications allowed):**
```env
NEXTDNS_READ_ONLY=true
```

**Restrict reading to specific profiles:**
```env
NEXTDNS_READABLE_PROFILES=home123,mobile456,work789
```

**Allow writing only to a test profile (all profiles readable):**
```env
NEXTDNS_WRITABLE_PROFILES=test123
```

**Combined restrictions:**
```env
# Users can read these three profiles
NEXTDNS_READABLE_PROFILES=home123,mobile456,test789
# But can only modify the test profile
NEXTDNS_WRITABLE_PROFILES=test789
```

### Behavior Details

When access is denied, the server returns a 403 Forbidden response with details:
```json
{
  "error": "Write access denied for profile: abc123",
  "profile_id": "abc123"
}
```

Operations that don't involve a specific profile (like listing all profiles) are always permitted, allowing users to discover available profiles even with restrictions in place.

## Safety Mechanisms

Per `AGENTS.md` guidelines:

- **Profile Deletion**: Profile deletion endpoint is exposed but includes strong warnings - use with extreme caution
- **Test Profile Only**: Write operations should use designated test profiles
- **Profile ID Verification**: All write operations verify the target profile
- **Comprehensive Logging**: Full audit trail of all operations
- **Automatic Restoration**: Tests restore original state when possible

**IMPORTANT**: The `deleteProfile` operation permanently removes all profile data. This cannot be undone. Always verify the profile ID before deletion.

## Development

### Project Structure

```
nextdns-mcp/
├── src/nextdns_mcp/
│   ├── __init__.py
│   ├── server.py              # FastMCP server implementation
│   └── nextdns-openapi.yaml   # OpenAPI specification
├── catalog.yaml               # Docker MCP Gateway catalog entry
├── pyproject.toml             # Poetry dependencies
├── Dockerfile                 # Container definition with OCI labels
├── .env                       # Configuration (not in git)
├── AGENTS.md                  # Development guidelines
└── README.md                  # This file
```

### Updating the API

To add or modify NextDNS API operations:

1. Edit `src/nextdns_mcp/nextdns-openapi.yaml` to add/update endpoints
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

## Integration

### Docker MCP Gateway (Recommended)

Docker MCP Gateway provides secure, isolated MCP server management with built-in secrets handling.

#### Quick Start

1. **Build and tag the image:**
   ```bash
   docker build -t nextdns-mcp:latest .
   ```

2. **Import the catalog:**
   ```bash
   # Import the catalog file (registers the server definition)
   docker mcp catalog import ./catalog.yaml
   ```

3. **Enable and configure the server:**
   ```bash
   # Enable the server
   docker mcp server enable nextdns
   ```
   
   When you enable the server, Docker MCP Gateway will prompt you to configure the required API key secret (`nextdns.api_key`). Enter your NextDNS API key from https://my.nextdns.io/account.

4. **Set the API key (if not prompted during enable):**
   
   If you need to set or update the API key after enabling, use the secret management command:
   
   ```bash
   # Option 1: Direct assignment (simple)
   docker mcp secret set nextdns.api_key=your_api_key_here
   
   # Option 2: Via STDIN (more secure, avoids shell history)
   # PowerShell:
   Write-Output "your_api_key_here" | docker mcp secret set nextdns.api_key
   
   # Bash/Zsh:
   echo "your_api_key_here" | docker mcp secret set nextdns.api_key
   
   # Verify the secret is set
   docker mcp secret ls
   ```
   
   **Note**: The secret name must match what's defined in `catalog.yaml`: `nextdns.api_key`. The server automatically strips any trailing whitespace or newlines from the secret value.

5. **Verify it's working:**
   ```bash
   # List available tools (should show 76 tools)
   docker mcp tools ls

   # Call a tool to list your profiles
   docker mcp tools call nextdns listProfiles
   ```

#### Using Self-Contained Image (No Catalog)

You can also run the server directly without adding it to a catalog:

```bash
docker run -i --rm \
  -e NEXTDNS_API_KEY=your_api_key_here \
  nextdns-mcp:latest
```

Then enable it in Claude Desktop or other MCP clients using the Docker transport.

### Claude Desktop

Add to your Claude Desktop configuration:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

**Option 1: Using Poetry (Development)**
```json
{
  "mcpServers": {
    "nextdns": {
      "command": "poetry",
      "args": ["run", "python", "-m", "nextdns_mcp.server"],
      "cwd": "/path/to/nextdns-mcp",
      "env": {
        "NEXTDNS_API_KEY": "your_api_key_here",
        "NEXTDNS_DEFAULT_PROFILE": "your_profile_id"
      }
    }
  }
}
```

**Option 2: Using Docker**
```json
{
  "mcpServers": {
    "nextdns": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "NEXTDNS_API_KEY=your_api_key_here",
        "-e", "NEXTDNS_DEFAULT_PROFILE=your_profile_id",
        "nextdns-mcp:latest"
      ]
    }
  }
}
```

**Option 3: Using Python directly (after `poetry install`)**
```json
{
  "mcpServers": {
    "nextdns": {
      "command": "python",
      "args": ["-m", "nextdns_mcp.server"],
      "cwd": "/path/to/nextdns-mcp",
      "env": {
        "NEXTDNS_API_KEY": "your_api_key_here",
        "PYTHONPATH": "/path/to/nextdns-mcp/src"
      }
    }
  }
}
```

### Other MCP Clients

This server works with any MCP client that supports stdio transport. Configure your client to:
1. Run command: `python -m nextdns_mcp.server` (or via Docker)
2. Set environment variable: `NEXTDNS_API_KEY=your_api_key_here`
3. Use stdio transport (standard input/output)

## Docker MCP Gateway Commands

Once you've enabled the server, here are useful management commands:

```bash
# Server management
docker mcp server ls                          # List all enabled servers
docker mcp server enable nextdns              # Enable the server
docker mcp server disable nextdns             # Disable the server

# Secret management
docker mcp secret ls                          # List all secrets
docker mcp secret set nextdns.api_key=value   # Set API key secret
docker mcp secret rm nextdns.api_key          # Remove a secret

# Configuration
docker mcp config read                        # Read current configuration
docker mcp config write                       # Write/edit configuration interactively

# Tools
docker mcp tools ls                           # List all available tools
docker mcp tools call createProfile '{"name":"Test"}'  # Call a tool

# Catalog management
docker mcp catalog ls                         # List all catalogs
docker mcp catalog show nextdns-mcp-catalog   # Show catalog contents
docker mcp catalog import ./catalog.yaml      # Import/update catalog
```

## License

This project is released under the [MIT License](LICENSE).

## Contributing

1. Follow guidelines in `AGENTS.md`
2. Maintain OpenAPI spec accuracy
3. Test all changes with designated test profiles
4. Never commit `.env` or API keys
