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
- `manageProfiles(operation="list")` and `dohLookup` bypass per-profile access checks; they are safe to use for discovery and testing.

## CI credentials
- GitHub Actions logs are world-readable on a public repository; never print the API key or any file that contains it to CI logs.
- The container E2E workflow (`.github/workflows/e2e-container.yml`) injects `NEXTDNS_API_KEY` into the test container via `docker run -e` from a GitHub Actions secret; it does not write the key to a catalog or other file, so there is no secret-bearing artifact to dump.
- If a key is ever exposed in a log or artifact, rotate it immediately at <https://my.nextdns.io/account> and purge the affected runs/artifacts.
