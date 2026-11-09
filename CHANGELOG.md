# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.1.0]

### Added

- Interactive Prefab UI charts and dashboards:
  - `overviewDashboard`: Full overview dashboard mirroring the NextDNS web UI with KPI summary cards, queries evolution chart, and resolved/blocked domain tables.
  - Dedicated standalone visual widgets: `kpiAnalytics`, `domainsAnalytics`, `blockedDomainsAnalytics`, `reasonsAnalytics`, `deviceAnalytics`, `ipAnalytics`, `rootDomainAnalytics`, `countryAnalytics`, `gafamAnalytics`, and `securityAnalytics`.
  - Flexible visualization styles (`view_type="auto" | "bar" | "table" | "pie"` and `domain_view="table" | "bar" | "pie"`).
  - FastMCP `GenerativeUI` provider enabled by default (`generate_prefab_ui`, `search_prefab_components`), toggleable via `NEXTDNS_GENERATIVE_UI`.
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
