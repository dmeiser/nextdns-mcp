# FAQ

## How do I find my profile_id?
Run the `manageProfiles` tool with `operation="list"` (or check your NextDNS dashboard URL: `https://my.nextdns.io/<profile_id>`) and copy the `id` of the desired profile.

## Can I use the tools without specifying profile_id?
Set `NEXTDNS_DEFAULT_PROFILE` to a profile ID; tools that accept `profile_id` will use it when omitted.

## How do I disable all write operations?
Set `NEXTDNS_READ_ONLY=true`.

## Which tools work without per-profile access?
Only `dohLookup` bypasses per-profile checks. `manageProfiles(operation="list")` and `create` carry no `profile_id` but still respect the global denials: `list` requires at least one readable profile, and `create` requires write permission (see configuration.md).

## Where do I get logs and analytics?
Use `manageLogs` and `queryAnalytics`; real-time log streaming (SSE) is not exposed as a tool. `manageLogs(operation="download")` provides CSV export of retained logs.
