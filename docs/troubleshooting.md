# Extra/Unknown Fields in Tool Input

**Problem:** AI or CLI clients send extra/unknown fields to MCP tools, causing 400 errors or schema validation failures.

**Solution:** The server is configured to ignore extra/unknown fields for all tool input models (custom and OpenAPI-imported) by:

- Setting `strict_input_validation=False` in the FastMCP server constructor.
- Patching all OpenAPI-imported Pydantic models to allow extra fields using a custom `mcp_component_fn`:

  ```python
  def allow_extra_fields_component_fn(component, *args, **kwargs):
	  # For Pydantic v2
	  if hasattr(component, "model_config"):
		  component.model_config = {**getattr(component, "model_config", {}), "extra": "ignore"}
	  # For Pydantic v1
	  elif hasattr(component, "__config__"):
		  class Config(getattr(component, "__config__")):
			  extra = "ignore"
		  component.__config__ = Config
	  return component
  ...
  mcp = FastMCP.from_openapi(..., mcp_component_fn=allow_extra_fields_component_fn)
  ```

This ensures all tools accept extra/unknown fields in input without error, while still enforcing required/typed fields. See `src/nextdns_mcp/server.py` for implementation details.
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
