# Live API Integration Tests

This directory contains integration tests that run against the **live NextDNS API**.

## Test Files

- **`run_integration_tests.py`** - **Unified test runner** (recommended)
- `test_server_init.py` - Server initialization tests (pytest-based)
- `test_live_api.py` - Live API validation of all 76 MCP tools
- `test_live_access_control.py` - Access control validation (all scenarios)

## Quick Start (Recommended)

Use the unified test runner:

```bash
# Set your API key
export NEXTDNS_API_KEY="your_api_key_here"

# Run ALL integration tests
poetry run python tests/integration/run_integration_tests.py

# Run with auto-cleanup (no prompts)
poetry run python tests/integration/run_integration_tests.py --auto-cleanup
```

### Run Specific Test Suites

```bash
# Run only API validation (76 tools)
poetry run python tests/integration/run_integration_tests.py --only-api

# Run only access control tests
poetry run python tests/integration/run_integration_tests.py --only-access-control

# Run only server initialization
poetry run python tests/integration/run_integration_tests.py --only-server-init

# Skip specific suites
poetry run python tests/integration/run_integration_tests.py --skip-server-init
```

## Individual Test Scripts

You can also run tests individually:

```bash
# Server initialization (pytest)
poetry run pytest tests/integration/test_server_init.py -v

# Live API validation (76 tools)
poetry run python tests/integration/test_live_api.py

# Access control validation
poetry run python tests/integration/test_live_access_control.py
```

## Prerequisites

1. **Valid NextDNS API Key**
   - Sign up at https://nextdns.io
   - Get your API key from the account settings
   - Set the `NEXTDNS_API_KEY` environment variable

2. **Python Environment**
   - Poetry installed
   - Dependencies installed (`poetry install`)

3. **Internet Connection**
   - Tests connect to the live NextDNS API

## Running the Tests

### Quick Start

```bash
# Set your API key
export NEXTDNS_API_KEY="your_api_key_here"

# Run the integration tests
poetry run python tests/integration/test_live_api.py
```

### Using Docker

If you want to test through the Docker container:

```bash
# Build the container
docker build -t nextdns-mcp .

# Run tests inside the container
docker run -it --rm \
  -e NEXTDNS_API_KEY="your_api_key_here" \
  nextdns-mcp \
  python tests/integration/test_live_api.py
```

## What the Tests Do

### 1. Profile Creation
- Creates a new profile named "Validation Profile [timestamp]"
- Uses this profile for all subsequent tests
- Ensures tests don't affect your existing profiles

### 2. Test All Operations
The script systematically tests:

**Profile Management** (4 operations)
- `listProfiles` - List all profiles
- `getProfile` - Get profile details
- `updateProfile` - Update profile name
- `deleteProfile` - Delete profile (with confirmation)

**Settings** (10 operations)
- `getSettings` / `updateSettings`
- `getLogsSettings` / `updateLogsSettings`
- `getBlockPageSettings` / `updateBlockPageSettings`
- `getPerformanceSettings` / `updatePerformanceSettings`
- `getWeb3Settings` / `updateWeb3Settings`

**Security** (5 operations)
- `getSecurity` / `updateSecurity`
- `listThreatIntelligenceFeeds`
- `addThreatIntelligenceFeed`
- `removeThreatIntelligenceFeed`

**Privacy** (8 operations)
- `getPrivacy` / `updatePrivacy`
- `listBlocklists` / `addBlocklist` / `removeBlocklist`
- `listNativeTrackingProtection` / `addNativeTrackingProtection` / `removeNativeTrackingProtection`

**Parental Control** (10 operations)
- `getParentalControl` / `updateParentalControl`
- `listServices` / `addService` / `removeService`
- `listCategories` / `addCategory` / `removeCategory`
- `listRecreationTime` / `setRecreationTime`

**Allowlist/Denylist** (6 operations)
- `listDenylist` / `addDenylist` / `removeDenylist`
- `listAllowlist` / `addAllowlist` / `removeAllowlist`

**Analytics** (10 operations)
- `getAnalyticsStatus`
- `getAnalyticsQueries`
- `getAnalyticsDNSSEC`
- `getAnalyticsEncryption`
- `getAnalyticsIPVersions`
- `getAnalyticsProtocols`
- `getAnalyticsDestinations`
- `getAnalyticsDevices`
- `getAnalyticsRootDomains`
- `getAnalyticsGAFAM`

