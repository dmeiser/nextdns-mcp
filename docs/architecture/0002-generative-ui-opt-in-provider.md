# ADR 0002 — Generative UI: an opt-in provider that runs model-written code

Status: accepted (4.1.0, still opt-in beta). Applies to `src/nextdns_mcp/generative.py`,
`src/nextdns_mcp/generative_sandbox.py`, the `NEXTDNS_GENERATIVE_UI` flag in
`src/nextdns_mcp/config.py`, and the mount point in
`src/nextdns_mcp/server.py`. What a host client owes the tree it renders is a separate
record — [docs/generative-ui.md](../generative-ui.md) — and this one is why the
server-side feature is shaped the way it is.

## Context

4.1.0 replaced static PNG plotting with interactive Prefab UI, and that left the catalog
holding two different kinds of UI:

- `overviewDashboard` and `plotAnalytics` render component trees this project wrote. The
  server knows every component in the tree before a call happens
  (the `_COMPOSITE_TOOLS` registry, `src/nextdns_mcp/server.py:73-78`).
- FastMCP's `GenerativeUI` provider is the other shape: the **model** writes Python and
  the server executes it (`generate_prefab_ui(code, data)` answers with a `PrefabApp`).
  Nothing else in the catalog runs caller-authored code.

Executing model-written Python in a process that holds the NextDNS API key is a different
risk class from every other tool, and the exposures were concrete — most of them inherited
from the upstream defaults the provider starts from:

- `prefab_ui.sandbox.PyodideSandbox` copies `os.environ` verbatim into the Deno worker,
  and generated code runs with Pyodide's full `os` module, so `NEXTDNS_API_KEY` was
  readable from model-authored code (`src/nextdns_mcp/generative_sandbox.py:96-108`).
- The upstream worker is granted a 4 GiB V8 heap and keeps one warm interpreter reused
  across calls: a runaway render costs the host memory, and imports from one call can
  poison later ones (`src/nextdns_mcp/generative_sandbox.py:39-41`, `:382-395`).
