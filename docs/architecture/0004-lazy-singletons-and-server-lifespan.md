# ADR 0004 — Lazy per-server resources and one shutdown point per server

Status: accepted (3.x; the pattern predates the 4.0.0 tool-surface rewrite and now
holds explicit per-server owners). Applies to `src/nextdns_mcp/server.py`,
`src/nextdns_mcp/client.py`, `src/nextdns_mcp/tools/doh.py`,
`src/nextdns_mcp/lifespan.py`, and the server wiring in `src/nextdns_mcp/openapi.py`.

## Context

Three objects in a server runtime are expensive or awkward to create more than once:
the FastMCP server (one parse of the shipped document and 81 tool registrations), the
authenticated API client (a connection pool to api.nextdns.io), and the DoH client (the
same, to dns.nextdns.io).

They were originally module-level objects built at import time (issue #150), and that
shape cost four things, each found the hard way:

- Importing the package built a server, built a client, and ran logging configuration —
  any importer, test or library consumer alike, paid for all three and had its
  environment mutated (issue #150, then #190 for the env mutation).
- Importing the server loaded whatever `.env` file the working directory happened to sit
  near (issue #177), because `load_dotenv()` walked upward from the cwd at module import.
- Configuration set after import was invisible: the client had already read the key and
  the timeout, so a test that set `NEXTDNS_HTTP_TIMEOUT` was testing a client built from
  the old value.
- Nothing owned teardown. The clients were never closed, and their by-products — the
  downloaded CSVs — were tracked in one process-global set that every embedded server's
  shutdown emptied, deleting a live sibling's files mid-read (issue #277).

The other side of the ledger is keep-alive: dozens of tools that each open a fresh TLS
connection are slower and noisier than one pooled client. The DoH client states its
reason for existing at all: persistence for connection reuse, now held by one
`DohClientOwner` per server and mirroring `ApiClientOwner`
(`src/nextdns_mcp/tools/doh.py:60-73`).

## Decision

**Three shared instances per server runtime, each created on first use and held by an
explicit owner; construction is triggered explicitly, never by import; shutdown has
exactly one owner — that server's lifespan.**

- **The server** is held by a `McpServerOwner` whose `_server` starts `None` and is built
  by `get_mcp_server()` on first use; `configure()` publishes the entrypoint's owner into
  its context and defaults `FASTMCP_CHECK_FOR_UPDATES` before returning that server
  (`src/nextdns_mcp/server.py:109-150`, `:212-252`). Embedded servers call
  `build_mcp_server()` and hold their own instance. Importing the module builds no server
  and mutates no environment (issues #177 and #190), and that is pinned by
  `tests/unit/test_server_dotenv.py` and `tests/unit/test_lazy_factories.py`.
- **The clients** are held by `ApiClientOwner` and `DohClientOwner`, one of each attached
  to every `FastMCP` instance as `ServerResources` by `create_mcp_server()`
  (`src/nextdns_mcp/openapi.py:91-92`, `src/nextdns_mcp/lifespan.py:32-47`). Each `get()`
  builds lazily on first use and reuses its client until `aclose()`; the owner clears its
  slot *before* awaiting the close, so a call arriving during teardown builds a fresh
  client rather than joining one that is shutting down
  (`src/nextdns_mcp/client.py:683-718`, `src/nextdns_mcp/tools/doh.py:64-93`).
  `close_api_client()` and `close_doh_client()` close the current owner's client
  (`src/nextdns_mcp/client.py:824-839`, `src/nextdns_mcp/tools/doh.py:139-145`). Served
  requests resolve the owner from the FastMCP lifespan context; standalone callers
  publish one in the task context, and an absent owner fails loudly instead of silently
  sharing a process-global client (`src/nextdns_mcp/client.py:721-754`,
  `src/nextdns_mcp/tools/doh.py:96-125`).
- **Legacy spellings keep working without an import-time object.** `client.api_client` and
  `server.mcp_server`/`server.mcp` are served through module `__getattr__` and resolve
  through the current context's owner accessors (`src/nextdns_mcp/client.py:842-846`,
  `src/nextdns_mcp/server.py:255-261`).
- **One shutdown point per server.** `server_lifespan` resolves the resources attached to
  that instance and, in `finally`, closes its API client, the raw probe client, its DoH
  client, cleans up its download registry and redirect client, then resets its context
  vars (`src/nextdns_mcp/lifespan.py:77-117`). The rule in `AGENT.md` follows from the
  decision: any new long-lived client must be closed there too. The optional provider's
  sandbox is stopped by `PrefabGenerativeUI.lifespan()` before this lifespan unwinds, so
  one server's shutdown does not reach a sibling's subprocess
  (`src/nextdns_mcp/generative.py:235-248`).
- **State that belongs to one server runtime is attached to that instance and published
  per lifespan, not per process.** `create_mcp_server()` attaches `ServerResources` to
  each `FastMCP` instance (`src/nextdns_mcp/openapi.py:91-92`); `server_lifespan`
  publishes those owners both into the task context vars and into the FastMCP lifespan
  context, because the lifespan task's context vars do not propagate into request tasks
  (`src/nextdns_mcp/lifespan.py:76-98`). Shutdown reclaims only the downloads that server
  handed out (issue #277), and the generative sandbox is owned by the provider that
  mounted it, not by a process-global set (issue #542).

## What lazy does *not* freeze

The owner fixes the connection pool, the key-derived headers, and the timeout built at
construction (`src/nextdns_mcp/client.py:552-596`). The per-request access-control
decision is not frozen: `_authorize()` re-reads the environment and takes one snapshot
per request (issue #179, `src/nextdns_mcp/client.py:437-453`). So changing a profile
allowlist takes effect on the next call; changing the key or `NEXTDNS_HTTP_TIMEOUT` does
not take effect until that owner's client is rebuilt — which shutdown, or an explicit
close, does.

## Consequences

**Gained**

- Importing the package is free and non-destructive: no `.env` walk, no environment
  mutation, no server or client construction.
- Tests set the environment and then call `configure()`, and see a server built from
  what they set.
- One API connection pool and one DoH connection pool per owner serve every generated
  tool and composite that server publishes — keep-alive reuse against the same host,
  without a sibling's shutdown closing them.
- Each server's teardown has one place to look, one rule to extend, and close-then-reset
  means a configuration change applies after a close without restarting the process.

**Accepted costs**

- A creation-order dependency on configuration: the first caller in an owner's context
  wins for the key and the timeout until that client is closed (see "What lazy does not
  freeze").
- The API and DoH clients are per-owner, not process-wide. A standalone caller must
  publish an owner and an absent one fails loudly rather than silently sharing a client
  across independent servers; embedding two servers gives each its own pools. The raw
  probe client remains process-global because the `/health` probe needs an ACL-free
  transport (`src/nextdns_mcp/client.py:767-789`). The download registry and the sandbox
  are per-server too, so one shutdown cannot reclaim a sibling's downloads or process.
- A long-lived resource added later has to remember the shutdown rule; forgetting it
  leaks a pool, and no unit test fails on the leak.
- The compat `__getattr__` shims keep an old spelling of each singleton alive, so code
  holding `from nextdns_mcp import server; server.mcp` still resolves — through a path
  that hides which owner accessor it depends on.
- Each `/health` route owns its TTL cache and single-flight lock through a
  `ReadinessProber` created when the route is registered
  (`src/nextdns_mcp/health.py:80-100`, `:181-200`), so an embedded pair does not share
  probe state. The cache is a TTL: the worst case is a stale readiness answer for up to
  `HEALTH_CACHE_TTL` seconds, not a leaked resource.

## History

- Issue #150: `api_client` and `mcp_server` built at import time and
  `logging.basicConfig` at import — replaced by lazy factories, pinned by
  `tests/unit/test_lazy_factories.py`.
- Issue #177: `load_dotenv()` at import walked upward and loaded whatever `.env` it
  found; it now runs in the package `__main__` entrypoint before the server is imported
  (`src/nextdns_mcp/__main__.py:25-31`), and the `python -m nextdns_mcp.server` path
  re-execs through it to apply that ordering when the variable was not exported
  (`src/nextdns_mcp/server.py:274-301`).
- Issue #190: import-time environment mutation and instance building moved into
  `configure()`.
- Issue #291: the update-check variable was once read with a bare `os.environ[...]` and
  raised `KeyError` when `configure()` had not set it; it now defaults to `"off"` with
  `setdefault` in `configure()` and in the package entrypoint before FastMCP is imported
  (`src/nextdns_mcp/server.py:217-252`, `src/nextdns_mcp/__main__.py:25-31`).
- Issue #277 (`e808646`, 4.0.0): the download registries stop being one process-global
  set; each server's lifespan owns its own through the context var.
- Issues #408 and #542 (`712c855` #499, `8ffbdab` #564): the lazily-started sandbox
  subprocess is stopped at server shutdown, and its ownership moves from a process-global
  set to the provider that started it.
