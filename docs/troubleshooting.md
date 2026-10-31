# Troubleshooting

Fix common setup and runtime issues.

## Extra/Unknown Fields in Tool Input

**Problem:** AI clients (like OpenAI) or CLI tools may send extra/unknown fields with tool arguments that don't match the tool's input schema, causing validation errors like "Unexpected keyword argument".

**Solution:** The server uses a custom middleware (`StripExtraFieldsMiddleware`) that intercepts all tool calls and filters out unknown fields before they reach FastMCP's validation layer. This approach:

- **Silently ignores** extra fields (no errors)
- **Maintains type safety** for known/required fields
- **Works with all tools** (custom `@mcp_server.tool()` decorated tools)
- **Logs stripped fields** at DEBUG level for troubleshooting

The middleware inspects each tool's parameter schema and removes any arguments that aren't defined in the schema. For example:

```python
# Tool expects: {"domain": str, "record_type": str}
# Client sends: {"domain": "example.com", "record_type": "A", "extra": "ignored", "schema_hint": "..."}
# Middleware filters to: {"domain": "example.com", "record_type": "A"}
```

See `src/nextdns_mcp/openapi.py` for implementation details (class `StripExtraFieldsMiddleware`).
The middleware only filters top-level tool arguments; fields nested inside an argument value are not checked.

## "NEXTDNS_API_KEY is required" or 401 errors
- Set `NEXTDNS_API_KEY` in your environment.
- Alternatively set `NEXTDNS_API_KEY_FILE` to a readable file containing only the key.

## 403: Read/Write access denied
- Check `NEXTDNS_READ_ONLY` (true disables all writes).
- Verify `NEXTDNS_READABLE_PROFILES`/`NEXTDNS_WRITABLE_PROFILES` (unset denies all; use `ALL` to allow all).
- Ensure the `profile_id` you call is permitted.
- If the message is `Blocked request to non-NextDNS host: <host>`, the request targeted a host outside the client's destination allow-list and was refused before any request was sent. See [safety.md](safety.md).

## Log download refused
- If `downloadLogs` returns `Refusing log download redirect to a non-public or non-https destination`, the download endpoint pointed its redirect at a destination that is not https on a globally routable host, and the redirect was refused before that target was contacted. See [safety.md](safety.md).

## Invalid JSON or array expected
- Bulk tools take a JSON array of entry objects, e.g. `'[{"id": "ads.example.com"}, {"id": "tracker.net"}]'`.
- Use single quotes to avoid escaping inner quotes in shells.
- The array is validated against the endpoint's published schema before any request goes out: it must be an array of
  objects with a string `id`, and a malformed value is rejected by argument validation without reaching the API.

## Network/DNS issues
- Ensure outbound HTTPS to `api.nextdns.io` and `dns.nextdns.io`.
- Increase `NEXTDNS_HTTP_TIMEOUT` if needed, then restart the server process: the timeout is read when the HTTP clients are built, so an in-place change has no effect (see [configuration.md](configuration.md)).

## No default profile
- `dohLookup` and `plotAnalytics` accept an optional `profile_id`; if omitted, set `NEXTDNS_DEFAULT_PROFILE` or pass `profile_id` explicitly.
- Every other profile-scoped tool — all the generated operation tools (`getProfile`, `updateDenylistEntry`, `getAnalyticsStatus`, …) — requires `profile_id` and never falls back to `NEXTDNS_DEFAULT_PROFILE`; only `dohLookup` and `plotAnalytics` use the default. They report the two failure modes with distinct codes: an omitted (or blank) `profile_id` returns `missing_profile_id`, while a malformed one returns `invalid_profile_id` with the rejected value quoted back.
