# Usage and Examples

How to list tools and call them from Docker MCP Gateway or other MCP clients.

## List tools
```bash
docker mcp tools ls
```

## Call tools (flag style)

DoH lookup
```bash
# Test resolution of a domain (A record)
docker mcp tools call dohLookup '{"domain":"google.com","record_type":"A","profile_id":"YOUR_PROFILE_ID"}'

# IPv6 (AAAA)
docker mcp tools call dohLookup '{"domain":"google.com","record_type":"AAAA","profile_id":"YOUR_PROFILE_ID"}'
```

Get profile settings
```bash
docker mcp tools call getSettings '{"profile_id":"abc123"}'
```

Bulk updates that take JSON array strings
```bash
# Denylist
# Bash/Zsh
docker mcp tools call updateDenylist '{"profile_id":"abc123","entries":"[\"ads.example.com\",\"tracker.net\"]"}'
# PowerShell (use single quotes to avoid escaping quotes)
docker mcp tools call updateDenylist '{"profile_id":"abc123","entries":"[\"ads.example.com\",\"tracker.net\"]"}'

# Blocked TLDs
docker mcp tools call updateSecurityTlds '{"profile_id":"abc123","tlds":"[\"zip\",\"mov\"]"}'
```

## Windows vs POSIX quoting
- docker mcp tools call expects a single JSON string argument for params.
- Use single quotes on Bash/Zsh; on PowerShell use double quotes with escaped inner quotes.
- For complex payloads, build the JSON in a variable or load from a file.

## Tips
- Set NEXTDNS_DEFAULT_PROFILE to omit profile_id for many tools.
- Use read-only mode when exploring: set NEXTDNS_READ_ONLY=true.
