# FAQ

## How do I find my profile_id?
Run the `manageProfiles` tool with `operation="list"` (or check your NextDNS dashboard URL: `https://my.nextdns.io/<profile_id>`) and copy the `id` of the desired profile.

## Can I use the tools without specifying profile_id?
Set `NEXTDNS_DEFAULT_PROFILE` to a profile ID; tools that accept `profile_id` will use it when omitted.

## How do I disable all write operations?
Set `NEXTDNS_READ_ONLY=true`.

## Which tools work without per-profile access?
None bypass per-profile checks entirely. `dohLookup` uses a separate DoH endpoint but still enforces `can_read_profile` on the target profile. `manageProfiles(operation="list")` and `create` carry no `profile_id` but still respect the global denials: `list` requires at least one readable profile, and `create` requires write permission (see configuration.md).

## How do I check that the HTTP server is up?
When running with `MCP_TRANSPORT=http`, send a `GET` request to `/health`; it returns `200 OK` with `{"status": "ok"}`. (There is no HTTP surface in the default stdio transport.)

## Where do I get logs and analytics?
Use `manageLogs` and `queryAnalytics`; real-time log streaming (SSE) is not exposed as a tool. `manageLogs(operation="download")` provides CSV export of retained logs.
