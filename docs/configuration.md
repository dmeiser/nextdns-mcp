# Configuration

This page describes all configuration for the NextDNS MCP Server, how to use environment variables, `.env` files, and Docker secrets.

The server validates configuration on import. If a required value is missing (API key), it will exit with a clear error.

## Environment Variables

Required:
- `NEXTDNS_API_KEY`: NextDNS API key. Get from https://my.nextdns.io/account
  - Alternative: `NEXTDNS_API_KEY_FILE` path to a file containing the key (e.g., Docker secret)

Optional:
- `NEXTDNS_DEFAULT_PROFILE`: Default profile ID used by tools that accept an optional `profile_id` (e.g., `dohLookup`).
- `NEXTDNS_HTTP_TIMEOUT`: HTTP timeout in seconds (default: `30`).

Access Control:
- `NEXTDNS_READABLE_PROFILES`: Comma-separated list of profile IDs that can be read. Empty or unset means all profiles are readable.
- `NEXTDNS_WRITABLE_PROFILES`: Comma-separated list of profile IDs that can be written to. Empty or unset means all profiles are writable (unless read-only mode is enabled). Write implies read.
- `NEXTDNS_READ_ONLY`: Enable read-only mode (`true|1|yes` to enable). When enabled, all write operations are denied.

Notes:
- Global operations that do not reference a specific profile are always allowed (e.g., `listProfiles`, `dohLookup`).
- Access control is enforced by the `AccessControlledClient` in `server.py`.

## .env Files

The server loads environment variables from a `.env` file if present using `python-dotenv`.

Example `.env`:
```env
NEXTDNS_API_KEY=your_api_key_here
NEXTDNS_DEFAULT_PROFILE=your_profile_id
NEXTDNS_HTTP_TIMEOUT=30

# Access control examples
# NEXTDNS_READABLE_PROFILES=home123,work456
# NEXTDNS_WRITABLE_PROFILES=test789
# NEXTDNS_READ_ONLY=false
```

## Docker Secrets

For production use, store the API key as a Docker secret and reference it with `NEXTDNS_API_KEY_FILE`.

Example (non-swarm):
```bash
# Create a secret file
echo "your_api_key_here" > /tmp/nextdns_api_key
chmod 600 /tmp/nextdns_api_key

# Run with mounted secret
docker run -i --rm \
  -v /tmp/nextdns_api_key:/run/secrets/nextdns_api_key:ro \
  -e NEXTDNS_API_KEY_FILE=/run/secrets/nextdns_api_key \
  nextdns-mcp:latest
```

## Excluded Routes and Custom Tools

FastMCP cannot accept raw JSON arrays in request bodies, so several array-body PUT endpoints are excluded from auto-generation and implemented as custom tools that accept JSON array strings:

- `updateDenylist`, `updateAllowlist`
- `updateParentalControlServices`, `updateParentalControlCategories`
- `updateSecurityTlds`
- `updatePrivacyBlocklists`, `updatePrivacyNatives`

Unsupported endpoints (excluded due to API limitations):
- `GET /profiles/{profile_id}/analytics/domains;series` (NextDNS API returns 404)
- `GET /profiles/{profile_id}/logs/stream` (SSE streaming not supported)

These exclusions are defined in `EXCLUDED_ROUTES` in `src/nextdns_mcp/config.py` and handled in `server.py` with custom tool implementations.

## Constants

- `NEXTDNS_BASE_URL`: `https://api.nextdns.io`
- `VALID_DNS_RECORD_TYPES`: Supported DNS record types for `dohLookup`
- `DNS_STATUS_CODES`: Mapping of DNS response status codes to descriptions
