# Configuration Reference

All configuration is done via environment variables (or Docker MCP Gateway secrets/config).

## Extra Field Relaxation

All tool input models (custom and OpenAPI-imported) are configured to ignore extra/unknown fields. This is achieved by:
- Setting `strict_input_validation=False` in FastMCP
- Patching OpenAPI-imported models with a custom `mcp_component_fn` (see troubleshooting.md)

This allows AI/CLI clients to send extra fields without causing errors, while still enforcing required/typed fields.

## Environment variables

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| NEXTDNS_API_KEY | string | - | Yes | API key used for authenticated NextDNS API calls |
| NEXTDNS_API_KEY_FILE | string (path) | - | No | Path to a file containing only the API key (e.g., Docker secret) |
| NEXTDNS_DEFAULT_PROFILE | string | - | No | Default profile ID to use when a tool parameter omits profile_id |
| NEXTDNS_HTTP_TIMEOUT | number (seconds) | 30 | No | HTTP timeout for API and DoH requests |
| NEXTDNS_READ_ONLY | bool (true/false/1/0/yes/no) | false | No | Disables all write operations when true |
| NEXTDNS_READABLE_PROFILES | string | (unset) | No | Comma-separated profile IDs allowed for reads; special value "ALL" allows reads of all profiles; empty/unset denies all reads |
| NEXTDNS_WRITABLE_PROFILES | string | (unset) | No | Comma-separated profile IDs allowed for writes; special value "ALL" allows writes to all profiles; empty/unset denies all writes; ignored if NEXTDNS_READ_ONLY=true |

Notes
- Global tools bypass per-profile checks: listProfiles and dohLookup.
- "Write implies read": profiles allowed for writes are automatically considered readable.

## Examples

Bash/Zsh
```bash
export NEXTDNS_API_KEY=sk_live_...
export NEXTDNS_DEFAULT_PROFILE=abc123
export NEXTDNS_HTTP_TIMEOUT=45
export NEXTDNS_READABLE_PROFILES=ALL
export NEXTDNS_WRITABLE_PROFILES=test789
# Read-only mode (overrides writes)
export NEXTDNS_READ_ONLY=true
```

PowerShell
```powershell
$env:NEXTDNS_API_KEY = "sk_live_..."
$env:NEXTDNS_DEFAULT_PROFILE = "abc123"
$env:NEXTDNS_HTTP_TIMEOUT = "45"
$env:NEXTDNS_READABLE_PROFILES = "ALL"
$env:NEXTDNS_WRITABLE_PROFILES = "test789"
$env:NEXTDNS_READ_ONLY = "true"
```

