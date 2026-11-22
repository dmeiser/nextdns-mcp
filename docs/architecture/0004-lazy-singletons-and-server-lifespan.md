# ADR 0004 — Lazy process singletons and one shutdown point

Status: accepted (3.x; the pattern predates the 4.0.0 tool-surface rewrite — it already
exists at the shallow-history boundary commit `10a3729`). Applies to
`src/nextdns_mcp/server.py`, `src/nextdns_mcp/client.py`,
`src/nextdns_mcp/tools/doh.py`, and the server lifespan in `src/nextdns_mcp/openapi.py`.

## Context

Three objects in this process are expensive or awkward to create more than once: the
FastMCP server (one parse of the shipped document and 81 tool registrations), the
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
reason for existing at all: module-level persistence for connection reuse, mirroring the
API client (`src/nextdns_mcp/tools/doh.py:62-63`).

## Decision

**Three shared instances, each created on first use and cached at module level;
construction is triggered explicitly, never by import; shutdown has exactly one owner —
the server lifespan.**

- **The server** is `None` until `get_mcp_server()` builds it
  (`src/nextdns_mcp/server.py:79`, `:172-177`). The side effects — disabling FastMCP's
  automatic update check and building the instance — live in `configure()`, which the
  entrypoint and the tests call on purpose (`src/nextdns_mcp/server.py:197-216`).
  Importing the module does neither (issues #177 and #190), and that is pinned by
  `tests/unit/test_server_dotenv.py` and `tests/unit/test_lazy_factories.py`.
- **The clients** follow the same shape: `get_api_client()`
  (`src/nextdns_mcp/client.py:446-455`) and `_get_doh_client()`
  (`src/nextdns_mcp/tools/doh.py:64-72`). `close_api_client()` and `close_doh_client()`
  clear the module global *before* awaiting the close, so a call arriving during teardown
  builds a fresh client rather than joining one that is shutting down
  (`src/nextdns_mcp/client.py:461-468`, `src/nextdns_mcp/tools/doh.py:75-83`).
- **Legacy spellings keep working without an import-time object.** `client.api_client` and
  `server.mcp_server`/`server.mcp` are served through module `__getattr__` and resolve
  through the same accessors (`src/nextdns_mcp/client.py:474-477`,
  `src/nextdns_mcp/server.py:219-225`).
- **One shutdown point.** `_server_lifespan` closes the API client, then the DoH client,
  each in a `finally` so a failure in one cannot strand the other
  (`src/nextdns_mcp/openapi.py:577-604`). The rule in `AGENT.md` follows from the
  decision: any new long-lived client must be closed there too.
- **State that belongs to one server runtime is registered per lifespan, not per
  process.** The download registry is a `ContextVar` set at the start of the lifespan and
  reset at the end, so shutdown reclaims only the downloads that server handed out
  (`src/nextdns_mcp/openapi.py:579-603`), and the generative sandbox is owned by the
  provider that mounted it, not by a process-global set (issue #542).

## What lazy does *not* freeze

The singleton fixes the connection pool, the key-derived headers, and the timeout built
at construction (`src/nextdns_mcp/client.py:414-443`). The per-request access-control
decision is not frozen: it re-reads the environment and takes one snapshot per request
(issue #179, `src/nextdns_mcp/client.py:343-348`). So changing a profile allowlist takes
effect on the next call; changing the key or `NEXTDNS_HTTP_TIMEOUT` does not take effect
until the client is rebuilt — which shutdown, or an explicit close, does.

## Consequences

**Gained**

- Importing the package is free and non-destructive: no `.env` walk, no environment
  mutation, no server or client construction.
- Tests set the environment and then call `configure()`, and see a server built from
  what they set.
- One connection pool per process serves every generated tool and composite — keep-alive
  reuse against the same host.
- Teardown has one place to look, one rule to extend, and close-then-reset means a
  configuration change applies after a close without restarting the process.

**Accepted costs**

- A creation-order dependency on configuration: the first caller's environment wins for
  the key and the timeout until the client is closed (see "What lazy does not freeze").
- The two HTTP clients are process-wide, not per-server. Embedding two servers in one
  process shares them, so one server's shutdown closes the pool the other is using; the
  survivor recovers because the next call rebuilds the client, at the cost of a fresh
  handshake. The download registry and the sandbox were moved to per-server ownership
  precisely because closing *them* for a sibling was destructive, not merely disruptive.
- A long-lived resource added later has to remember the shutdown rule; forgetting it
  leaks a pool, and no unit test fails on the leak.
- The compat `__getattr__` shims keep an old spelling of each singleton alive, so code
  holding `from nextdns_mcp import server; server.mcp` still resolves — through a path
  that hides which accessor it depends on.
- The health probe's TTL cache and single-flight lock are process-level too
  (`src/nextdns_mcp/openapi.py:104-114`), so an embedded pair shares probe state. The
  cache is a TTL: the worst case is a stale readiness answer for up to
  `HEALTH_CACHE_TTL` seconds, not a leaked resource.

## History

- Issue #150: `api_client` and `mcp_server` built at import time and
  `logging.basicConfig` at import — replaced by lazy factories, pinned by
  `tests/unit/test_lazy_factories.py`.
- Issue #177: `load_dotenv()` at import walked upward and loaded whatever `.env` it
  found; it now runs only in the `__main__` entrypoint
  (`src/nextdns_mcp/server.py:340-345`).
- Issue #190: import-time environment mutation and instance building moved into
  `configure()`.
- Issue #291: `_sync_fastmcp_update_check()` subscripted the update-check variable with a
  bare `os.environ[...]` and raised `KeyError` when `configure()` had not set it; it now
  reads with the same `"off"` fallback as `configure()` applies
  (`src/nextdns_mcp/server.py:180-194`).
- Issue #277 (`e808646`, 4.0.0): the download registries stop being one process-global
  set; each server's lifespan owns its own through the context var.
- Issues #408 and #542 (`712c855` #499, `8ffbdab` #564): the lazily-started sandbox
  subprocess is stopped at server shutdown, and its ownership moves from a process-global
  set to the provider that started it.
