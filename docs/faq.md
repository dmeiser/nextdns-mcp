# FAQ

## How do I find my profile_id?
Run the `manageProfiles` tool with `operation="list"` (or check your NextDNS dashboard URL: `https://my.nextdns.io/<profile_id>`) and copy the `id` of the desired profile.

## Can I use the tools without specifying profile_id?
Set `NEXTDNS_DEFAULT_PROFILE` to a profile ID; tools that accept `profile_id` will use it when omitted.

## How do I disable all write operations?
Set `NEXTDNS_READ_ONLY=true`.

## Which tools work without per-profile access?
`dohLookup` does not bypass per-profile checks: it queries a separate DoH endpoint, but the queried profile must still be readable (`NEXTDNS_READABLE_PROFILES`, or `NEXTDNS_WRITABLE_PROFILES` since write implies read). `manageProfiles(operation="list")` and `create` carry no `profile_id` but still respect the global denials: `list` requires at least one readable profile, and `create` requires write permission (see configuration.md).

## How do I check that the server can actually reach NextDNS?
When running with `MCP_TRANSPORT=http`, send a `GET` request to `/health`. It is a readiness check, not a constant: it probes the NextDNS API with your configured API key and returns `200 OK` with `{"status": "ok"}` only when that probe succeeds. A failing probe returns `503` with `{"status": "error", "class": "auth"|"unreachable", "reason": "..."}`, where `auth` means NextDNS rejected your credentials and `unreachable` means the API could not be probed or answered unusably. The probe allows 5 seconds, ignores profile access control (so a local read denial does not look like an auth failure), and its result is cached for 30 seconds. (There is no HTTP surface in the default stdio transport.)

## Where do I get logs and analytics?
Use `manageLogs` and `queryAnalytics`; real-time log streaming (SSE) is not exposed as a tool. `manageLogs(operation="download")` provides CSV export of retained logs.
