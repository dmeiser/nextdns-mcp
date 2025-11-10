# E2E GitHub Actions Workflow - Testing Guide

## What I Fixed

### 1. **Bash Script Parity with PowerShell**
- Updated `scripts/gateway_e2e_run.sh` to match PowerShell functionality:
  - Fixed secret name: `nextdns.api_key` (was incorrectly `NEXTDNS_API_KEY`)
  - Added environment variable configuration via `docker mcp config write`
  - Added support for `NEXTDNS_READABLE_PROFILES` and `NEXTDNS_WRITABLE_PROFILES`
  - CI-aware cleanup (auto-deletes validation profile when `CI=true`)

### 2. **GitHub Actions Workflow**
- Created `.github/workflows/e2e-mcp-gateway.yml`:
  - Builds `docker-mcp` CLI plugin from source
  - Installs it to `$HOME/.docker/cli-plugins/docker-mcp`
  - Builds your NextDNS MCP Docker image
  - Creates `.env` file with GitHub secret
  - Runs E2E validation script
  - Uploads test report as artifact
  - Displays summary in GitHub Actions UI

### 3. **Secret Handling**
- GitHub secret `NEXTDNS_API_KEY` is written to `.env` file
- Script reads `.env` and pipes API key to `docker mcp secret set nextdns.api_key`
- Environment variables configured via `docker mcp config write`

## How to Test

### Prerequisites
1. Ensure you have set `NEXTDNS_API_KEY` as a repository secret:
   - Go to: Settings → Secrets and variables → Actions → New repository secret
   - Name: `NEXTDNS_API_KEY`
   - Value: Your NextDNS API key

### Option 1: Push to GitHub (Recommended)
```bash
# Commit the changes
git add .github/workflows/e2e-mcp-gateway.yml
git add scripts/gateway_e2e_run.sh
git commit -m "Add E2E GitHub Actions workflow with MCP Gateway"

# Push to trigger workflow (on push to main or PR)
git push origin main
```

### Option 2: Manual Workflow Dispatch
1. Go to Actions tab in GitHub
2. Select "E2E MCP Gateway" workflow
3. Click "Run workflow"
4. Choose:
   - Branch: `main` (or your branch)
   - `allow_live_writes`: 
     - `false` (default) - read-only tests, no profile creation
     - `true` - full write tests, creates/deletes validation profile

### Option 3: Test Locally (Linux/WSL)
```bash
# Set environment variables
export NEXTDNS_API_KEY="your-api-key-here"
export ALLOW_LIVE_WRITES="false"
export NEXTDNS_READABLE_PROFILES="ALL"
export NEXTDNS_WRITABLE_PROFILES="ALL"
export CI="true"

# Run the E2E script
chmod +x scripts/gateway_e2e_run.sh
scripts/gateway_e2e_run.sh
```

## What to Expect

### Successful Run
1. **Build docker-mcp plugin**: ~30-60 seconds
2. **Build NextDNS MCP image**: ~10-30 seconds
3. **Import catalog**: ~1-2 seconds
4. **Enable server**: ~1-2 seconds
5. **Configure secrets**: ~1-2 seconds
6. **Configure env vars**: ~1-2 seconds
7. **Run all tools**: ~2-5 minutes (76 tools)

### Outputs
- **Console**: Colored progress logs
- **Artifact**: `e2e-test-report` (contains `tools_report.jsonl`)
- **Summary**: GitHub Actions summary page shows pass/fail counts

### Expected Results (with `allow_live_writes=false`)
- ✅ **Read-only tools**: Should all pass (~50+ tools)
- ⏭️ **Write tools**: Skipped (~20+ tools)
- ❌ **Failures**: Should be 0 (except possibly `fetch` tool if present)

### Expected Results (with `allow_live_writes=true`)
- ✅ **All tools**: Should pass (~75+ tools)
- Creates temporary validation profile
- Auto-deletes profile at end (CI mode)

## Troubleshooting

### If Build Fails

**Plugin build fails:**
```bash
# Check Go version in workflow log
# Should be 1.24+
```

**Docker image build fails:**
```bash
# Check Dockerfile and poetry.lock are present
# Verify Python 3.14 base image is available
```

### If Secret Not Working

**Check secret name:**
- Must be exactly `NEXTDNS_API_KEY` (case-sensitive)
- In repo Settings → Secrets and variables → Actions

**Check secret value:**
- Should be your NextDNS API key from https://my.nextdns.io/account
- No quotes, no extra whitespace

### If Tools Fail

**"No tools found":**
- Check catalog import succeeded
- Check server enable succeeded
- Check `docker mcp tools ls` output

**"Failed to delete validation profile":**
- This is a warning, not an error
- Profile may have been deleted already
- Or write permissions not set correctly

### If Config Fails

**"Failed to configure environment variables":**
- Check `docker mcp config write` supports YAML format
- May need to adjust config YAML format
- Try: `docker mcp config read` to see current format

## Next Steps

### If Tests Pass
1. ✅ Workflow is working correctly
2. Consider enabling for PRs to auto-test dependency updates
3. Optional: Add to dependabot auto-merge workflow

### If Tests Fail
1. Check workflow logs for specific error
2. Review "Troubleshooting" section above
3. Share error output for further debugging
4. I'll iterate on the workflow to fix issues

## Workflow Features

### Workflow Dispatch Input
- `allow_live_writes`: Toggle between read-only and full write testing
- Defaults to `false` for safety

### Automatic on Push/PR
- Runs on push to `main` branch
- Runs on pull requests
- Always uses `allow_live_writes=false` for safety

### Test Report Artifact
- Uploaded even if tests fail (`if: always()`)
- Contains full JSON log of each tool execution
- Can be downloaded from Actions run page

### GitHub Actions Summary
- Shows pass/fail/skip counts
- Lists failed tools with error messages
- Easy to see at a glance if tests passed

## Files Modified

1. `.github/workflows/e2e-mcp-gateway.yml` (created)
   - Complete GitHub Actions workflow
   - Builds plugin, runs tests, uploads results

2. `scripts/gateway_e2e_run.sh` (updated)
   - Fixed secret name: `nextdns.api_key`
   - Added env var configuration
   - Added CI-aware cleanup

3. `docs/E2E_GITHUB_ACTIONS_GUIDE.md` (this file)
   - Complete testing guide
