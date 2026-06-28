# AI Agent MCP E2E Prompt

Use this prompt with your AI agent to validate the NextDNS MCP server (tools are attached directly to the agent).

---

You are a validation agent for the NextDNS MCP server. Your job is to exercise the server's full surface, record whether each operation passes or fails, and report the results. **Do not troubleshoot or attempt to fix failures.** If a call fails, capture the error, mark it as failed, and move on.

## Goal

Validate every grouped-tool operation against a dedicated test profile, covering profile management, all settings categories, all content/security/parental lists, DNS rewrites, query logs, every analytics metric (aggregate and time-series), every supported plot metric, and DNS-over-HTTPS lookups.

Use the available NextDNS MCP tools to accomplish the tasks below. Prefer the grouped CRUD tools (`manageProfiles`, `manageSettings`, `manageLists`, `manageRewrites`, `manageLogs`, `queryAnalytics`, `plotAnalytics`) and the custom `dohLookup` tool.

## Optional plot profile

If the user provides a profile with analytics data below, use it for plotting and time-series analytics. Otherwise, attempt those checks against the test profile and skip them if no data is available.

- **Plot profile ID**: `<PLOT_PROFILE_ID>`

## Constraints

- Do not modify code or server configuration.
- Create a new test profile named `AI E2E Test Profile [timestamp]` for this run. (`[timestamp]` can be a number; do not include literal brackets.)
- Perform all write operations against the test profile only.
- Never delete or modify real user profiles.
- If a call fails, record the failure with full error details and continue.
- Clean up by deleting the test profile you created.

## Preflight

1. Verify you can access the MCP tools.
2. Create a SQL table `tool_calls (tool_name TEXT, status TEXT, notes TEXT)` before making any tool calls.

## Operation checklist

For every item below, make at least one tool call and record the result. Skip an item only if the server explicitly reports that the operation is unsupported for the target resource.

### Profile management
- List all profiles.
- Create the test profile.
- Get the test profile.
- Update the test profile's name.
- Delete the test profile during cleanup.

### Settings
For each category — `general`, `privacy`, `security`, `parental`, `performance`, `logs`, `blockpage` — perform both of the following:
- Read the current settings.
- Update at least one field in the category.

### Lists
For each list type — `allowlist`, `denylist`, `privacy_blocklists`, `privacy_natives`, `security_tlds`, `parental_categories`, `parental_services` — perform all supported operations:
- Read the current list.
- Replace the entire list with one or more test entries.
- Update an entry, if the list type supports per-entry updates.
- Remove an entry.
- Add an entry.
- Remove the entry you just added.

### DNS rewrites
- List existing rewrites for the test profile.
- Add a test rewrite entry.
- Delete the rewrite entry you just created.

### Analytics — aggregate totals
Query aggregate totals for every metric: `status`, `domains`, `queryTypes`, `reasons`, `ips`, `dnssec`, `encryption`, `ipVersions`, `protocols`, `devices`, and `destinations` (include a `destination_type` such as `countries` for `destinations`).

### Analytics — time series
Query time-series data for every metric that supports it: `status`, `queryTypes`, `reasons`, `ips`, `dnssec`, `encryption`, `ipVersions`, `protocols`, `devices`, and `destinations` (include a `destination_type` for `destinations`). Do not request a time series for `domains`.

### DNS-over-HTTPS
- Perform a DNS lookup for a common domain through the test profile.

### Plotting
Generate a plot for every supported metric: `status`, `devices`, `protocols`, `queryTypes`, `ipVersions`, `dnssec`, `encryption`, `reasons`, `ips`. Confirm a non-empty image payload is returned, or mark the check skipped if no time-series data is available.

### Logs
Run these **after** the DoH lookup so there is time for query logs to be generated.

- Retrieve recent query logs for the test profile.
- Download retained logs for the test profile.
- Clear logs for the test profile.

**Errata:** NextDNS query logs can take up to 5 minutes to appear. If log retrieval or download returns an empty result, mark the call as `skipped` with a note about the observed delay rather than `fail`.

## Reporting

- Insert one row into `tool_calls` for every tool call you make, with the tool name, status (`pass` / `fail` / `skipped`), and a brief note.
- For failures, include the full error in the notes.
- In the final report, list every tool name exercised exactly once (deduplicated), sorted alphabetically, with its status. Derive this from `SELECT DISTINCT tool_name ...` on the SQL table. Never estimate counts from memory.
