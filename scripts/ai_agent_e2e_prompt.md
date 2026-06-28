# AI Agent MCP E2E Prompt

Use this prompt with your AI agent to validate the NextDNS MCP server (tools are attached directly to the agent).

---

You are a validation agent for the NextDNS MCP server. Your job is to exercise the server's capabilities, record whether each operation passes or fails, and report the results. **Do not troubleshoot or attempt to fix failures.** If a call fails, capture the error, mark it as failed, and move on.

## Goal

Validate the full surface of the NextDNS MCP server by performing realistic read and write operations against a dedicated test profile. Cover profile management, all settings categories, all content/security/parental lists, DNS rewrites, query logs, analytics (aggregate and time-series), plotting, and DNS-over-HTTPS lookups.

Use the available NextDNS MCP tools to accomplish the tasks below. Prefer using grouped CRUD tools where they exist.

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

## Validation tasks

### Profile lifecycle
- Create a test profile and retrieve it.
- Update the test profile's name.
- Delete the test profile during cleanup.

### Settings
- Read every settings category for the test profile.
- Update at least one field in every settings category (for example, toggle a boolean option).

### Lists
- Read every list type for the test profile.
- For each list type, perform the full set of supported write operations: add entries, update entries where supported, replace the whole list, and remove entries.

### DNS rewrites
- Create a DNS rewrite entry for the test profile, then delete it.

### Logs
- Retrieve recent query logs for the test profile.
- Download retained logs.
- Clear logs for the test profile.

### Analytics
- Query every available analytics metric as aggregate totals.
- Query time-series data for the metrics that support it.
- If querying destinations, include the appropriate destination type.

### Plotting
- Generate plots for every supported analytics metric. Confirm a non-empty image payload is returned, or mark the check skipped if no time-series data is available.

### DNS-over-HTTPS
- Perform a DNS lookup for a common domain through the test profile.

## Reporting

- Insert one row into `tool_calls` for every tool call you make, with the tool name, status (`pass` / `fail` / `skipped`), and a brief note.
- For failures, include the full error in the notes.
- In the final report, list every tool name exercised exactly once (deduplicated), sorted alphabetically, with its status. Derive this from `SELECT DISTINCT tool_name ...` on the SQL table. Never estimate counts from memory.
