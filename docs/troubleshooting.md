# Troubleshooting

Fix common setup and runtime issues.

## Extra/Unknown Fields in Tool Input

**Problem:** AI clients (like OpenAI) or CLI tools may send extra/unknown fields with tool arguments that don't match the tool's input schema, causing validation errors like "Unexpected keyword argument".

**Solution:** The server runs a middleware (`StripExtraFieldsMiddleware`) on every tool call that filters out fields the tool's published schema does not declare, before the arguments are validated. This approach:

- **Drops extra fields and reports them** (no error): the answer carries `ignored_arguments` naming every field that was not applied, with a `hint` pointing at the tool that does declare the name. Nothing is dropped silently — a call that carried a field this endpoint has no use for says so
- **Maintains type safety** for known/required fields, coercing the obvious string spellings of a boolean, integer or number field at the top level (`"true"`, `"86400"`)
- **Works with every published tool** — the ones generated from the OpenAPI document included, which is the point: a generated tool has no hand-written Python signature to reject a keyword against, so the relaxation runs between the caller and the generator
- **Logs stripped fields** at DEBUG level for troubleshooting

The middleware inspects each tool's parameter schema and removes any arguments that aren't defined in the schema. For example:

```python
# Tool expects: {"domain": str, "record_type": str}
# Client sends: {"domain": "example.com", "record_type": "A", "extra": "ignored", "schema_hint": "..."}
# The call runs as: {"domain": "example.com", "record_type": "A"}
# And the answer says what was not applied:
#   {"ignored_arguments": ["extra", "schema_hint"], "hint": "these arguments are not parameters of this tool and were not applied …"}
```

See `src/nextdns_mcp/openapi.py` for implementation details (class `StripExtraFieldsMiddleware`).
The middleware only filters top-level tool arguments; fields nested inside an argument value are not checked.

## "NEXTDNS_API_KEY is required" or 401 errors
- Set `NEXTDNS_API_KEY` in your environment.
- Alternatively set `NEXTDNS_API_KEY_FILE` to a readable file containing only the key.

## 403: Read/Write access denied
- Check `NEXTDNS_READ_ONLY` (true disables all writes).
- Verify `NEXTDNS_READABLE_PROFILES`/`NEXTDNS_WRITABLE_PROFILES` (unset denies all; use `ALL` to allow all).
- Check `NEXTDNS_PROTECTED_PROFILES`: a listed profile is unwritable even under `NEXTDNS_WRITABLE_PROFILES=ALL`, and that denial is the feature working, not a misconfiguration (see [safety.md](safety.md)).
- Ensure the `profile_id` you call is permitted.
- If the message is `Forbidden URL: <url>`, the access-control layer could not classify the request URL against a profile you may reach — normally an id in the URL that is not a valid profile id (6 or 10 lowercase alphanumeric characters; `listProfiles()` returns the valid ones).
- If the message is `Blocked request to non-NextDNS host: <host>`, the request targeted a host outside the client's destination allow-list and was refused before any request was sent. See [safety.md](safety.md).

## Only `search_tools` and `call_tool` are listed
That is the default, not a broken server. `NEXTDNS_TOOL_SEARCH` defaults to `bm25`, so `tools/list` advertises the synthetic discovery pair and every operation tool is found by searching: call `search_tools` with what you want to do, then `call_tool` with the name the search returned. The hidden tools are still callable — disclosure hides the listing, it does not restrict access. Set `NEXTDNS_TOOL_SEARCH=off` to advertise all 80 tools in the listing instead (see [configuration.md](configuration.md)).

## Log download refused
- If `downloadLogs` returns `Refusing log download redirect to a non-public or non-https destination`, the download endpoint pointed its redirect at a destination that is not https on a globally routable host, and the redirect was refused before that target was contacted. See [safety.md](safety.md).

## Invalid JSON or array expected
- Bulk tools take a JSON array of entry objects, e.g. `'[{"id": "ads.example.com"}, {"id": "tracker.net"}]'`.
- Use single quotes to avoid escaping inner quotes in shells.
- Every generated call validates its arguments against that endpoint's published schema before any request goes
  out: `body` must be an array of objects with a string `id`, and a value that fails the schema is rejected there,
  without reaching the API. Upstream validates the body too, so a shape the schema lets through and NextDNS does
  not still comes back as a typed `http_error` carrying the upstream error document.

## Network/DNS issues
- Ensure outbound HTTPS to `api.nextdns.io` and `dns.nextdns.io`.
- Increase `NEXTDNS_HTTP_TIMEOUT` if needed, then restart the server process: the timeout is read when the HTTP clients are built, so an in-place change has no effect (see [configuration.md](configuration.md)).

## No default profile
- `dohLookup` and `plotAnalytics` accept an optional `profile_id`; if omitted, set `NEXTDNS_DEFAULT_PROFILE` or pass `profile_id` explicitly.
- Every other profile-scoped tool — all the generated operation tools (`getProfile`, `updateDenylistEntry`, `getAnalyticsStatus`, …) — requires `profile_id` and never falls back to `NEXTDNS_DEFAULT_PROFILE`; only `dohLookup` and `plotAnalytics` use the default.
- The two surfaces name a missing argument in their own terms, and the codes are distinct. A generated tool answers `missing_required_argument`, listing what the endpoint still needs and where it comes from (for `profile_id` the hint reads `listProfiles()`); `dohLookup` and `plotAnalytics` answer `missing_profile_id` for the same omission.
- A malformed `profile_id` — wrong length or characters — answers `invalid_profile_id` with the rejected value quoted back, and a value that could address a different endpoint (`a/b`, `..`, a `#` fragment) is refused with the same code before the request path is built. Profile ids are 6 or 10 lowercase alphanumeric characters; `listProfiles()` returns the ones this API key can reach.
