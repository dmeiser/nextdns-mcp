# NextDNS MCP Server Usage Guide

The server publishes one tool per operation of the bundled OpenAPI document
([nextdns-openapi.yaml](../src/nextdns_mcp/nextdns-openapi.yaml)), plus two
operations the document cannot describe (`plotAnalytics`, `dohLookup`). A tool
is named for the API operation it performs and takes that endpoint's own
arguments, so the name says what the call does — there is no `operation`
argument to choose.

## Core concepts

- **Profile**: A named NextDNS configuration that owns settings, lists, logs,
  analytics, and rewrites. Every profile-scoped tool requires a `profile_id`;
  only `dohLookup` and `plotAnalytics` fall back to `NEXTDNS_DEFAULT_PROFILE`.
- **Access control**: The server reads `NEXTDNS_READABLE_PROFILES` and
  `NEXTDNS_WRITABLE_PROFILES`. Reads/writes outside those profiles are rejected.
- **Read-only mode**: If `NEXTDNS_READ_ONLY=true`, write operations are blocked.
- **Discovery**: By default (`NEXTDNS_TOOL_SEARCH=bm25`), `tools/list` exposes
  only `search_tools` + `call_tool`, and the model finds the operation tools
  by searching; they stay callable either way. Set `NEXTDNS_TOOL_SEARCH=off`
  to advertise the full catalog in the listing instead.

## Finding the tools

Each tool's description is the document's own description of the operation,
and its schema is that endpoint's parameters flattened into one level:
`profile_id` plus the path, query and body fields. Endpoints whose body is a
JSON array (full list replacement) take the array as one `body` argument the
document describes; endpoints whose body is an object have its properties
flattened in beside the path parameters.

Use `tools/list` — or `search_tools` under progressive disclosure — for the
authoritative catalog; this page describes the families, not all 80 names.

## Tool families

### Profiles
`listProfiles`, `createProfile`, `getProfile`, `updateProfile`, `deleteProfile`.
Use `listProfiles` first to discover the `profile_id` you need for other tools
(it paginates: pass the returned `cursor` back as `cursor` for the next page).

### Settings
Each settings category is its own endpoint pair: `getSettings` /
`updateSettings` (core options, e.g. web3 blocking), `getPrivacySettings` /
`updatePrivacySettings`, `getSecuritySettings` / `updateSecuritySettings`,
`getParentalControlSettings` / `updateParentalControlSettings`,
`getPerformanceSettings` / `updatePerformanceSettings`, `getLogsSettings` /
`updateLogsSettings`, `getBlockPageSettings` / `updateBlockPageSettings`.
Read a category first to see its current shape, then send only the fields you
want to change.

### Content lists
Per-domain allow/deny lists (`getAllowlist` / `addToAllowlist` /
`updateAllowlistEntry` / `removeFromAllowlist`, same shape for the denylist),
privacy blocklists and natives (`getPrivacyBlocklists`,
`addPrivacyBlocklist`, `removePrivacyBlocklist`, …), security TLDs
(`getSecurityTLDs`, `addSecurityTLD`, `removeSecurityTLD`), and parental
control categories and services (`getParentalControlCategories`,
`addToParentalControlCategories`, `updateParentalControlCategoryEntry`, …).
Full replacement is its own tool per collection (`replaceDenylist`,
`replacePrivacyBlocklists`, …) taking `body=[{"id": "value"}, ...]`.
Entry updates toggle `active` and address the entry by its id: parental
control spells the path parameter `id`, the other collections `entry_id`.

### DNS rewrites
`listRewrites`, `addRewrite`, `deleteRewrite`. `deleteRewrite` takes the
opaque rewrite `entry_id` from `listRewrites`, not the hostname.

### Logs
`getLogs` — recent entries (`raw=true` for unfiltered logs); `clearLogs` —
delete stored logs; `downloadLogs` — CSV export streamed to a local file, the
payload answers with the `file_path` and a bounded preview. Time values can be
Unix timestamps or relative strings such as `-1d`.

### Analytics
One aggregate tool per metric — `getAnalyticsStatus`, `getAnalyticsDomains`,
`getAnalyticsQueryTypes`, `getAnalyticsReasons`, `getAnalyticsIPs`,
`getAnalyticsDNSSEC`, `getAnalyticsEncryption`, `getAnalyticsIPVersions`,
`getAnalyticsProtocols`, `getAnalyticsDevices`, `getAnalyticsDestinations` —
and a `…Series` twin for time-series data (everything except `domains`).
`getAnalyticsDestinations` requires `type`, one of `countries` or `gafam`.

#### Time window
`from` and `to` accept epoch seconds as a number, or a string: ISO 8601
(`2026-10-30T00:00:00Z`), a bare date (`2026-10-30`), `now`, or a relative
offset from now (`-1d`, `-12h`, `-30d`). `from` is optional on the aggregate
and logs endpoints: a request that names none covers the last day (`-1d`).
The `…Series` endpoints require `from` — upstream refuses a series request
that names none — so for the last day write `-1d`.
`to` omitted ends at now. The Grafana spelling `now-1d` is
not accepted upstream; write `-1d`.
`interval` (the `…Series` and `plotAnalytics` window size) is bounded to
60–86400 seconds, and `partials` accepts only `none` or `all`. The 2000-point
budget described under `plotAnalytics` guards the generated `…Series` tools as
well: a range that would return more points than that is refused with
`invalid_argument` rather than answered with a truncated series.

### plotAnalytics
Generate a PNG line chart for supported metrics. Use a profile with query
history. Returns an MCP image or an error if no data is available.

A series is limited server-side to 2000 points (the requested range divided by
`interval`) with `interval` between 60 and 86400 seconds. A request past either
limit is rejected with an `invalid_argument` error rather than silently
adjusted. `limit` must not exceed 500 and is rejected above that; the series
endpoints consume no `limit`, so it is validated but never sent.

### dohLookup
`dohLookup(domain="example.com", profile_id="<profile id>", record_type="A")`

## Common workflows

### Block a domain
1. `addToDenylist(profile_id="<profile id>", id="bad.example.com")`

### Allow a domain
1. `addToAllowlist(profile_id="<profile id>", id="safe.example.com")`

### Block a parental control category
1. `addToParentalControlCategories(profile_id="<profile id>", id="gambling")`

### Toggle a parental-control service
1. `updateParentalControlServiceEntry(profile_id="<profile id>", id="tiktok", active=False)`

### View blocked query trends
1. `getAnalyticsStatusSeries(profile_id="<profile id>", from="-1d", interval=3600)`
2. `plotAnalytics(metric="status", profile_id="<profile id>", from_time="-1d")`

### Add a DNS rewrite
1. `addRewrite(profile_id="<profile id>", name="router.home", content="192.168.1.1")`

### Delete a DNS rewrite
1. `listRewrites(profile_id="<profile id>")` — find the entry's opaque `id`
2. `deleteRewrite(profile_id="<profile id>", entry_id="<id-from-list>")`
