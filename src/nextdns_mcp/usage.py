"""MCP prompt usage guide for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""


def nextdns_usage_guide() -> str:
    """Return a detailed usage guide for the NextDNS MCP tools."""
    return """# NextDNS MCP Server Usage Guide

This MCP server exposes NextDNS through a small set of grouped tools. Each tool
maps to a functional area of the NextDNS API.

## Core concepts

- **Profile**: A named NextDNS configuration that owns settings, lists, logs,
  analytics, and rewrites. Most tools require a `profile_id`.
- **Default profile**: If `NEXTDNS_DEFAULT_PROFILE` is set, tools that accept an
  optional `profile_id` will use it automatically.
- **Access control**: The server reads `NEXTDNS_READABLE_PROFILES` and
  `NEXTDNS_WRITABLE_PROFILES`. Reads/writes outside those profiles are rejected.

## Available tools

### manageProfiles
List, create, get, update, or delete profiles. Use this first to discover the
`profile_id` you need for other tools.

- `operation="list"`
- `operation="create" name="My Profile"`
- `operation="get" profile_id="abc123"`
- `operation="update" profile_id="abc123" name="New Name"`
- `operation="delete" profile_id="abc123"`

### manageSettings
Get or update one of the seven settings categories:

- `general` — core profile options (e.g., web3 blocking)
- `privacy` — disguised trackers, affiliate links
- `security` — threat intelligence, Google Safe Browsing
- `parental` — safe search, YouTube restricted mode
- `performance` — ECS, cache boost
- `logs` — logging enablement and retention
- `blockpage` — custom block page toggle

When updating, first call `get` to inspect the current schema, then pass only the
fields you want to change in `settings`.

### manageLists
Manage allow/deny/block lists:

- `allowlist` / `denylist` — per-domain overrides
- `privacy_blocklists` — subscribed blocklists
- `privacy_natives` — native tracking blockers
- `security_tlds` — dangerous TLDs
- `parental_categories` — content categories
- `parental_services` — specific apps/services

Operations: `get`, `add`, `remove`, `update`, `replace`.

For `add`, pass `entry={"id": "value"}`. For `remove`/`update`, pass
`entry_id`. For `replace`, pass `entries=[{"id": "value"}, ...]`.

### manageRewrites
Create custom DNS responses for a hostname:

- `operation="list"`
- `operation="add" name="router.home" content="192.168.1.1"`
- `operation="delete" entry_id="router.home"`

### manageLogs
Inspect or export query logs:

- `operation="get"` — recent entries (set `raw=true` for unfiltered logs)
- `operation="clear"` — delete stored logs
- `operation="download"` — CSV export

Time values can be Unix timestamps or relative strings such as `-1d`.

### queryAnalytics
Fetch analytics for a profile. Metrics:

- `status`, `devices`, `protocols`, `queryTypes`, `ipVersions`, `dnssec`,
  `encryption`, `reasons`, `ips`, `destinations`

Set `series=true` for time-series data. The `destinations` metric requires
`destination_type` (e.g., `countries` or `gafam`).

### plotAnalytics
Generate a PNG line chart for supported metrics. Use a profile with query
history. Returns an MCP image or an error if no data is available.

### dohLookup
Perform a DNS-over-HTTPS lookup through NextDNS:

- `dohLookup(domain="example.com", profile_id="abc123", record_type="A")`

## Common workflows

### Block a domain
1. `manageLists(list_type="denylist", operation="add", profile_id="abc123", entry={"id": "bad.example.com"})`

### Allow a domain
1. `manageLists(list_type="allowlist", operation="add", profile_id="abc123", entry={"id": "safe.example.com"})`

### View blocked query trends
1. `queryAnalytics(metric="status", profile_id="abc123", from_time="-1d", series=true)`
2. `plotAnalytics(metric="status", profile_id="abc123", from_time="-1d")`

### Add a DNS rewrite
1. `manageRewrites(operation="add", profile_id="abc123", name="router.home", content="192.168.1.1")`
"""
