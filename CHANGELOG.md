# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.2.1]

### Fixed

- Combined the nested conditionals in `scripts/validate_schema.py` into a single
  `elif` so newer Ruff versions (SIM102) pass, and widened the PR lint steps to
  cover `scripts/` in addition to `src` and `tests`.
- Added the missing `requires_deno` skip marker to the host-smoke state-leak
  test, and the weekly maintenance release gate now installs Deno so the
  sandbox smoke tests execute there instead of silently skipping.

## [4.2.0]

### Security

- Hardened the generative-UI sandbox end to end: the Deno release download is
  checksum-verified and `curl`/`unzip` are removed from the runtime image,
  sandbox stdout reads enforce `MAX_RESULT_BYTES`, code-size and data
  validation run before AST parsing and serialization, chart-placement
  validation is an iterative walk, the sandbox environment, V8 heap, and
  interpreter isolation are tightened, and sandbox teardown is scoped to the
  server lifespan.
- `generate_prefab_ui` is annotated as non-read-only and its sandbox gained
  timeout and size limits.
- `x-mcp-destructive` is honored for `destructive_hint`, `open_world_hint` is
  set explicitly on all tool annotations, and non-endpoint write tools refuse
  loose or ambiguous name matches.
- Upstream-controlled dashboard and chart fields, including DNS labels in
  summaries, are sanitized consistently.

### Fixed

- `overviewDashboard` correctness: `section`/`view_type`/`domain_view` and
  time ranges are validated up front; `view_type`/`domain_view` are honored
  across all cards, with `domain_view` scoped to domain cards so
  `view_type="auto"` survives; the countries card is included in
  `section="all"` with `limit` forwarded; `_item_queries` is guarded against
  malformed and infinite upstream counts; sub-request errors propagate instead
  of being swallowed; and production asserts on request paths are replaced.
- Empty-state cards render consistently when a section has no data.
- `_parse_ui_data` rejects malformed and non-dict payloads.
- The Deno/Pyodide sandbox subprocess stops on server shutdown.
- The Prefab component walk in the call log is bounded.
- `limit` is validated and forwarded in `overviewDashboard` and
  `plotAnalytics`.
- Boolean environment parsing is unified with recognized-token validation, and
  surrounding whitespace is stripped from `NEXTDNS_READ_ONLY`.
- The streaming-path payload cap applies only to series endpoints.
- Chart `Column` children are passed instead of mutating the child list.
- `is_float_shaped` rejects malformed numerics with an interior sign stripped.
- Queued dashboard fan-out waves short-circuit on an early typed error.
- Chart downsampling is shared between dashboard and plots, interval counts
  are reported truthfully, and dashboard payloads are bounded.

### Changed

- `dashboard.py` is split into a package over a neutral charts module, with
  dashboard sections single-sourced and a shared `build_timeseries_area_chart`
  factory and analytics-card builder.
- The triplicated domain-card analytics implementations are consolidated, and
  single-card handlers, dispatch wrappers, and summary formatters are
  deduplicated.
- `fastmcp` moved from a `==4.1.2` pin to a `>=4.1.8` floor.
- CI, packaging metadata, and docs are aligned on Python 3.15, and the runtime
  image is `python:3.15-slim`.
- CI and release hardening: Docker images are not published for `no-release`
  merges, the docker-publish gate fails closed, the revert watchdog validates
  `HEAD_SHA` and checks out the repo before opening auto-revert PRs, and Auto
  Release polls the unit-tests check-run instead of racing it.
- Live E2E suites are serialized repo-wide with backoff on upstream 429s,
  `Retry-After` headers are surfaced, and E2E write bursts are paced.

### Performance

- `overviewDashboard` upstream fan-out is bounded.
- `plotAnalytics` chart series are downsampled above 250 points.
- Destination domain labels accumulate only to the display budget.
- Each chart series' query list is read once per chart build.
- Call-log intermediates are serialized once, without `sort_keys`.

### Docs

- Documented the `NEXTDNS_API_BASE` environment override.
- Added the trusted-host contract for generative UI and documented the FastMCP
  private members borrowed by `NextDNSGenerativeUI`.
