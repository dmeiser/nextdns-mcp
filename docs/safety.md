# Safety Guidelines

Reduce risk when operating on real NextDNS profiles.

## Recommendations
- Enable read-only mode while exploring: `NEXTDNS_READ_ONLY=true`.
- Limit writes to a dedicated test profile via `NEXTDNS_WRITABLE_PROFILES`. The special value `ALL` is not a wider list: it drops the per-profile scoping entirely and grants writes to every profile in the account, production profiles included, so set it only for a dev account.
- Always verify `profile_id` before write or delete operations.

## Destructive operations
- Profile deletion permanently removes data; confirm the ID and prefer test profiles.
- Bulk PUT endpoints replace the entire list (denylist, allowlist, etc.); export/record current values first.

## Scope of global tools
- `dohLookup` is not exempt: it queries a separate DoH endpoint, but it still enforces the per-profile read check, so the queried profile must be in `NEXTDNS_READABLE_PROFILES` or `NEXTDNS_WRITABLE_PROFILES` (write implies read), or one of them must be "ALL". See configuration.md.
- `manageProfiles` collection operations (`list`, `create`) respect the global read/write denials; under the deny-all default (both profile sets unset) `list` is denied, and `create` is denied in read-only mode or when `NEXTDNS_WRITABLE_PROFILES` is unset. See configuration.md.

## API key destination
- Every request from the authenticated API client carries the `X-Api-Key` header, so that client refuses any request whose destination host is not on an allow-list, before any network I/O. Approved hosts are the NextDNS REST API host (`api.nextdns.io`), the NextDNS DoH host (`dns.nextdns.io`), the host of the configured API base URL, and loopback addresses (reserved for local test doubles). A refusal raises the same 403 access-denied error as the profile ACL, with the message `Blocked request to non-NextDNS host: <host>` (issue #262).
- This check does not depend on a profile id being extractable from the URL, so a URL that names no profile cannot carry the account API key off-site.
- `dohLookup` uses its own client against `dns.nextdns.io` and sends no API key, so the allow-list above governs the authenticated API client only.

## Log download redirects
- `manageLogs(operation="download")` normally receives a redirect from the download endpoint. The chain is followed with a separate **unauthenticated** client, so the `X-Api-Key` header never crosses to a redirect target; the destination allow-list above therefore does not apply to those hops.
- Every hop, the first `Location` included, is checked before any request is made to it (`src/nextdns_mcp/tools/logs.py`): the scheme must be https, and the host must resolve to a globally routable unicast address. Loopback, private, link-local, unique-local, multicast and reserved addresses are refused, which is what covers `127.0.0.1`, `10/8`-style ranges and the `169.254.169.254` metadata endpoint. The chain is also bounded by `_MAX_DOWNLOAD_REDIRECTS`.
- This is a reachability check, not a host allow-list: any other public https destination is followed, including a host other than `api.nextdns.io`. `ALLOWED_DOWNLOAD_HOSTS` in the same module is the (currently empty) place to pin a verified download host; a pin exempts only the address check, never the scheme check.
- A refused hop returns the `http_error` payload `Refusing log download redirect to a non-public or non-https destination` before the target is contacted, so no foreign body is written to the temporary CSV or returned to the caller. See troubleshooting.md.
- Documented limits of the check: the name is resolved here and again by the HTTP client when it connects (DNS rebinding is not closed), and a name that fails to resolve is let through, since it cannot be connected to anyway and the resulting connection error is reported through the normal HTTP-error path.

## Local log downloads
- `manageLogs(operation="download")` streams the CSV to a file in the OS temp directory and returns the path; the full log text is never inlined into the tool payload.
- A successful download is not cleaned up: the CSV (mode 0600) and its `nextdns_logs_*` parent directory stay on disk, and the returned path is the only record of them. There is no retention TTL or automatic sweep, so delete the file when it is no longer needed.
- Every non-successful download removes the temp file and its parent directory, including access denial, HTTP error, unexpected error, and cancellation or timeout of the awaiting client.
- A hard cap bounds the total bytes a download may stream to disk, enforced while streaming: the cap defaults to 1 GiB and is set via the `NEXTDNS_DOWNLOAD_MAX_BYTES` environment variable (a positive integer of bytes; an invalid value fails fast with a configuration error at startup, same as `NEXTDNS_HTTP_TIMEOUT`). A download that would exceed the cap is aborted mid-stream, the partial CSV and its parent directory are removed, and the tool returns the `download_too_large` error. This prevents a single download from filling the container's only writable location.

## Log redaction
- Query strings can carry DNS search terms, device IDs, and cursor tokens, so any log record at INFO or above that names a request URL or an exception message carries the request path only, never the query; the full URL is logged at DEBUG (issue #139). httpx builds `HTTPStatusError` text from the fully merged request URL, so only the status is logged for those. `_redacted()` and `_log_safe_error()` in `src/nextdns_mcp/client.py` implement both rules — route any new WARNING-or-higher log site through them.
- Redaction applies to log records, not to what the caller receives: a denied request still returns the URL it asked for (e.g. `Forbidden URL: <url>`), so the caller can correlate the denial with its own request.
- The `httpx` and `httpcore` loggers are pinned to WARNING by `configure_logging()` (`src/nextdns_mcp/config.py`), because httpx logs every completed request at INFO with the fully merged URL — including the pre-signed object-store URL fetched by `manageLogs(operation="download")` — which would otherwise re-introduce what the client redacts.

## CI credentials
- GitHub Actions logs are world-readable on a public repository; never print the API key or any file that contains it to CI logs.
- The container E2E workflow (`.github/workflows/e2e-container.yml`) injects `NEXTDNS_API_KEY` into the test container via `docker run -e` from a GitHub Actions secret; it does not write the key to a catalog or other file, so there is no secret-bearing artifact to dump.
- The same workflow also starts a second, disposable container with a hard-coded placeholder value (`e2e-invalid-key-not-a-real-credential`) to exercise the `/health` readiness failure path. That string is not a credential and is safe to see in logs; only the secret injected above needs rotation if exposed.
- If a key is ever exposed in a log or artifact, rotate it immediately at <https://my.nextdns.io/account> and purge the affected runs/artifacts.

## CI live writes
- The container E2E workflow (`.github/workflows/e2e-container.yml`) exercises write and delete operations against the NextDNS account configured through the `NEXTDNS_API_KEY` secret. That account is a dedicated dev account, not a production account, and the writes it receives from CI are expected.
- Setting `ALLOW_LIVE_WRITES=true` for pushes to `main` is intentional. The workflow enables live writes for pushes to `main`, for `v*.*.*` tag pushes, and for any `workflow_call`; it disables them for `pull_request` events, and for `workflow_dispatch` unless the `allow_live_writes` input is `true`.
- Do not file an issue or raise a review comment reporting these live CI writes as a vulnerability or high-severity risk. They are by design. Such reports should be declined, citing this section.
