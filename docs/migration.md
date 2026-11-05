# Migration from 3.x to 4.0.0

## 4.0.0 upgrade notes

4.0.0 replaces the six grouped management tools with one tool per operation of
the shipped OpenAPI document (78 generated tools), and keeps the two
operations the server performs itself. An operation selector argument no
longer exists: the operation is the tool's name, and every tool takes only
the arguments its own endpoint takes. There is no behavioural replacement
for `streamLogs`: polling `getLogs` (query search) or `downloadLogs` (full
CSV) covers what it was used for.

<!-- migration-table:start -->
| Old tool | New tools |
|---|---|
| manageProfiles | createProfile, deleteProfile, getProfile, listProfiles, updateProfile |
| manageSettings | getSettings, updateSettings (general); getLogsSettings, updateLogsSettings (logs); getBlockPageSettings, updateBlockPageSettings (block page); getPerformanceSettings, updatePerformanceSettings (performance); getPrivacySettings, updatePrivacySettings (privacy); getSecuritySettings, updateSecuritySettings (security); getParentalControlSettings, updateParentalControlSettings (parental control) |
| manageLists | getAllowlist, addToAllowlist, removeFromAllowlist, replaceAllowlist, updateAllowlistEntry (allowlist); getDenylist, addToDenylist, removeFromDenylist, replaceDenylist, updateDenylistEntry (denylist) |
| manageRewrites | addRewrite, deleteRewrite, listRewrites |
| manageLogs | getLogs, clearLogs, downloadLogs |
| queryAnalytics | getAnalyticsDomains, getAnalyticsDevices, getAnalyticsIPs, getAnalyticsIPVersions, getAnalyticsQueryTypes, getAnalyticsProtocols, getAnalyticsEncryption, getAnalyticsDNSSEC, getAnalyticsDestinations, getAnalyticsReasons, getAnalyticsStatus (totals); getAnalyticsDevicesSeries, getAnalyticsIPsSeries, getAnalyticsIPVersionsSeries, getAnalyticsQueryTypesSeries, getAnalyticsProtocolsSeries, getAnalyticsEncryptionSeries, getAnalyticsDNSSECSeries, getAnalyticsDestinationsSeries, getAnalyticsReasonsSeries, getAnalyticsStatusSeries (`;series` time series, each under the same 2000-point budget `plotAnalytics` has always enforced) |
| dohLookup | dohLookup (unchanged) |
| plotAnalytics | plotAnalytics (unchanged) |
<!-- migration-table:end -->

Operations the grouped tools never exposed are new surface in 4.0.0, not
replacements: the privacy families (`getPrivacyBlocklists`, `addPrivacyBlocklist`,
`removePrivacyBlocklist`, `replacePrivacyBlocklists`, `getPrivacyNatives`,
`addPrivacyNative`, `removePrivacyNative`, `replacePrivacyNatives`), the
security TLD list (`getSecurityTLDs`, `addSecurityTLD`, `removeSecurityTLD`,
`replaceSecurityTLDs`), the per-entry parental control families
(`getParentalControlCategories`, `addToParentalControlCategories`,
`removeFromParentalControlCategories`, `replaceParentalControlCategories`,
`updateParentalControlCategoryEntry`, and the same five for Services), and
`listRewrites` (the grouped rewrite tool could only add and delete).

Entry ids changed meaning: the grouped list and rewrite tools took a
hostname or domain and addressed entries by value, where the new tools
address an entry by its opaque `id` — the `id` field of the entry in that
endpoint's list response (`getDenylist`, `getAllowlist`, `listRewrites`).
There is no hostname→id resolution; list first, then act on the id.
