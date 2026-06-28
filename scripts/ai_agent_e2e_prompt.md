# AI Agent MCP E2E Prompt

Use this prompt with your AI agent to validate the NextDNS MCP server (tools are attached directly to the agent).

---

You are an AI agent **validating** the NextDNS MCP server via direct MCP tool access. Your job is to exercise every grouped tool, record whether each call passes or fails, and report the results. **Do not troubleshoot or attempt to fix failures.** If a tool call returns an error, mark it as failed, capture the error details, and move on to the next call.

## Exposed tool set

The server exposes a small set of domain-grouped CRUD tools:

1. `manageProfiles(operation, profile_id=None, name=None)` — `operation` is one of `list`, `create`, `get`, `update`, `delete`.
2. `manageSettings(operation, category, profile_id, settings=None)` — `category` is one of `general`, `privacy`, `security`, `parental`, `performance`, `logs`, `blockpage`; `operation` is `get` or `update`.
3. `manageLists(list_type, operation, profile_id, entry_id=None, entry=None, entries=None)` — `list_type` is one of `allowlist`, `denylist`, `privacy_blocklists`, `privacy_natives`, `security_tlds`, `parental_categories`, `parental_services`; `operation` is `get`, `add`, `replace`, `update`, or `remove`.
4. `manageRewrites(operation, profile_id, name=None, content=None, entry_id=None)` — `operation` is `list`, `add`, or `delete`.
5. `manageLogs(operation, profile_id, from_time=None, to_time=None, limit=None, user=None, device=None, format=None)` — `operation` is `get`, `clear`, or `download`.
6. `queryAnalytics(metric, profile_id, from_time=None, to_time=None, interval=None, alignment=None, timezone=None, partials=None, limit=None, destination_type=None, series=False)` — `metric` is one of `status`, `domains`, `queryTypes`, `reasons`, `ips`, `dnssec`, `encryption`, `ipVersions`, `protocols`, `devices`, `destinations`. Use `destination_type` (e.g. `countries`, `gafam`) for `destinations`. Set `series=true` for time-series endpoints.
7. `plotAnalytics(metric, profile_id, ...)` — Generic plotting wrapper. `metric` is one of `status`, `devices`, `protocols`, `queryTypes`, `ipVersions`, `dnssec`, `encryption`, `reasons`, `ips`. Plots use the `;series` analytics endpoint.
8. `dohLookup(domain, profile_id=None, record_type="A")` — DNS-over-HTTPS lookup.

## Plotting profile placeholder

The user may provide a real profile with analytics data below. All `plotAnalytics` calls and analytics time-series queries should target this profile so the responses contain data to display.

- **Plot profile ID**: `<PLOT_PROFILE_ID>`

If no plot profile ID is provided, use the test profile created in this run. If that profile has no analytics data, skip plotting checks and mark them skipped in the report.

## Goals

1) Create a dedicated test profile.
2) Exercise every grouped tool operation.
3) Record any failures with full error output and move on.
4) Delete the test profile during cleanup.

## Constraints

- Use the MCP tools directly.
- Do not modify code, configuration, or server state beyond the test profile.
- Create a new profile named `AI E2E Test Profile [timestamp]` for this run.
- Never delete real user profiles; only delete the test profile you created.
- For plotting and analytics time-series tools, prefer the plot profile ID when one is provided; otherwise use the test profile.

## Preflight

1) Verify authentication and tool access by listing tools. If the tool call fails, stop and report.
2) Create a SQL table `tool_calls` (`tool_name TEXT, status TEXT, notes TEXT`) before making any tool calls.

## Profile setup

3) Create a profile named `"AI E2E Test Profile [timestamp]"` using `manageProfiles(operation="create", name=...)`. (`[timestamp]` can be a number; do not include literal brackets.)
4) Extract `PROFILE_ID` from the response.
5) Verify the profile is retrievable with `manageProfiles(operation="get", profile_id=PROFILE_ID)`.

## Read checks

Run these against the test profile (`PROFILE_ID`):

