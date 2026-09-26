# Usage and Examples

How to call NextDNS MCP tools from MCP clients (such as Claude Desktop, Cursor, or any MCP-compatible agent).

## Available Tools

The server exposes domain-grouped CRUD tools:
- `manageProfiles`: CRUD operations on NextDNS profiles
- `manageSettings`: Configuration for profile settings categories
- `manageLists`: Manage allowlist, denylist, blocklists, native tracking, security TLDs, and parental categories
- `manageRewrites`: Manage DNS rewrite entries
- `manageLogs`: Query log retrieval, download, and clearing
- `queryAnalytics`: Query analytics metrics (aggregate and time-series)
- `plotAnalytics`: Generate visual chart images for analytics metrics
- `dohLookup`: Perform DNS-over-HTTPS queries through a profile

## Tool Examples

### DoH Lookup (`dohLookup`)

Test DNS resolution through a profile:

```json
{
  "domain": "google.com",
  "record_type": "A",
  "profile_id": "YOUR_PROFILE_ID"
}
```

### Manage Profiles (`manageProfiles`)

List profiles:
```json
{
  "operation": "list"
}
```

Get a single profile:
```json
{
  "operation": "get",
  "profile_id": "abc123"
}
```

### Manage Settings (`manageSettings`)

Get settings for a category (`general`, `privacy`, `security`, `parental`, `performance`, `logs`, `blockpage`):
```json
{
  "operation": "get",
  "category": "general",
  "profile_id": "abc123"
}
```

Update settings:
```json
{
  "operation": "update",
  "category": "general",
  "profile_id": "abc123",
  "settings": {
    "web3": true
  }
}
```

### Manage Lists (`manageLists`)

Replace a list:
```json
{
  "operation": "replace",
  "list_type": "denylist",
  "profile_id": "abc123",
  "entries": [
    {"id": "ads.example.com"},
    {"id": "tracker.net"}
  ]
}
```

Add an entry:
```json
{
  "operation": "add",
  "list_type": "denylist",
  "profile_id": "abc123",
  "entry": {
    "id": "ads.example.com"
  }
}
```

Remove an entry:
```json
{
  "operation": "remove",
  "list_type": "denylist",
  "profile_id": "abc123",
  "entry_id": "ads.example.com"
}
```

### Query Analytics (`queryAnalytics`)

Aggregate totals for a metric:
```json
{
  "metric": "status",
  "profile_id": "abc123",
  "from_time": "-1d"
}
```

Time-series data:
```json
{
  "metric": "status",
  "profile_id": "abc123",
  "from_time": "-1d",
  "series": true
}
```

Destinations metric (requires `destination_type`):
```json
{
  "metric": "destinations",
  "profile_id": "abc123",
  "from_time": "-1d",
  "destination_type": "countries"
}
```

### Plot Analytics (`plotAnalytics`)

Returns a PNG chart for supported metrics:
```json
{
  "metric": "status",
  "profile_id": "abc123",
  "from_time": "-1d"
}
```

## Tips
- Set `NEXTDNS_DEFAULT_PROFILE` to omit `profile_id` for many tools.
- Use read-only mode when exploring: set `NEXTDNS_READ_ONLY=true`.
- See `scripts/ai_agent_e2e_prompt.md` for a task-level prompt that exercises every grouped tool.