- The worker is a lazily-started subprocess that, once started, outlived the server that
  started it (issue #408).
- The answer is a tree the **client** renders, across boundaries this server cannot
  verify: iframe sandboxing, CSP, navigation policy. Those guarantees belong to the host
  ([docs/generative-ui.md](../generative-ui.md)).

The options were: enable the provider by default as upstream would have it; ship it
hardened behind an explicit opt-in; or not ship it. Default-on makes every deployment of a
DNS-management server a code-execution host without asking. Not shipping it leaves a real
capability — model-authored views over real data — unavailable to the hosts that want it
and can sandbox it.

## Decision

**Ship the provider opt-in, harden it on the server side, and write the host's
obligations down instead of assuming them.**

- The flag is read at mount time and nowhere else. `is_generative_ui_enabled()` is true
  only for `true`/`1`/`on`/`yes` (`src/nextdns_mcp/config.py:231-237`), and an
  unrecognized value fails startup with `ConfigurationError` rather than quietly
  disabling the feature (`validate_configuration()`'s env parse, `src/nextdns_mcp/config.py:349-355`). `build_mcp_server()` mounts
  the provider only when the flag is true (`register_generative_ui_provider()` at `src/nextdns_mcp/server.py:208`, with the gate in
  `src/nextdns_mcp/catalog.py:79-82`).
- The catalog stays self-describing under the flag. `published_tool_names()` adds the
  provider's two names in the same conditional that registers them
  (`published_tool_names()`, `src/nextdns_mcp/server.py:163-177`), so the `initialize` instructions, schema
  validation's completeness checks, and the container pre-flight see the pair exactly
  when the provider publishes it. No consumer hardcodes the conditional surface.
- The two tools are partitioned by what they answer with, declared once on the module
  that registers them and exported so validation imports the split instead of restating
  it: `generate_prefab_ui` answers with UI content (so no payload exists for a schema to
  pin), `search_prefab_components` answers with plain JSON whose shape validation pins
  (`src/nextdns_mcp/catalog.py:28-55`).
- Code execution is annotated as code execution: `generate_prefab_ui` carries
  `read_only_hint=False, destructive_hint=False, open_world_hint=False`, while
  `search_prefab_components` is read-only (`src/nextdns_mcp/generative.py:302-318`). A
  tool that runs generated code is never published as a read.
- The upstream sandbox is owned, not trusted or subclassed. `DenoPyodideSandbox`
  (`src/nextdns_mcp/generative_sandbox.py`) keeps the worker's whole lifecycle in this
  repository — the subprocess environment carries only an explicit allowlist of variables,
  the V8 heap is bounded to 1 GiB, and the worker is discarded after every execution so no
  interpreter state survives a call (`src/nextdns_mcp/generative_sandbox.py:58-112`,
  `:353-467`). Inheriting `prefab_ui.sandbox.PyodideSandbox` and overriding its private
  startup was the earlier shape; it meant an upstream rename of that private surface could
  silently drop one of these guards, so the class now speaks the runner's wire protocol
  itself. The Deno invocation is pinned:
  `--no-config`, `--frozen` against the vendored lock, and network to
  `cdn.jsdelivr.net:443` only (`src/nextdns_mcp/generative_sandbox.py:160-192`). Size and time are
  budgeted — 32 KiB of code, 128 KiB of data, 256 KiB of result, 10 s of execution, and a
  separate 60 s cold-start budget so a Pyodide boot never eats the user-code timeout — and
  the worker's stdout is read under a byte cap (`src/nextdns_mcp/generative_sandbox.py:30-46`,
  `:271-332`).
- A failure mode that was observed is refused before execution: an AST walk rejects a
  chart nested inside a `Tab`, because tab panels initialize at 0 px width and the chart
  renders blank with nothing for the caller to act on
  (`src/nextdns_mcp/chart_placement.py:80-91`).
- The renderer resource ships with the CSP domains read from the renderer's own
  declaration and FastMCP's UI MIME type (`src/nextdns_mcp/generative.py:158-180`). What the host must
  add around it is documented, not implied ([docs/generative-ui.md](../generative-ui.md)).
- The subprocess belongs to the provider that started it. It is created lazily, owned by
  the provider's own FastMCP lifespan, stopped off the event loop so a slow teardown
  cannot stall the rest of shutdown, and reset so teardown is idempotent
  (`src/nextdns_mcp/generative.py:236-270`).

## Consequences

**Gained**

- Hosts that want model-authored views over real data can have them; everyone else keeps a
  server that runs no caller-authored code.
- The default catalog is unchanged (81 tools), and the conditional pair is derived from
  the same flag everywhere the catalog is reasoned about.
- Hardening sits where the risk is: no key material in the worker, a bounded heap, one
  fresh interpreter per call, pinned dependencies inside the sandbox, and byte/time
  ceilings on both directions of the exchange.

**Accepted costs**

- Two published tools are conditional, so any new consumer that pins the catalog must read
  the flag or import the server's accessors
  (`generative_ui_tool_names()` and friends, `src/nextdns_mcp/catalog.py:28-55`); a
  hardcoded list goes stale silently.
- The provider used to borrow nine private FastMCP members (`_gen`, `_build_csp`,
  `_local._add_component`, `_setup_done`, `_get_sandbox`, `_sandbox`, `_tool_name`,
  `_components_tool_name`, `_include_components_tool`), and a FastMCP bump could have
  broken the borrow at request time. That borrow is gone: the provider composes its own
  tools and resource through `Provider`'s documented hooks and prefab-ui's public
  functions, so no private-member contract needs to ride along with the
  `fastmcp>=4.1.8` floor.
- Deno is a runtime dependency of the opt-in feature: with no `deno` on `PATH` the
  provider raises when the sandbox first starts
  (`src/nextdns_mcp/generative_sandbox.py:163`).
- Sandbox teardown is deliberately *not* in the server lifespan: that module has no
  dependency on the optional generative module (the `PrefabGenerativeUI.lifespan()` comment, `src/nextdns_mcp/lifespan.py:14-18`). A
  future optional subsystem owned by the server needs its own shutdown owner — the
  process-global registry that used to own it killed sibling servers' sandboxes (issue
  #542).
- The host contract is not enforced here. A client that cannot sandbox an iframe or apply
  a CSP is told what to restrict instead ([docs/generative-ui.md](../generative-ui.md)),
  and the server's own guards are defense in depth, not a substitute.

## History

- 4.1.0 (`bbe2e5c`, #386): interactive Prefab dashboards plus the opt-in provider;
  `matplotlib` replaced by the pinned `prefab-ui` dependency.
- `fd418e0` (#462): `generate_prefab_ui` marked non-read-only — it executes generated
  code, so publishing it as a read was wrong.
- `2879ad0` (#460): `_parse_ui_data` rejects malformed and non-dict payloads rather than
  passing them through.
- `712c855` (#499): the Deno/Pyodide subprocess is stopped on server shutdown (issue
  #408).
- `4948b22` (#508): execution timeout, input size limits, and a bounded result size for
  `generate_prefab_ui`.
- `6c9b53a` (#512): the chart-in-tab check moves from a text pattern to an AST walk.
- `4b115f0` (#503): `open_world_hint` set explicitly on every tool annotation.
- `36c78f7` (#565), `a283108` (#562), `2f1ab1b` (#569): result size enforced during the
  bounded stdout read; size guards applied before parsing and serialization; the walk
  rewritten iteratively so deep nesting cannot raise.
- `ab2863d` (#572, closes #541): host secrets stripped from the worker environment, V8
  heap bounded to 1 GiB, one fresh interpreter per execution, and the 60 s cold-start
  budget split from the 10 s execution budget.
- (WORK-283): the sandbox classes that subclassed `PyodideSandbox` and the provider that
  extended FastMCP's `GenerativeUI` are replaced by `DenoPyodideSandbox` and
  `PrefabGenerativeUI(Provider)`, which own the worker and the tool surface instead of
  borrowing upstream privates; the startup-read EOF failure and the environment allowlist
  landed in the same module afterwards (WORK-239, WORK-246).
- `8ffbdab` (#564): teardown scoped to this provider's lifespan — a process-global set
  had one embedded server's shutdown killing every sibling's sandbox (issue #542).
