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
- Only `dohLookup` bypasses per-profile access checks; it is safe to use for discovery and testing.
- `manageProfiles` collection operations (`list`, `create`) respect the global read/write denials; under the deny-all default (both profile sets unset) `list` is denied, and `create` is denied in read-only mode or when `NEXTDNS_WRITABLE_PROFILES` is unset. See configuration.md.

## CI credentials
- GitHub Actions logs are world-readable on a public repository; never print the API key or any file that contains it to CI logs.
- The container E2E workflow (`.github/workflows/e2e-container.yml`) injects `NEXTDNS_API_KEY` into the test container via `docker run -e` from a GitHub Actions secret; it does not write the key to a catalog or other file, so there is no secret-bearing artifact to dump.
- If a key is ever exposed in a log or artifact, rotate it immediately at <https://my.nextdns.io/account> and purge the affected runs/artifacts.
