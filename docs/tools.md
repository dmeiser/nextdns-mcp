# Tools Catalog

This page lists all MCP tools exposed by the NextDNS MCP Server, organized by category. Tools are generated from the OpenAPI spec with additional custom tools where needed.

Total tools: 76 (68 OpenAPI tools + 7 custom bulk updates + 1 custom DoH lookup)

Notes:
- Two endpoints are excluded: one time-series analytics endpoint that returns 404 from the NextDNS API, and one SSE streaming endpoint (not supported by FastMCP).
- Seven array-body PUT endpoints are implemented as custom tools accepting JSON array strings.

## DNS Testing (1 custom)
- `dohLookup`: Perform DNS-over-HTTPS lookups through a NextDNS profile

## Profile Management (5)
- `listProfiles`
- `getProfile`
- `createProfile`
- `updateProfile`
- `deleteProfile` (use with extreme caution)

## Settings (multiple)
- `getSettings`, `updateSettings`
- `getLogsSettings`, `updateLogsSettings`
- `getBlockPageSettings`, `updateBlockPageSettings`
- `getPerformanceSettings`, `updatePerformanceSettings`
- `getWeb3Settings`, `updateWeb3Settings` (if present in API)

## Logs
- `getLogs`
- `downloadLogs`
- `clearLogs`
- `streamLogs` is excluded (SSE streaming unsupported)

## Analytics
Base:
- `getAnalyticsStatus`
- `getAnalyticsDomains`
- `getAnalyticsReasons`
- `getAnalyticsIPs`
- `getAnalyticsDevices`
- `getAnalyticsProtocols`
- `getAnalyticsQueryTypes`
- `getAnalyticsIPVersions`
- `getAnalyticsDNSSEC`
- `getAnalyticsEncryption`
- `getAnalyticsDestinations`

Time-Series:
- `getAnalyticsStatusSeries`
- `getAnalyticsReasonsSeries`
- `getAnalyticsIPsSeries`
- `getAnalyticsDevicesSeries`
- `getAnalyticsProtocolsSeries`
- `getAnalyticsQueryTypesSeries`
- `getAnalyticsIPVersionsSeries`
- `getAnalyticsDNSSECSeries`
- `getAnalyticsEncryptionSeries`
- `getAnalyticsDestinationsSeries`
- `getAnalyticsDomainsSeries` is excluded (NextDNS API returns 404)

## Content Lists - Denylist
- `getDenylist`
- `addToDenylist`
- `removeFromDenylist`
- `updateDenylist` (custom bulk replacement; JSON array string)
- `updateDenylistEntry` (PATCH)

## Content Lists - Allowlist
- `getAllowlist`
- `addToAllowlist`
- `removeFromAllowlist`
- `updateAllowlist` (custom bulk replacement; JSON array string)
- `updateAllowlistEntry` (PATCH)

## Security
- `getSecuritySettings`, `updateSecuritySettings`
- `getSecurityTLDs`
- `addSecurityTLD`, `removeSecurityTLD`
- `updateSecurityTlds` (custom bulk replacement; JSON array string)

## Privacy
- `getPrivacySettings`, `updatePrivacySettings`
- `getPrivacyBlocklists`, `addPrivacyBlocklist`, `removePrivacyBlocklist`
- `updatePrivacyBlocklists` (custom bulk replacement; JSON array string)
- `getPrivacyNatives`, `addPrivacyNative`, `removePrivacyNative`
- `updatePrivacyNatives` (custom bulk replacement; JSON array string)

## Parental Control
Settings:
- `getParentalControlSettings`, `updateParentalControlSettings`

Services:
- `getParentalControlServices`, `addToParentalControlServices`, `removeFromParentalControlServices`
- `updateParentalControlServices` (custom bulk replacement; JSON array string)
- `updateParentalControlServiceEntry` (PATCH)

Categories:
- `getParentalControlCategories`, `addToParentalControlCategories`, `removeFromParentalControlCategories`
- `updateParentalControlCategories` (custom bulk replacement; JSON array string)
- `updateParentalControlCategoryEntry` (PATCH)

## Notes on Access Control

- Global operations like `listProfiles` and `dohLookup` are always allowed.
- Access to profile-specific operations is controlled by environment variables:
  - `NEXTDNS_READABLE_PROFILES`
  - `NEXTDNS_WRITABLE_PROFILES`
  - `NEXTDNS_READ_ONLY`
