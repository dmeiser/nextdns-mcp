# Generative UI Trusted-Host Contract

NextDNS MCP exposes a Generative UI provider (`generate_prefab_ui`) that executes Prefab Python in a sandbox and renders the resulting tree in the host client. The generated tree crosses host-controlled boundaries the backend cannot verify, so the **host client** must provide the guarantees below. Where a guarantee is unavailable, the host (not this server) must restrict output to passive, allowlisted components and reject dangerous actions and raw HTML; the server does not enforce such a filter today.

## Trusted-host guarantees required

1. **Iframe sandboxing.** Render generated trees inside a sandboxed iframe with `sandbox="allow-scripts"` only; never `allow-same-origin`, `allow-top-navigation`, `allow-forms`, or `allow-popups` unless explicitly required and audited.
2. **Content Security Policy.** Apply a CSP that permits only the server-provided renderer origin and explicitly allowlisted asset domains (`resourceDomains`); block inline eval of untrusted code and `data:`/`javascript:` URIs.
3. **Network/navigation policy.** Block outbound navigation and arbitrary network requests from the rendered tree; only the host may initiate requests, and only to approved origins.
4. **UI-initiated tool calls.** Treat any tool call originating from rendered UI as untrusted input: validate arguments server-side, enforce the same read/write ACLs and profile scoping as direct calls, and require explicit user confirmation for destructive actions.
5. **Chart sizing.** Provide a measurable container width before charts render; charts placed in zero-width containers render blank (see sizing rules in tool docs).

## Fallback when guarantees are unavailable (host obligation)

If the host cannot provide the above (e.g. clients without iframe sandboxing or CSP support), output must be limited to passive, allowlisted Prefab components (text, tables, charts, static layout) and must reject:
- raw HTML or script injection,
- navigation/link actions,
- form submissions and tool calls initiated from UI,
- external resource loading beyond the allowlist.

## Backend-side guards

The server additionally enforces:
- Chart placement validation (charts may not be nested in `Tab` components).
- Tool annotations on the UI tools: `generate_prefab_ui` is annotated `read_only_hint=False, destructive_hint=False, open_world_hint=False` — it executes generated code, so it is not published as read-only — while `search_prefab_components` is `read_only_hint=True` (`src/nextdns_mcp/generative.py`).
- The composite UI tools `overviewDashboard` and `plotAnalytics`, registered with `app=True` in `src/nextdns_mcp/server.py`, publish Prefab UI views without going through `generate_prefab_ui`, and carry `read_only_hint=True, destructive_hint=False, open_world_hint=True`.
- CSP built via `_build_csp()` applied to the renderer resource.

Hosts remain responsible for the guarantees above; backend guards are defense in depth, not a substitute.

Why the provider is opt-in, how the sandbox is hardened, and what those guards are in service of is recorded in [ADR 0002](architecture/0002-generative-ui-opt-in-provider.md).
