# Configuration Reference

All configuration is done via environment variables.

## Extra Field Relaxation

Unknown/extra fields in tool arguments are ignored by the `StripExtraFieldsMiddleware` (see troubleshooting.md), which filters each tool call to the fields defined in the tool's parameter schema before FastMCP validates the arguments.

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
| MCP_TRANSPORT | string | stdio | No | `stdio` (default) or `http` (streamable-HTTP). See "HTTP Transport" in the README |
| MCP_HOST | string | 127.0.0.1 | No | Bind interface for HTTP transport. Loopback-only by default; a non-loopback value is an explicit opt-in that requires reverse-proxy/auth protection |
| MCP_PORT | number | 8000 | No | Port for HTTP transport |

Notes
- `NEXTDNS_API_KEY` (or `NEXTDNS_API_KEY_FILE`) is required. An absent or empty key fails fast with `ConfigurationError` during client construction instead of creating an unauthenticated client.
- `NEXTDNS_HTTP_TIMEOUT` must be a positive number of seconds. Invalid values (e.g. `abc`, `0`, empty) fail fast at startup with a clear `ConfigurationError` instead of a confusing crash deep in client construction.
- Per-profile checks match the `profile_id` in the URL. Collection profile endpoints (`GET /profiles` for `manageProfiles(operation="list")`, `POST /profiles` for `create`) carry no `profile_id` but still respect the global denials above: collection reads are denied when both profile sets are unset, and collection writes are denied in read-only mode or when `NEXTDNS_WRITABLE_PROFILES` is unset.
- Only `dohLookup` bypasses per-profile checks entirely (it uses a separate DoH endpoint).
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