**Logs** (4 operations)
- `getLogs`
- `streamLogs` (skipped - streaming endpoint)
- `downloadLogs`
- `clearLogs` (skipped - destructive operation)

**Custom Tool** (1 operation)
- `dohLookup` - DNS-over-HTTPS lookup

### 3. Interactive Cleanup

After all tests complete, the script will:
1. Show the validation profile ID
2. **Ask for confirmation** before deleting
3. Only delete if you type "yes"
4. If you decline, the profile remains in your account

## Safety Features

### Read-Only by Default
Most operations are safe to run:
- All GET operations are read-only
- Created resources are cleaned up
- Tests use a dedicated validation profile

### User Confirmation Required
- Profile deletion requires explicit "yes" confirmation
- You can keep the validation profile for manual inspection
- Interrupted tests (Ctrl+C) will still prompt for cleanup

### Skipped Operations
Some operations are intentionally skipped:
- `streamLogs` - Requires streaming connection handling
- `clearLogs` - Destructive operation that can't be undone

## Understanding the Output

### Test Status Symbols
- `✓` **PASS** - Operation succeeded
- `✗` **FAIL** - Operation failed (check error details)
- `⊘` **SKIP** - Operation skipped (see reason)

### Example Output

```
================================================================================
  PROFILE CREATION
================================================================================

✓ createProfile                                        [PASS]
  → Created profile: Validation Profile 20251025_143022 (ID: abc123)

================================================================================
  PROFILE MANAGEMENT
================================================================================

✓ listProfiles                                         [PASS]
  → Found 3 profiles
✓ getProfile                                           [PASS]
  → Profile name: Validation Profile (Updated)
✓ updateProfile                                        [PASS]
  → Profile updated successfully
```

### Test Summary

At the end, you'll see:
- Total number of tests
- Pass/Fail/Skip counts
- Detailed failure information
- List of skipped tests with reasons

## Troubleshooting

### API Key Issues

**Error**: `ERROR: NEXTDNS_API_KEY is required`
- **Solution**: Set the environment variable:
  ```bash
  export NEXTDNS_API_KEY="your_key_here"
  ```

### Network Issues

**Error**: `Connection failed` or `Timeout`
- **Solution**: Check internet connection and firewall settings
- NextDNS API requires outbound HTTPS (port 443)

### Authentication Failures

**Error**: `401 Unauthorized` or `403 Forbidden`
- **Solution**: Verify your API key is correct
- Check that your NextDNS account is active

### Rate Limiting

**Error**: `429 Too Many Requests`
- **Solution**: Wait a few minutes and try again
- NextDNS may rate-limit API requests

## Development

### Adding New Tests

To add tests for new API operations:

1. **Add the operation method**:
   ```python
   async def _your_operation(self):
       response = await self.client.get(f"/your/endpoint")
       response.raise_for_status()
       return "Operation result"
   ```

2. **Call it from a test section**:
   ```python
   async def test_your_section(self):
       await self.test_operation("yourOperation", self._your_operation())
   ```

3. **Add it to `run_all_tests()`**:
   ```python
   await self.test_your_section()
   ```

### Running Individual Tests

You can modify the script to run only specific test sections by commenting out unwanted sections in `run_all_tests()`.

## Coverage

This integration test provides:
- ✅ **100% API operation coverage** - All 54 OpenAPI operations tested
- ✅ **Custom tool coverage** - DoH lookup tested
- ✅ **End-to-end validation** - Tests against real NextDNS API
- ✅ **Safe cleanup** - User confirmation before deletion

## Notes

- Tests create minimal data (one profile + temporary settings)
- All changes are made to the validation profile only
- Your existing profiles are never modified
- Analytics might show no data for newly created profiles (expected)
- Some operations (like adding threat feeds) might fail if the feed doesn't exist (this is normal)

## Support

For issues with:
- **This test script**: Open an issue in the repository
- **NextDNS API**: Contact NextDNS support
- **MCP Server**: Check the main README.md

---

**Warning**: While these tests are designed to be safe, they do interact with your live NextDNS account. Always review what operations are being performed before running integration tests.
