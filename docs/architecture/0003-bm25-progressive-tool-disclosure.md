# ADR 0003 — Progressive tool disclosure by BM25 search ("search, then call by exact name")

Status: accepted (4.0.0, and the default since). Applies to `src/nextdns_mcp/tool_search.py`,
`src/nextdns_mcp/tool_search_rank.py`, the transform mount in `src/nextdns_mcp/openapi.py`,
the `NEXTDNS_TOOL_SEARCH` flag in `src/nextdns_mcp/config.py`, and the `initialize`
instructions in `src/nextdns_mcp/usage.py`. It sits **on top of** ADR 0001: the catalog is
generated there, disclosed here.

## Context

ADR 0001 replaced eight grouped tools with one tool per operation — 78 generated
operations plus the composites the document cannot describe. That fixed the catalog's
completeness and immediately created a second problem: `tools/list` now advertises all of
them, each with its own spec description, in every session, for every client.

What that costs was measured, not guessed. Four fully-described search results still
measured about 10 KB, and the live run that carried 10-18 KB per discovery stalled 27
times in 29 steps (`src/nextdns_mcp/tool_search.py:66-75`).

The failure mode of naive disclosure was measured too. FastMCP's `BM25SearchTransform`
ranks prose similarity, and small models query with tool-shaped guesses —
`updateProfile`, `replace_parental_services`, `add dns rewrite record` — rather than task
descriptions, so the search comes back empty and the model then invents a name that is not
in the catalog at all and fails the step (`src/nextdns_mcp/tool_search.py:9-16`).

The options:

1. **Keep the full listing** and accept the context cost forever.
2. **FastMCP's pattern-match transform.** Rejected before this server shipped: it ranks
   nothing (matches come back in registration order) and none of the live model
   validations that shaped this feature ran under it
   (`src/nextdns_mcp/config.py:216-221`).
3. **FastMCP's BM25 transform, with its two failure modes closed here** — the choice made.

## Decision

**Default to `bm25`: `tools/list` publishes only the synthetic `search_tools` +
`call_tool` pair, and the real catalog is discovered by searching it. Two generic safety
nets make discovery survive a bad query, and disclosure never touches authorization.**

- The mode is an environment decision resolved once: `NEXTDNS_TOOL_SEARCH` accepts `off`
  and `bm25`, unset means `bm25`, and any other value fails startup with
  `ConfigurationError` (`src/nextdns_mcp/config.py:216-252`). `bm25` is the default
  because every live-model validation of this contract ran under it; the full listing is
  the explicit opt-out.
- The transform is mounted when the `FastMCP` instance is constructed, because the
  middleware that repairs `call_tool` shape has to exist on the instance before any call
  is served (`src/nextdns_mcp/openapi.py:649-691`). A mode that config accepts and this
  module has not implemented raises at startup rather than silently meaning "no
  disclosure" (`src/nextdns_mcp/tool_search.py:856-875`).
- The synthetic names stay FastMCP's: `search_tools` plural, `call_tool` singular. That
  asymmetry is upstream's contract, not a local inconsistency to rename
  (`src/nextdns_mcp/tool_search.py:26-28`). Their descriptions are replaced with this
  server's discovery contract, because under disclosure they are the only always-visible
  place to say that the catalog is hidden and that guessing a name fails
  (`src/nextdns_mcp/tool_search.py:255-283`).
- Two safety nets, both task-independent and client-agnostic — no prompt hints, no
  per-task routing (`src/nextdns_mcp/tool_search.py:16-25`):
  1. **Full-catalog fallback.** A query that matches nothing answers with every tool,
     reads first, rather than an empty list
     (`src/nextdns_mcp/tool_search.py:228-247`).
  2. **Name-aware re-rank.** A tool whose own name carries the caller's words is never
     left out of the answer, and ranks first, with BM25's order preserved on ties. The
     re-rank runs over the full merged list and the slice comes last — slicing first cut
     tools out before the name scorer ever saw them
     (`src/nextdns_mcp/tool_search.py:192-252`, ranking machinery in
     `src/nextdns_mcp/tool_search_rank.py`).
