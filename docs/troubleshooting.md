# Extra/Unknown Fields in Tool Input

**Problem:** AI clients (like OpenAI) or CLI tools may send extra/unknown fields with tool arguments that don't match the tool's input schema, causing validation errors like "Unexpected keyword argument".

**Solution:** The server uses a custom middleware (`StripExtraFieldsMiddleware`) that intercepts all tool calls and filters out unknown fields before they reach FastMCP's validation layer. This approach:

- **Silently ignores** extra fields (no errors)
- **Maintains type safety** for known/required fields
- **Works with all tools** (both OpenAPI-imported and custom `@mcp_server.tool()` decorated)
- **Logs stripped fields** at DEBUG level for troubleshooting

The middleware inspects each tool's parameter schema and removes any arguments that aren't defined in the schema. For example:

```python
# Tool expects: {"domain": str, "record_type": str}
# Client sends: {"domain": "example.com", "record_type": "A", "extra": "ignored", "schema_hint": "..."}
# Middleware filters to: {"domain": "example.com", "record_type": "A"}
```

See `src/nextdns_mcp/server.py` for implementation details (class `StripExtraFieldsMiddleware`).

# Troubleshooting

Fix common setup and runtime issues.

## "NEXTDNS_API_KEY is required" or 401 errors
- Set `NEXTDNS_API_KEY` or configure the `nextdns.api_key` secret in Docker MCP Gateway.
- Alternatively set `NEXTDNS_API_KEY_FILE` to a readable file containing only the key.

## 403: Read/Write access denied
- Check `NEXTDNS_READ_ONLY` (true disables all writes).
- Verify `NEXTDNS_READABLE_PROFILES`/`NEXTDNS_WRITABLE_PROFILES` (unset denies all; use `ALL` to allow all).
- Ensure the `profile_id` you call is permitted.

## Invalid JSON or array expected
- Bulk tools require the parameter to be a JSON array string (e.g., `'["ads.example.com","tracker.net"]'`).
- Use single quotes to avoid escaping inner quotes in shells.

## Network/DNS issues
- Ensure outbound HTTPS to `api.nextdns.io` and `dns.nextdns.io`.
- Increase `NEXTDNS_HTTP_TIMEOUT` if needed.

## No default profile
- Some tools accept `profile_id`; if omitted, set `NEXTDNS_DEFAULT_PROFILE` or pass `--profile_id` explicitly.