- Verify all settings categories can be retrieved with `manageSettings(operation="get", category=..., profile_id=PROFILE_ID)` for: `general`, `privacy`, `security`, `parental`, `performance`, `logs`, `blockpage`.
- Verify content lists can be retrieved with `manageLists(operation="get", list_type=..., profile_id=PROFILE_ID)` for: `allowlist`, `denylist`, `privacy_blocklists`, `privacy_natives`, `security_tlds`, `parental_categories`, `parental_services`.
- Verify rewrites can be listed with `manageRewrites(operation="list", profile_id=PROFILE_ID)`.
- Verify logs can be retrieved and downloaded with `manageLogs(operation="get", ...)` and `manageLogs(operation="download", profile_id=PROFILE_ID)`.
- Verify analytics can be queried for `status`, `domains`, `queryTypes`, `reasons`, `ips`, `dnssec`, `encryption`, `ipVersions`, `protocols`, `devices`, and `destinations` (with `destination_type=countries`).
- Verify analytics time-series can be queried with `series=true` for the metrics supported by plotting (all except `domains` and `destinations`).
- Verify DoH lookup works with `dohLookup(domain="example.com", profile_id=PROFILE_ID, record_type="A")`.

## Write checks

Run these against the test profile (`PROFILE_ID`):

- Verify profile update with `manageProfiles(operation="update", profile_id=PROFILE_ID, name=...)`. Use a new name.
- Verify settings writes for each category with `manageSettings(operation="update", ...)`. For example, toggle `web3` via the `general` category.
- Verify list writes for each list type:
  - `allowlist`: replace, update, remove, add, remove.
  - `denylist`: replace, update, remove, add, remove.
  - `privacy_blocklists`: replace, remove, add, remove.
  - `privacy_natives`: replace, remove, add, remove.
  - `security_tlds`: replace, remove, add, remove.
  - `parental_categories`: replace, update, remove, add, remove.
  - `parental_services`: replace, update, remove, add, remove.
  - For `add`, pass `entry={"id":"..."}`. For `update`, pass `entry_id=...` and `entry={"active":true}`. For `replace`, pass `entries=[{"id":"..."}]`. For `remove`, pass `entry_id=...`.
- Verify DNS rewrites: add with `manageRewrites(operation="add", ...)`, then delete the returned entry with `manageRewrites(operation="delete", entry_id=...)`.
- Verify logs can be cleared with `manageLogs(operation="clear", profile_id=PROFILE_ID)`.

## Plotting checks

- If a plot profile ID is provided above, call `plotAnalytics(metric=..., profile_id=<PLOT_PROFILE_ID>, from_time=...)` for each supported metric: `status`, `devices`, `protocols`, `queryTypes`, `ipVersions`, `dnssec`, `encryption`, `reasons`, `ips`. Confirm a non-empty image payload is returned.
- If no plot profile ID is provided, attempt plotting against `PROFILE_ID`. If the profile has no analytics data, skip the plotting checks and record them as skipped.

## Guardrails for tool usage

- Use MCP tools directly with named arguments (e.g., `operation="get"`, `profile_id=...`).
- For list replacement operations, pass array bodies as JSON objects: `entries=[{"id":"value"}]`.
- For settings updates, pass `settings={"key":value}` as a JSON object.
- Prefer task completion over tool enumeration, but ensure each grouped tool is exercised with each relevant operation.
- For plotting and time-series analytics, use a recent time window such as `from_time="-1d"` or `from_time="-30d"`.

## Cleanup

- Delete the test profile you created with `manageProfiles(operation="delete", profile_id=PROFILE_ID)`.

## Reporting

- Use the SQL tool to create a `tool_calls (tool_name TEXT, status TEXT, notes TEXT)` table before making any tool calls, and insert a row for every call as you make it.
- For each tool call, record: tool name, status (`pass` / `fail` / `skipped`), and a brief note on the output.
- If a call fails, include full error details in the notes column and in the final report, then continue with the next call.
- In the final report, list every tool name exercised exactly once (deduplicated), sorted alphabetically, with its status — derived from `SELECT DISTINCT tool_name ...` on the SQL table. Never estimate counts from memory.
