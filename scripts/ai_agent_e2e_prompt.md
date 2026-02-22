# AI Agent MCP E2E Prompt

Use this prompt with your AI agent to validate the NextDNS MCP server (tools are attached directly to the agent).

---

You are an AI agent validating the NextDNS MCP server via direct MCP tool access.
Follow these instructions exactly, report all results, and do not skip steps.

## Goals
1) Verify read-only tools work.
2) If `ALLOW_LIVE_WRITES=true`, verify write tools work using a dedicated test profile.
3) Record any failures with full command output.

## Constraints
- Use the MCP tools.
- Do not modify code.
- Use the `profile_id` provided or create a new one named `AI E2E Test Profile [timestamp]`.
- Never delete real user profiles; only delete the test profile you created.

## Preflight
1) Verify authentication and tool access:
   - If `authRequired`, stop and report.

## Profile setup
2) If `MCP_PROFILE_ID` is provided, use it.
3) If not provided and `ALLOW_LIVE_WRITES=true`:
   - Create a profile named `"AI E2E Test Profile [timestamp]"`.
   - Extract `profile_id` from the response.
   - If ALLOW_LIVE_WRITES is not specified, assume it is true.
4) If `ALLOW_LIVE_WRITES=false`:
   - Use the first profile from `listProfiles`.

## Read-only checks (always run)
- Verify the profile is retrievable and returns valid metadata for `PROFILE_ID`.
- Verify general settings can be retrieved for `PROFILE_ID`.
- Verify content lists can be retrieved (allowlist, denylist) for `PROFILE_ID`.
- Verify privacy lists can be retrieved (blocklists, native tracking) for `PROFILE_ID`.
- Verify security lists can be retrieved (TLDs) for `PROFILE_ID`.
- Verify parental control lists and settings are retrievable (categories, services, settings) for `PROFILE_ID`.
- Verify logs, block page, and performance settings are retrievable for `PROFILE_ID`.

## Write checks (only if `ALLOW_LIVE_WRITES=true`)
- Verify allowlist write operations by adding an entry and replacing the list.
- Verify denylist write operations by adding an entry and replacing the list.
- Verify privacy blocklists and native tracking writes by adding entries and replacing the lists.
- Verify security TLD writes by adding an entry and replacing the list.
- Verify parental control writes by adding a category/service and replacing the lists.

## Guardrails for tool usage
- Use MCP tools directly with named arguments (e.g., `profile_id=...`).
- For list replacement operations, pass array bodies as JSON objects: `body=[{"id":"value"}]`.
- Prefer task completion over tool enumeration, but ensure each task uses the correct tool(s) to validate behavior.

## Task-level execution plan (mirrors `run_all_tools.sh`)
Perform the following tasks in order. Each task should invoke the specific tool(s) required to validate behavior. Ensure **all tools** are exercised at least once in this flow.

### 1) Preflight and discovery
- Confirm the tool registry is reachable and enumerate available tools.
- Confirm authentication by listing profiles.

### 2) Profile management
- If writes are enabled, create a dedicated test profile and track its `profile_id`.
- If writes are disabled, select an existing profile for read-only checks.
- Validate `getProfile` and `updateProfile` (writes only) using the selected profile.

### 3) DNS testing
- Perform a DoH lookup for a known domain and valid record type using the selected profile.

### 4) Settings validation
- Retrieve general settings, logs settings, block page settings, performance settings.
- If writes are enabled, update each of the settings groups with minimal valid changes.

### 5) Logs validation
- Retrieve logs and download logs for a recent time window (use `from` and `limit`).
- If writes are enabled, clear logs.

### 6) Analytics validation (base + time-series)
- Query all analytics base endpoints for a recent time window.
- Query all analytics time-series endpoints for a recent time window.
- Use a valid destination `type` (e.g., `countries`) where required.

### 7) Content lists — allowlist & denylist
- Retrieve allowlist and denylist.
- If writes are enabled: add an entry, update an entry, remove an entry, and replace the entire list for each list.

### 8) Security lists and settings
- Retrieve security settings and TLDs.
- If writes are enabled: update security settings; add, remove, and replace security TLDs.

### 9) Privacy lists and settings
- Retrieve privacy settings, blocklists, and native tracking lists.
- If writes are enabled: update privacy settings; add, remove, and replace blocklists and native tracking lists.

### 10) Parental control lists and settings
- Retrieve parental control settings, services, and categories.
- If writes are enabled: update parental control settings; add, update, remove, and replace services and categories.

## Cleanup
- If you created the profile in this run, delete it.

## Reporting
- For each tool call, record: tool name, exit code, and JSON output.
- If a call fails, include full error details in the report.