- Analytics tool docstrings state their differing default time windows.
- Corrected stale references to hand-written tools, image-only plotting,
  dashboard fail-closed behaviour, UI tool annotations, and the sandbox
  inventory.
- `AGENT.md` states the why-only comment rule.

## [4.1.0]

### Added

- Interactive Prefab UI charts and dashboards:
  - `overviewDashboard`: Consolidated analytics dashboard tool supporting `section` selection (`"overview"`, `"all"`, `"kpi"`, `"domains"`, `"blocked_domains"`, `"reasons"`, `"devices"`, `"ips"`, `"root_domains"`, `"countries"`, `"gafam"`, `"security"`), with flexible formatting (`view_type="auto" | "bar" | "table" | "pie"` and `domain_view="table" | "bar" | "pie"`).
  - Compact factual text summaries in `ToolResult` alongside `PrefabApp` structured content across all dashboard sections and plots, ensuring text-only clients and LLMs have actual data values without behavioral prompt steering.
  - FastMCP `GenerativeUI` provider available as an opt-in beta feature (`generate_prefab_ui`, `search_prefab_components`), disabled by default and enabled via `NEXTDNS_GENERATIVE_UI=on` (or `true`).
  - Added `/v1/models` route to satisfy OpenWebUI's periodic model discovery probes without 404s.

### Changed

- `plotAnalytics` now renders interactive Prefab UI `AreaChart` with smooth curves and soft gradient fills instead of static PNGs.
- Replaced `matplotlib` dependency with pinned `prefab-ui==0.21.1`, eliminating its ~35MB footprint and cold import overhead.

## [4.0.0]

### Breaking

- The eight grouped management tools are replaced by one tool per operation of
  the shipped OpenAPI document (78 generated tools), registered under the
  operationId names, plus the two hand-written composites (`plotAnalytics`,
  `dohLookup`). There is no `streamLogs` replacement: poll `getLogs` or call
  `downloadLogs`. See [docs/migration.md](docs/migration.md) for the
  old-tool-to-new-tool mapping.
- List and rewrite entries are addressed by their opaque entry `id` (the `id`
  field of the endpoint's list response); the old hostname/value addressing is
  gone and there is no hostname→id resolution.
- `tools/list` no longer enumerates the catalog when `NEXTDNS_TOOL_SEARCH` is
  enabled (the default): discovery runs through `search_tools`/`call_tool`
  (progressive disclosure), and a query that matches nothing falls back to the
  whole catalog, reads first.

### Security

- Per-profile access control: read/write allowlists, a protected-profile
  denylist, and read-only mode, decided from the rendered path of each request.
- The log-download redirect chain is vetted hop by hop (https only, globally
  routable hosts, pinned DNS for the dialled address) and fetched
  unauthenticated, so the API key never crosses to a redirect host and a
  hostile `Location` cannot probe internal services.
- Signed download URLs are never included in error payloads, logs, or tool
  answers.
- The test harness (`scripts/ai_e2e_harness.py`) protects every pre-existing
  profile before a key-bearing run starts and fails closed on any probe or
  pagination failure.

### Added

- `downloadLogs`: streams the full log CSV to a bounded temp file and returns
  only a bounded preview; total size capped by `NEXTDNS_DOWNLOAD_MAX_BYTES`.
- `plotAnalytics` renders a PNG chart from any analytics `;series` endpoint.
- Search answers and series responses carry byte/point budgets
  (`SEARCH_ANSWER_MAX_BYTES`, `MAX_SERIES_RESPONSE_BYTES`).
- Query length is capped at 512 characters so a huge query cannot stall the
  server.
- `docs/migration.md`: the 3.x → 4.0.0 tool mapping.

### Fixed

- Error payloads set the MCP `isError` flag consistently, including the two
  hand-written composite tools.
- Successful `downloadLogs` temp files are reclaimed per server lifespan (with
  bounded retention), so one embedded server can no longer delete another
  live server's CSVs.
- Tool annotations reflect side effects: `downloadLogs` is not marked
  read-only despite its GET method.
