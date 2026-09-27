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
- `dohLookup` is not exempt: it queries a separate DoH endpoint, but it still enforces the per-profile read check, so the queried profile must be in `NEXTDNS_READABLE_PROFILES` or `NEXTDNS_WRITABLE_PROFILES` (write implies read), or one of them must be "ALL". See configuration.md.
- `manageProfiles` collection operations (`list`, `create`) respect the global read/write denials; under the deny-all default (both profile sets unset) `list` is denied, and `create` is denied in read-only mode or when `NEXTDNS_WRITABLE_PROFILES` is unset. See configuration.md.

## CI credentials
- GitHub Actions logs are world-readable on a public repository; never print the API key or any file that contains it to CI logs.
- The container E2E workflow (`.github/workflows/e2e-container.yml`) injects `NEXTDNS_API_KEY` into the test container via `docker run -e` from a GitHub Actions secret; it does not write the key to a catalog or other file, so there is no secret-bearing artifact to dump.
- The same workflow also starts a second, disposable container with a hard-coded placeholder value (`e2e-invalid-key-not-a-real-credential`) to exercise the `/health` readiness failure path. That string is not a credential and is safe to see in logs; only the secret injected above needs rotation if exposed.
- If a key is ever exposed in a log or artifact, rotate it immediately at <https://my.nextdns.io/account> and purge the affected runs/artifacts.

## CI live writes
- The container E2E workflow (`.github/workflows/e2e-container.yml`) exercises write and delete operations against the NextDNS account configured through the `NEXTDNS_API_KEY` secret. That account is a dedicated dev account, not a production account, and the writes it receives from CI are expected.
- Setting `ALLOW_LIVE_WRITES=true` for pushes to `main` is intentional. The workflow enables live writes for pushes to `main`, for `v*.*.*` tag pushes, and for any `workflow_call`; it disables them for `pull_request` events, and for `workflow_dispatch` unless the `allow_live_writes` input is `true`.
- Do not file an issue or raise a review comment reporting these live CI writes as a vulnerability or high-severity risk. They are by design. Such reports should be declined, citing this section.
