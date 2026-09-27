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
| NEXTDNS_DOWNLOAD_MAX_BYTES | number (bytes) | 1073741824 (1 GiB) | No | Hard cap on the total bytes a single `manageLogs(operation="download")` may stream to disk; the download is aborted mid-stream when it would be exceeded |
| NEXTDNS_READ_ONLY | bool (true/false/1/0/yes/no) | false | No | Disables all write operations when true |
| NEXTDNS_READABLE_PROFILES | string | (unset) | No | Comma-separated profile IDs allowed for reads; special value "ALL" allows reads of all profiles; empty/unset denies all reads |
| NEXTDNS_WRITABLE_PROFILES | string | (unset) | No | Comma-separated profile IDs allowed for writes; special value "ALL" allows writes to all profiles; empty/unset denies all writes; ignored if NEXTDNS_READ_ONLY=true |
| MCP_TRANSPORT | string | stdio | No | `stdio` (default) or `http` (streamable-HTTP). Case-insensitive and trimmed. Any other value fails startup with `ConfigurationError`. See "HTTP Transport" in the README |
| MCP_HOST | string | 127.0.0.1 | No | Bind interface for HTTP transport. Loopback-only by default; a non-loopback value is an explicit opt-in that requires reverse-proxy/auth protection |
| MCP_PORT | number | 8000 | No | Port for HTTP transport |
| FASTMCP_CHECK_FOR_UPDATES | string | off | No | FastMCP's automatic update check, disabled by default because it slows startup and can hang offline/CI. Set an explicit FastMCP value (e.g. `stable`) to opt back in |

Notes
- `FASTMCP_CHECK_FOR_UPDATES` is applied by the server's `configure()` step (run by the `python -m nextdns_mcp.server` entrypoint) and only defaults to `off`; an explicit value you set is preserved.
- `NEXTDNS_API_KEY` (or `NEXTDNS_API_KEY_FILE`) is required. An absent or empty key fails fast instead of creating an unauthenticated client: at startup as `MissingApiKeyError`, and again on first client construction as `ConfigurationError`. `MissingApiKeyError` is a subclass of `ConfigurationError`, so a single `except ConfigurationError` covers both.
- `NEXTDNS_HTTP_TIMEOUT` must be a positive number of seconds. Invalid values (e.g. `abc`, `0`, empty) fail fast at startup with a clear `ConfigurationError` instead of a confusing crash deep in client construction.
- `MCP_TRANSPORT` must be `stdio` or `http` (case-insensitive, trimmed). An unrecognized value (e.g. `https`, `sse`, a typo, or a stray trailing space) fails fast at startup with a clear `ConfigurationError` instead of silently downgrading to a stdio server that speaks no HTTP.
- `NEXTDNS_DOWNLOAD_MAX_BYTES` must be a positive integer of bytes. Invalid values (e.g. `abc`, `0`, `-5`) raise `ConfigurationError`, like the timeout above. See "Local log downloads" in safety.md.
- Profile IDs are hexadecimal and matched case-insensitively: values in `NEXTDNS_READABLE_PROFILES`/`NEXTDNS_WRITABLE_PROFILES` and the `profile_id` being checked are both normalized to lowercase before comparison, so `2F4A9B` in the config matches a query for `2f4a9b`.
- Per-profile checks match the `profile_id` in the URL. Collection profile endpoints (`GET /profiles` for `manageProfiles(operation="list")`, `POST /profiles` for `create`) carry no `profile_id` but still respect the global denials above: collection reads are denied when both profile sets are unset, and collection writes are denied in read-only mode or when `NEXTDNS_WRITABLE_PROFILES` is unset.
- `dohLookup` uses a separate DoH endpoint, but it still enforces the per-profile read check: the queried profile must be in the effective readable set, i.e. `NEXTDNS_READABLE_PROFILES` or `NEXTDNS_WRITABLE_PROFILES` ("write implies read", below), or either set must be "ALL".
- "Write implies read": profiles allowed for writes are automatically considered readable.
- On the `AccessControlledClient` request and stream paths, for the `dohLookup` send, and for the collection-denial checks in the `manageProfiles` `list`/`create` operations, access control settings are read once per operation into an immutable snapshot (`nextdns_mcp.config.load_profile_access_control()`) that authorizes all of that operation's checks together, so a change to the environment applies from the next request onward, never midway through one in flight. A single MCP call therefore reads each profile ACL variable once rather than once per check. No cache invalidation or restart is needed: there is no cross-request cache to expire.

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

