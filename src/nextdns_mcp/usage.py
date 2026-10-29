"""MCP prompt usage guide for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

import textwrap

from .tools.metrics import ANALYTICS_METRICS


def _analytics_metric_bullets() -> str:
    """Render the queryAnalytics metric list from the shared metric definition.

    Derived from ``ANALYTICS_METRICS`` — the same literal FastMCP turns into the
    tool's input schema — so the guide cannot drift from the metrics the server
    actually accepts (issue #292).
    """
    listed = ", ".join(f"`{metric}`" for metric in sorted(ANALYTICS_METRICS))
    return textwrap.fill(listed, width=72, initial_indent="- ", subsequent_indent="  ")


def server_instructions(tool_search_mode: str) -> str:
    """Short instructions sent in the MCP ``initialize`` handshake.

    This is the spec's own server-to-client channel (clients put it in the model
    context), so the server — not any test harness or agent CLI — is the one
    describing itself. It stays client-agnostic (no agent-specific wording),
    never names a real tool (under progressive disclosure the catalog is
    deliberately not advertised, so a name here would leak it), and only
    describes this server: what it exposes and what its responses look like.
    It does not instruct the model how to behave — that is not a DNS server's
    business, and a line like "do not undo your own writes" would be a test
    harness's failure mode speaking through the product.

    ``tool_search_mode`` is the resolved NEXTDNS_TOOL_SEARCH value, so the
    discovery mechanism is only described when discovery is actually required.
    """
    lines = [
        "# NextDNS MCP server",
        "",
        "This server manages a NextDNS account through a small set of grouped tools:",
        "profiles, settings, allow/deny and block lists, DNS rewrites, analytics",
        "(query and plot), query logs, and DNS-over-HTTPS lookups. Each tool takes",
        "an `operation` (get, list, add, replace, update, remove, create, delete)",
        "plus a `profile_id`; the profile tool's `list` returns the ids.",
        "",
        "- A failed call returns a JSON payload with an error `code`, plus a `hint`",
        "  whenever the server can say what to send instead.",
    ]
    if tool_search_mode and tool_search_mode != "off":
        lines += [
            "- This server does not advertise tool names. `search_tools` matches the",
            "  words of a task to the tools that fit it and returns their exact names",
            "  and schemas; `call_tool` runs one of those by that exact name.",
        ]
    return "\n".join(lines)


def nextdns_usage_guide() -> str:
    """Return a detailed usage guide for the NextDNS MCP tools."""
    return (
        """# NextDNS MCP Server Usage Guide

This MCP server exposes NextDNS through a small set of grouped tools. Each tool
maps to a functional area of the NextDNS API.

## Core concepts

- **Profile**: A named NextDNS configuration that owns settings, lists, logs,
  analytics, and rewrites. Most tools require a `profile_id`.
- **Default profile**: If `NEXTDNS_DEFAULT_PROFILE` is set, tools that accept an
  optional `profile_id` will use it automatically.
- **Access control**: The server reads `NEXTDNS_READABLE_PROFILES` and
  `NEXTDNS_WRITABLE_PROFILES`. Reads/writes outside those profiles are rejected.
- **Read-only mode**: If `NEXTDNS_READ_ONLY=true`, write operations are blocked.

## Available tools

### manageProfiles
List, create, get, update, or delete profiles. Use this first to discover the
`profile_id` you need for other tools.

- `operation="list"` (paginated: if the response includes a `cursor`, pass
  it as `cursor="..."` to fetch the next page)
- `operation="create" name="My Profile"`
- `operation="get" profile_id="<profile id>"`
- `operation="update" profile_id="<profile id>" name="New Name"`
- `operation="delete" profile_id="<profile id>"`

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
- `parental_categories` — content categories (e.g., `gambling`, `porn`)
- `parental_services` — specific apps/services (e.g., `tiktok`, `youtube`)

Operations: `get`, `add`, `remove`, `update`, `replace`.

For `add`, pass `entry={"id": "value"}`. For `remove`/`update`, pass
`entry_id`. `update` (toggle `entry={"active": True|False}`) is supported for
`allowlist`, `denylist`, `parental_categories`, and `parental_services`. For
`replace`, pass `entries=[{"id": "value"}, ...]`.

### manageRewrites
Create custom DNS responses for a hostname:

- `operation="list"`
- `operation="add" name="router.home" content="192.168.1.1"`
- `operation="delete" entry_id="<id-from-list>"` (`entry_id` is the opaque rewrite ID from `list`, not the hostname)

### manageLogs
Inspect or export query logs:

- `operation="get"` — recent entries (set `raw=true` for unfiltered logs)
- `operation="clear"` — delete stored logs
- `operation="download"` — CSV export

Time values can be Unix timestamps or relative strings such as `-1d`.

### queryAnalytics
Fetch analytics for a profile. Metrics:

"""
        + _analytics_metric_bullets()
        + """

Set `series=true` for time-series data. The `destinations` metric requires
`destination_type` (e.g., `countries` or `gafam`).

### plotAnalytics
Generate a PNG line chart for supported metrics. Use a profile with query
history. Returns an MCP image or an error if no data is available.

A series is limited server-side to 2000 points (the requested range divided by
`interval`) with `interval` between 60 and 86400 seconds. A request past either
limit is rejected with an `invalid_argument` error rather than silently adjusted.
`limit` must not exceed 500 and is rejected above that; the `;series` endpoints
consume no `limit`, so it is validated but never sent.

### dohLookup
Perform a DNS-over-HTTPS lookup through NextDNS:

- `dohLookup(domain="example.com", profile_id="<profile id>", record_type="A")`

## Common workflows

### Block a domain
1. `manageLists(list_type="denylist", operation="add", profile_id="<profile id>", entry={"id": "bad.example.com"})`

### Allow a domain
1. `manageLists(list_type="allowlist", operation="add", profile_id="<profile id>", entry={"id": "safe.example.com"})`

### Block a parental control category
1. `manageLists(list_type="parental_categories", operation="add", profile_id="<profile id>", entry={"id": "gambling"})`

### Block or toggle a service in parental control
1. `manageLists(list_type="parental_services", operation="add", profile_id="<profile id>", entry={"id": "tiktok"})`
2. `manageLists(list_type="parental_services", operation="update", profile_id="<profile id>", entry_id="tiktok", entry={"active": False})`

### View blocked query trends
1. `queryAnalytics(metric="status", profile_id="<profile id>", from_time="-1d", series=true)`
2. `plotAnalytics(metric="status", profile_id="<profile id>", from_time="-1d")`

### Add a DNS rewrite
1. `manageRewrites(operation="add", profile_id="<profile id>", name="router.home", content="192.168.1.1")`

### Delete a DNS rewrite
1. `manageRewrites(operation="list", profile_id="<profile id>")` — find the entry's opaque `id`
2. `manageRewrites(operation="delete", profile_id="<profile id>", entry_id="<id-from-list>")`
"""
    )