- Read/write ordering in the fallback is a fact, not a name shape. Classification comes
  from the endpoint map built from the shipped document's routes; a name the map does not
  carry — the three composites and the generative-UI pair — is classified from its own
  `readOnlyHint`. The name heuristic this replaced read every `list*` tool as a write, and
  defaulting off-map names to `GET` let a loose match reach `generate_prefab_ui`, a tool
  that executes code (`src/nextdns_mcp/tool_search.py:94-131`).
- Byte budgets are part of the contract: 1,400 characters of description per result, 8 KB
  for a ranked answer, 128 KB for the fallback, top 6 ranked entries
  (`src/nextdns_mcp/tool_search.py:64-86`). The fallback gets its own larger ceiling
  because it *is* the catalog: an answer that silently drops entries teaches the caller
  the catalog is smaller than it is (`src/nextdns_mcp/tool_search.py:238-247`).
- `CallToolShapeMiddleware` repairs the three ways live callers wrap `call_tool` wrong —
  the whole search answer where a name belongs, a name typed from memory, and parameters
  sent beside `name` instead of inside `arguments`. It runs first, before the extra-field
  strip (which would delete the misplaced parameters) and before the call log (which then
  records the call that actually happened). A re-target is reported on the payload and in
  the log, and an answer whose head is a write is refused rather than run
  (`src/nextdns_mcp/tool_search.py:673-697`, `src/nextdns_mcp/openapi.py:680-689`).
- The `initialize` instructions describe the catalog by name only when it is small enough
  for that to be cheaper than the search every caller would otherwise run — 12 tools or
  fewer. Above that they point at `search_tools` and leave the names to `tools/list`
  (`src/nextdns_mcp/usage.py:22-50`).
- Disclosure hides names; it does not grant or withhold permission. The ACL, the
  destination allow-list, redaction, and the typed error contract live on the client every
  call path goes through, so a hidden tool is still callable and a visible tool is still
  denied when it should be (ADR 0001 "On top of the surface", and
  `docs/troubleshooting.md:41-42`).

## Consequences

**Gained**

- The catalog can keep growing with the document without a per-session context cost, and
  without the client holding names it will never use.
- A query that matches nothing degrades to the whole surface, reads first, instead of an
  empty list that sends the model off to invent a tool.
- A caller that names a tool gets that tool, whatever its description happens to say.
- Mis-shaped `call_tool` wrappers become a call, or a refusal that says why, instead of a
  step that ends with no call at all.

**Accepted costs**

- `tools/list` is no longer the catalog by default. A client that expects to enumerate
  tools has to set `NEXTDNS_TOOL_SEARCH=off`, and that is the most common question about
  this server — which is why it is answered in `README.md`, `docs/getting-started.md`,
  `docs/usage.md`, `docs/faq.md`, and `docs/troubleshooting.md`.
- `search_tools` and `call_tool` are public contract now: renaming them breaks every
  client that learned them.
- A ranked search answer is not the whole catalog, so a caller must not infer catalog
  size from one answer.
- The transform borrows FastMCP's private surface — `_search`, `_make_search_tool`,
  `_make_call_tool`, `_render_results`, and the privately-set `_max_results` — which is
  why the `fastmcp>=4.1.8` floor in `pyproject.toml` is kept with it, and why the borrowed
  attributes are named in the subclass docstring so a dependency bump finds them instead
  of failing at request time (`src/nextdns_mcp/tool_search.py:125-141`).
- The word memos and per-tool payload caches assume a static catalog registered once at
  startup. A tool re-registered at runtime is a fresh instance with a fresh entry, and the
  stale entry lives for the life of the process.
- Read/write classification drives ordering and the refusal of a re-targeted write; it is
  not the authorization layer, and a wrong classification is a ranking bug, never a grant.

## History

- `e808646` (#378, 4.0.0): progressive disclosure lands with the one-tool-per-operation
  surface — "discovery is gated, access is not" — initially offered as three modes.
- The pattern-match mode is dropped before that release ships; the accepted set is `off`
  and `bm25`, with the reason recorded at `src/nextdns_mcp/config.py:216-221`.
- `81142b4` (#471): documentation re-pointed at the hand-written tools the surface rewrite
  left behind.
- `614f829` (#570): off-map tools are classified from their own `readOnlyHint` instead of
  a default `GET`, closing the path where a loose BM25 match could invoke
  `generate_prefab_ui`.
- `6df1cd5` (#590): the `fastmcp` pin becomes a `>=4.1.8` floor — the version the borrowed
  search-transform surface is pinned against.
