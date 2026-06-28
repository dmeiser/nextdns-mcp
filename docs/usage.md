# Usage and Examples

How to list tools and call them from Docker MCP Gateway or other MCP clients.

## List tools
```bash
docker mcp tools ls
```

## Call tools

DoH lookup
```bash
# Test resolution of a domain (A record)
docker mcp tools call dohLookup domain=google.com record_type=A profile_id=YOUR_PROFILE_ID

# IPv6 (AAAA)
docker mcp tools call dohLookup domain=google.com record_type=AAAA profile_id=YOUR_PROFILE_ID
```

Manage profiles
```bash
# List all profiles
docker mcp tools call manageProfiles operation=list

# Get a single profile
docker mcp tools call manageProfiles operation=get profile_id=abc123
```

Get profile settings
```bash
# general settings
docker mcp tools call manageSettings operation=get category=general profile_id=abc123

# privacy, security, parental, performance, logs, blockpage
docker mcp tools call manageSettings operation=get category=privacy profile_id=abc123
```

Update settings
```bash
docker mcp tools call manageSettings operation=update category=general profile_id=abc123 settings='{"web3":true}'
```

Manage lists
```bash
# Replace a whole list
docker mcp tools call manageLists list_type=denylist operation=replace profile_id=abc123 entries='[{"id":"ads.example.com"},{"id":"tracker.net"}]'

# Add or remove individual entries
docker mcp tools call manageLists list_type=denylist operation=add profile_id=abc123 entry='{"id":"ads.example.com"}'
docker mcp tools call manageLists list_type=denylist operation=remove profile_id=abc123 entry_id=ads.example.com

# Toggle an entry (only for allowlist, denylist, parental_categories, parental_services)
docker mcp tools call manageLists list_type=denylist operation=update profile_id=abc123 entry_id=ads.example.com entry='{"active":true}'

# Security TLDs
docker mcp tools call manageLists list_type=security_tlds operation=replace profile_id=abc123 entries='[{"id":"zip"},{"id":"mov"}]'
```

Query analytics
```bash
# Aggregate totals for a metric
docker mcp tools call queryAnalytics metric=status profile_id=abc123 from_time=-1d

# Time-series data
docker mcp tools call queryAnalytics metric=status profile_id=abc123 from_time=-1d series=true

# Destinations require destination_type
docker mcp tools call queryAnalytics metric=destinations profile_id=abc123 from_time=-1d destination_type=countries
```

Plot analytics
```bash
# Returns a PNG chart for supported metrics
docker mcp tools call plotAnalytics metric=status profile_id=abc123 from_time=-1d
```

## Windows vs POSIX quoting

`docker mcp tools call` accepts two argument styles:

1. **`key=value` arguments** — the style used throughout this doc and the E2E scripts.
   - Bash/Zsh: `docker mcp tools call toolName key1="value" key2=42`
   - PowerShell: `docker mcp tools call toolName key1='value' key2=42`

2. **A single JSON string argument** — legacy style still accepted by some clients.
   - Bash/Zsh: wrap the JSON in single quotes:
     ```bash
     docker mcp tools call toolName '{"key1":"value","key2":42}'
     ```
   - PowerShell: wrap the JSON in double quotes and escape inner double quotes:
     ```powershell
     docker mcp tools call toolName "{`"key1`":`"value`",`"key2`":42}"
     ```

For complex payloads, build the JSON in a variable or load from a file.

## Tips
- Set `NEXTDNS_DEFAULT_PROFILE` to omit `profile_id` for many tools.
- Use read-only mode when exploring: set `NEXTDNS_READ_ONLY=true`.
- See `scripts/ai_agent_e2e_prompt.md` for a task-level prompt that exercises every grouped tool.
