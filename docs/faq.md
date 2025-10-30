# FAQ

## How do I find my profile_id?
Run `docker mcp tools call listProfiles '{}'` and copy the `id` of the desired profile.

## Can I use the tools without specifying profile_id?
Set `NEXTDNS_DEFAULT_PROFILE` to a profile ID; tools that accept `profile_id` will use it when omitted.

## How do I disable all write operations?
Set `NEXTDNS_READ_ONLY=true`.

## Which tools work without per-profile access?
`listProfiles` and `dohLookup` are globally allowed and bypass per-profile checks.

## Where do I get logs and analytics?
Use the `getLogs`, `downloadLogs`, and the `getAnalytics*` tools; real-time log streaming (SSE) is not supported.
