# Safety Guidelines

Reduce risk when operating on real NextDNS profiles.

## Recommendations
- Enable read-only mode while exploring: `NEXTDNS_READ_ONLY=true`.
- Limit writes to a dedicated test profile via `NEXTDNS_WRITABLE_PROFILES`.
- Always verify `profile_id` before write or delete operations.

## Destructive operations
- Profile deletion permanently removes data; confirm the ID and prefer test profiles.
- Bulk PUT endpoints replace the entire list (denylist, allowlist, etc.); export/record current values first.

## Scope of global tools
- `listProfiles` and `dohLookup` bypass per-profile access checks; they are safe to use for discovery and testing.
