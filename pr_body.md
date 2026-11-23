## What this solves

The server kept five pieces of mutable state in process-global module singletons: the authenticated API client, the lazily built MCP server, the DoH client, the `/health` readiness cache, and the standalone download registry. Each was deliberate and documented, but each was also a hidden side channel: any code, in any context, could read or reset state that conceptually belonged to one server — and two embedded servers in one process shared all of it, so one server's shutdown closed the other's connections and deleted its tracked CSVs. Every one of the five now belongs to an explicit owner with an explicit lifetime.

Closes #642.

## What was wrong

At the review baseline (`6df1cd5`):

- `src/nextdns_mcp/client.py:446` — `_client`, populated by `get_api_client()` and reset by `close_api_client()`; every module-level accessor shared one client process-wide.
- `src/nextdns_mcp/server.py:79` — `_mcp_server`, populated by the lazy `get_mcp_server()`; `configure()`, the `mcp_server`/`mcp` compatibility attributes, and every caller shared one server.
- `src/nextdns_mcp/tools/doh.py:64` — `_doh_client`, populated by `_get_doh_client()` and reset by `close_doh_client()`; same process-wide sharing.
- `src/nextdns_mcp/openapi.py:105` — `_health_probe_cache` (with the module-global `_health_probe_lock`); two embedded servers shared cached readiness and serialized each other's probes.
- `src/nextdns_mcp/tools/logs.py:75,89-90` — `_SUCCESSFUL_DOWNLOADS`, the process-global fallback registry a download was registered with when no lifespan registry was set. The per-lifespan context var only covers calls made inside the lifespan's own task — FastMCP serves requests from other tasks, which cannot see a lifespan-set context var — so in served traffic every download fell into this process-global set, and whatever lifespan shut down next reclaimed it.

## What changed

Each state holder is an explicit owner class, per server instance, with no process-global mutable storage and no fallback:

- `client.ApiClientOwner`, `tools.doh.DohClientOwner` (which may also adopt an existing client) — lazily create their client on first use, reuse it for keep-alive, and reset on `aclose()`; `get_api_client()` / `close_api_client()` / `close_doh_client()` keep their signatures and delegate to the resolved owner.
- `server.McpServerOwner` — owns the lazily built server; `configure()` publishes one into the calling context, so repeated `configure()` calls and the `mcp_server`/`mcp` attributes keep returning the same instance, while `build_mcp_server()` remains the explicit path for embedded servers.
- `openapi.ServerResources` — one per `FastMCP` instance, created in `create_mcp_server()` and attached to the instance; owns the API client owner, DoH client owner, and a `tools.logs.DownloadRegistry`, and releases all three on shutdown. `_server_lifespan` yields it into the lifespan context and also publishes it into each module's owner context var for same-task callers.
- `openapi.ReadinessProber` — one per server, created when its `/health` route is registered; owns the TTL cache and the single-flight lock, and probes through the API client owner its server owns.
- `tools.logs.DownloadRegistry` — owns insertion-ordered registration, `MAX_RETAINED_DOWNLOADS` eviction, and idempotent cleanup; `downloads_owned()` lets a standalone session own one explicitly.

Resolution is explicit and fails loudly (`errors.StateOwnershipError`): an owner published with the module's `*_owned()` context manager wins (standalone callers, tests); otherwise the owner comes from the FastMCP request's lifespan context (`state.request_resources()`), which is the channel FastMCP itself binds per request task; otherwise the access raises. There is deliberately no default. A standalone `downloadLogs` call with no owner still succeeds and returns its payload — the CSV is reported untracked and left for the caller rather than bound to a hidden process-global set.

`transport.py` and `tools/logs.py` now send through `client.get_api_client()` instead of the `client.api_client` module attribute, so they resolve the current owner at call time.

## How it was verified

- `uv run pytest tests/unit` — 1900+ tests pass, including new coverage in `tests/unit/test_state_owners.py` (no-fallback guarantees, two-owner independence per state holder, registry retention/eviction, per-request resolution) and rewritten lifecycle tests: sibling-server shutdown no longer closes the other server's clients (`test_mcp_server.py`), two probers keep separate caches (`test_health_endpoint.py`), and two `McpServerOwner`s build independent servers (`test_lazy_factories.py`).
- `uv run mypy src`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run xenon --max-absolute A --max-modules A --max-average A src/ scripts/run_container_e2e.py` — all clean.
- The public MCP surface is unchanged: every tool, the `/health` and `/v1/models` routes, `configure()` semantics, and the lazy-creation/caching/single-flight behaviors are pinned by the existing suite, updated only where it patched the removed module globals.

## Risk

Behavior change is confined to out-of-lifespan callers: `get_api_client()` and friends now raise `StateOwnershipError` when no owner is active instead of silently sharing a process-global instance, and a standalone download with no active registry is left untracked (with a warning) instead of accumulating in a process-global set. Both are the intended contract of this change; served traffic is unaffected. Roll back by reverting this PR.
