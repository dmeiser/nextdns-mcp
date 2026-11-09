# Configuration Reference

All configuration is done via environment variables.

## Extra Field Relaxation

Unknown/extra fields in tool arguments are dropped by the `StripExtraFieldsMiddleware` (see troubleshooting.md), which filters each tool call to the fields defined in the tool's parameter schema before the arguments are validated against it.

This allows AI/CLI clients to send extra fields without causing errors, while still enforcing required/typed fields. The drop is not silent: the answer carries `ignored_arguments` naming every field that was not applied, so a caller never has to guess whether a field it sent took effect.

## Environment variables

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| NEXTDNS_API_KEY | string | - | Yes | API key used for authenticated NextDNS API calls |
| NEXTDNS_API_KEY_FILE | string (path) | - | No | Path to a file containing only the API key (e.g., Docker secret) |
| NEXTDNS_DEFAULT_PROFILE | string | - | No | Default profile ID used only by `overviewDashboard`, `dohLookup`, and `plotAnalytics` when they omit `profile_id`; every generated operation tool requires an explicit `profile_id` |
| NEXTDNS_HTTP_TIMEOUT | number (seconds) | 30 | No | HTTP timeout for API and DoH requests. Captured at client construction; changing this setting requires a process restart |
| NEXTDNS_DOWNLOAD_MAX_BYTES | number (bytes) | 1073741824 (1 GiB) | No | Hard cap on the total bytes a single `downloadLogs` may stream to disk; the download is aborted mid-stream when it would be exceeded |
| NEXTDNS_READ_ONLY | bool (true/false/1/0/yes/no) | false | No | Disables all write operations when true |
| NEXTDNS_READABLE_PROFILES | string | (unset) | No | Comma-separated profile IDs allowed for reads; special value "ALL" allows reads of all profiles; empty/unset denies all reads |
| NEXTDNS_WRITABLE_PROFILES | string | (unset) | No | Comma-separated profile IDs allowed for writes; special value "ALL" allows writes to all profiles; empty/unset denies all writes; ignored if NEXTDNS_READ_ONLY=true |
| NEXTDNS_PROTECTED_PROFILES | string | (unset) | No | Comma-separated profile IDs that are NEVER writable, overriding even `NEXTDNS_WRITABLE_PROFILES=ALL`; reads are unaffected — the denylist is writes-only by design, and a profile created by `createProfile` is not covered by it. Intended for data-bearing/production profiles a test agent must not modify or delete. See "Protected profiles" in safety.md |
| NEXTDNS_TOOL_SEARCH | string (off/bm25) | bm25 | No | Progressive tool disclosure via FastMCP's BM25 search transform. `bm25` (the default) replaces `tools/list` with the synthetic `search_tools` + `call_tool` pair: the listing shows only those two, and the full catalog is read by searching it (the real tools stay callable either way, they are only hidden from the listing); `off` advertises every published tool in the listing. An invalid value fails startup with `ConfigurationError` |
| NEXTDNS_GENERATIVE_UI | bool (true/false/1/0/on/off/yes/no) | off | No | Opt-in beta feature: mounts FastMCP's Generative UI provider (`generate_prefab_ui` and `search_prefab_components`) for dynamic Prefab UI authoring in Pyodide. Disabled by default |
| NEXTDNS_CALL_LOG | string (path) | (unset) | No | Appends one JSON line per executed tool call to the named file: the tool name, the endpoint it executed, the `profile_id`, the arguments (bounded to ~600 characters) and up to ~2000 characters of what the call answered. The file is created mode `0600`. A harness/debug facility scoped to one run — nothing rotates or truncates it, so the operator bounds it. See "Call log" in safety.md |
| MCP_TRANSPORT | string | stdio | No | `stdio` (default) or `http` (streamable-HTTP). Case-insensitive and trimmed. Any other value fails startup with `ConfigurationError`. See "HTTP Transport" in the README |
| MCP_HOST | string | 127.0.0.1 | No | Bind interface for HTTP transport. Loopback-only by default; a non-loopback value is an explicit opt-in that requires reverse-proxy/auth protection |
| MCP_PORT | number | 8000 | No | Port for HTTP transport. Must be an integer in 1-65535; an invalid value fails fast at startup with `ConfigurationError`, like `NEXTDNS_HTTP_TIMEOUT` |
| FASTMCP_CHECK_FOR_UPDATES | string | off | No | FastMCP's automatic update check, disabled by default because it slows startup and can hang offline/CI. Set an explicit FastMCP value (e.g. `stable`) to opt back in |
| FASTMCP_HTTP_HOST_ORIGIN_PROTECTION | string (true/false/auto) | false (FastMCP's own default; `auto` in the container images) | No | FastMCP's HTTP host/origin header validation, which mitigates DNS-rebinding attacks against the unauthenticated HTTP transport. `auto` enforces the check when the bind address is loopback, and additionally Origin-checks any request presenting a loopback `Host` (see the note below). Set it to `false` behind a reverse proxy that already enforces origin checks |

Notes
- `FASTMCP_CHECK_FOR_UPDATES` is applied by the server's `configure()` step (run by the `python -m nextdns_mcp.server` entrypoint) and only defaults to `off`; an explicit value you set is preserved.
- `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION` is read directly by FastMCP, whose own default is `false`, so an application run has no protection unless you set it. Both container images set it to `auto`. It is deliberately **not** defaulted by the server's `configure()` step: on a loopback bind, turning it on also makes Host allowlisting unconditional, so an implicit default would answer `421 Misdirected Request` to every request that arrives under any name other than `127.0.0.1`, `localhost`, `::1` or the bind address — including the reverse proxy in front of a loopback bind that the README recommends. Enabling it requires configuring the names alongside it:
  - **Behind a TLS-terminating reverse proxy** (e.g. serving `https://mcp.example.com` and forwarding to `127.0.0.1:8000`) needs all three of the following, in order:
    1. **Make the server trust the proxy's forwarded headers.** The server takes the request scheme and client address from `X-Forwarded-Proto` / `X-Forwarded-For` only for proxies it trusts, so trust the proxy's address with the `FORWARDED_ALLOW_IPS` environment variable (it defaults to `127.0.0.1`, which already covers a same-host TLS-terminating proxy; set it explicitly when the proxy runs elsewhere). Without it the server sees a cleartext `http` socket while the browser sends `Origin: https://mcp.example.com`, treats the request as cross-origin and answers `403` to the proxy's own traffic. Never trust every source: a client could then forge `X-Forwarded-Proto` and talk its way past the guard.
    2. **List the public host**: `FASTMCP_HTTP_ALLOWED_HOSTS='["mcp.example.com"]'`. Its value is a JSON array of hostnames (`["a.example", "b.example"]`), not a comma-separated list: FastMCP parses it as JSON, and a bare value such as `mcp.example.com` fails validation at startup. Hostnames are matched without the port, so `mcp.example.com` also allows `Host: mcp.example.com:8000`.
    3. **Then enable the protection**: `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=auto`. Enabling it before step 2 answers `421 Misdirected Request` to the proxy's forwarded `Host` and `/mcp` stops serving.

    With that in place this is a working combination — no server flag or code change is needed — so the alternative is simply to leave the protection off and knowingly accept the DNS-rebinding exposure described below.
  - **Plain local use, no proxy** (clients connecting to `http://127.0.0.1:8000/mcp`): `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=auto` alone is enough; the loopback names are already allowed.
  - **Leaving it off** keeps FastMCP's `false` default: the loopback endpoint then serves any `Host` and `Origin` a browser can be tricked into sending, which is the DNS-rebinding exposure. The remaining mitigation is the network or reverse-proxy layer (an authenticating proxy, an allowlist, or not exposing the port at all).
- `auto` on a non-loopback bind (e.g. the `MCP_HOST=0.0.0.0` the container images are usually published on) leaves FastMCP's guard largely inert: no `Host` checking happens on that bind, and `Origin` checking happens only for a request that itself presents a loopback `Host`. So the shipped `auto` in those images does not protect the published port; protect it at the network or reverse-proxy layer.
- `NEXTDNS_API_KEY` (or `NEXTDNS_API_KEY_FILE`) is required. An absent or empty key fails fast instead of creating an unauthenticated client: at startup as `MissingApiKeyError`, and again on first client construction as `ConfigurationError`. `MissingApiKeyError` is a subclass of `ConfigurationError`, so a single `except ConfigurationError` covers both.
- `NEXTDNS_HTTP_TIMEOUT` must be a positive number of seconds. Invalid values (e.g. `abc`, `0`, empty) fail fast at startup with a clear `ConfigurationError` instead of a confusing crash deep in client construction. The timeout is captured once when the HTTP clients are constructed; changing this variable requires a full process restart.
- `MCP_TRANSPORT` must be `stdio` or `http` (case-insensitive, trimmed). An unrecognized value (e.g. `https`, `sse`, a typo, or a stray trailing space) fails fast at startup with a clear `ConfigurationError` instead of silently downgrading to a stdio server that speaks no HTTP.
- `MCP_PORT` must be an integer in 1-65535. A non-numeric or out-of-range value (e.g. `abc`, an empty value, `0`, `65536`) fails fast at startup with a clear `ConfigurationError` naming `MCP_PORT`, like the timeout above.
- `NEXTDNS_DOWNLOAD_MAX_BYTES` must be a positive integer of bytes. Invalid values (e.g. `abc`, `0`, `-5`) raise `ConfigurationError`, like the timeout above. See "Local log downloads" in safety.md.
- `NEXTDNS_CALL_LOG` writes what the calls did, not just that they happened: each row carries the arguments and the start of the answer, so enabling it persists whatever the run touched — queried domains, device names, query-log rows, contact details, settings payloads. The file is created mode `0600`, and it is a harness/debug facility scoped to a run: there is no rotation and no size cap, so an operator who sets it on a long-running server must bound and truncate the file themselves — retention is operator-managed. See "Call log" in safety.md.
- Profile IDs are lowercase alphanumeric, 6 or 10 characters (the shape `listProfiles` returns). Tool arguments must use that lowercase form. Entries in `NEXTDNS_READABLE_PROFILES`/`NEXTDNS_WRITABLE_PROFILES`/`NEXTDNS_PROTECTED_PROFILES` are normalized to lowercase before comparison, so `2F4A9B` in one of those environment lists matches a tool call for `2f4a9b`.
- `NEXTDNS_PROTECTED_PROFILES` is a **write-only denylist, by design**: reads of a protected profile (settings, logs, analytics) stay allowed so it remains usable as a data source beside a test agent, while every write to it — including `deleteProfile` — is refused even under `NEXTDNS_WRITABLE_PROFILES=ALL`. It is the mitigation for running with `ALL` on a key that also reaches production profiles; see safety.md. It has no `ALL` sentinel (an entry is one profile id), and a profile the server creates with `createProfile` is not protected by it: a new id is unprotected by definition, so writes to it are governed only by `NEXTDNS_WRITABLE_PROFILES`.
- Per-profile checks match the `profile_id` in the URL. Collection profile endpoints (`GET /profiles` for `listProfiles`, `POST /profiles` for `createProfile`) carry no `profile_id` but still respect the global denials above: collection reads are denied when both profile sets are unset, and collection writes are denied in read-only mode or when `NEXTDNS_WRITABLE_PROFILES` is unset.
- `dohLookup` uses a separate DoH endpoint, but it still enforces the per-profile read check: the queried profile must be in the effective readable set, i.e. `NEXTDNS_READABLE_PROFILES` or `NEXTDNS_WRITABLE_PROFILES` ("write implies read", below), or either set must be "ALL".
- "Write implies read": profiles allowed for writes are automatically considered readable.
- On the `AccessControlledClient` request and stream paths, for the `dohLookup` send, and for the collection-denial checks in `listProfiles`/`createProfile`, access control settings are read once per operation into an immutable snapshot (`nextdns_mcp.config.load_profile_access_control()`) that authorizes all of that operation's checks together, so a change to the environment applies from the next request onward, never midway through one in flight. Each of `NEXTDNS_READ_ONLY`, `NEXTDNS_READABLE_PROFILES`, `NEXTDNS_WRITABLE_PROFILES` and `NEXTDNS_PROTECTED_PROFILES` is read once per operation, so all of that operation's checks agree with each other. A single MCP call therefore reads each profile ACL variable once rather than once per check. No cache invalidation or restart is needed: there is no cross-request cache to expire.

## Tool catalog

The server publishes one tool per operation of the bundled OpenAPI document (`src/nextdns_mcp/nextdns-openapi.yaml`): 78 of the document's 79 operations, each named for its own `operationId` (`getAllowlist`, `addToDenylist`, `updateBlockPageSettings`) and taking that endpoint's own arguments — `profile_id` plus the path, query and body fields the endpoint documents, with an endpoint whose body is a JSON array taking the array as one `body` argument the document describes. There is no `operation` argument: the name says what the call does. Because each tool is bound to exactly one endpoint, the call log records the method and path template a call hit and whether it was a write, taken from the HTTP method rather than from an operation name. `streamLogs` — the live query-log Server-Sent Events stream — is the one spec operation that is not published: an SSE stream cannot answer a request/response tool call.

Three tools do something no endpoint describes and are registered by hand beside the generated ones: `overviewDashboard` renders interactive Prefab UI dashboards and specialized widgets, `dohLookup` resolves a name over DNS-over-HTTPS, and `plotAnalytics` renders an interactive area chart from analytics data.

Everything the server says about its own catalog comes from the document it generated the catalog from:

- The `initialize` instructions describe that catalog: each tool takes its endpoint's own arguments, and there is no operation to choose.
- Error hints name a call the caller can actually make: the hint that points at profile discovery reads `listProfiles()`.
- Each tool's description is the document's own description of the operation, so what a call accepts is stated where the caller finds the tool.

`NEXTDNS_TOOL_SEARCH` composes with the catalog: with progressive disclosure on — the default, `bm25` — `tools/list` exposes only `search_tools` + `call_tool` and the search index covers the full operation set, so the catalog is read by searching it; `NEXTDNS_TOOL_SEARCH=off` restores the full listing. The instructions list the tool names only when the catalog is small enough for that to be cheaper than searching; the 81-tool catalog points at `search_tools` and leaves the names to `tools/list`.

## Examples

Bash/Zsh
```bash
export NEXTDNS_API_KEY=sk_live_...
export NEXTDNS_DEFAULT_PROFILE=abc123
export NEXTDNS_HTTP_TIMEOUT=45
export NEXTDNS_READABLE_PROFILES=ALL
export NEXTDNS_WRITABLE_PROFILES=test789
# Never writable, even with NEXTDNS_WRITABLE_PROFILES=ALL (reads still allowed):
# export NEXTDNS_PROTECTED_PROFILES=prod1aa1
# Record every executed tool call for one run (created 0600; no rotation):
# export NEXTDNS_CALL_LOG=./call-log.jsonl
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
# Never writable, even with NEXTDNS_WRITABLE_PROFILES=ALL (reads still allowed):
# $env:NEXTDNS_PROTECTED_PROFILES = "prod1aa1"
# Record every executed tool call for one run (created 0600; no rotation):
# $env:NEXTDNS_CALL_LOG = "./call-log.jsonl"
# Read-only mode (overrides writes)
$env:NEXTDNS_READ_ONLY = "true"
```

