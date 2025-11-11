#!/usr/bin/env bash
#
# run_all_tools.sh - Enumerate and execute all NextDNS MCP tools via Docker MCP Gateway
#
# This script follows a 4-step process:
# 1. CREATE: Creates a test profile (if ALLOW_WRITES=true)
# 2. TEST: Executes all tools using the test profile
# 3. CLEANUP: Deletes the test profile (if created in step 1)
# 4. REPORT: Displays execution summary and outcomes
#
# Usage:
#   ./run_all_tools.sh [allow_writes]
#
# Arguments:
#   allow_writes - Enable write operations and profile creation (default: false)
#
# Output:
#   - Console: Colored progress output with step markers
#   - File: artifacts/tools_report.jsonl (one JSON object per line per tool)
#
# Exit Codes:
#   0 - All tools executed successfully
#   1 - One or more tools failed or script error
#

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*" >&2
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*" >&2
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# Configuration
ALLOW_WRITES="${1:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
ARTIFACTS_DIR="${PROJECT_DIR}/artifacts"
REPORT_FILE="${ARTIFACTS_DIR}/tools_report.jsonl"

# Ensure artifacts directory exists
mkdir -p "${ARTIFACTS_DIR}"

log_info "Starting NextDNS MCP tools enumeration and execution"
log_info "Allow writes: ${ALLOW_WRITES}"
log_info "Report file: ${REPORT_FILE}"

# Clear previous report
>"${REPORT_FILE}"

# Preflight checks
log_info "Performing preflight checks..."

# Check Docker MCP is responding
if ! docker mcp tools ls >/dev/null 2>&1; then
    log_error "Failed to enumerate tools from Docker MCP"
    log_error ""
    log_error "Troubleshooting steps:"
    log_error "1. Verify Docker Desktop is running"
    log_error "2. Check Docker MCP version: docker mcp version"
    log_error "3. Verify server is enabled: docker mcp server ls"
    log_error "4. Check logs: docker mcp logs"
    exit 1
fi

log_success "Docker MCP is responding"

# Parse tool names - tools ls returns an array of tool objects
# Filter out MCP Gateway built-in tools (mcp-*, code-*) - only test NextDNS tools
mapfile -t ALL_TOOLS < <(docker mcp tools ls --format json 2>&1 | jq -r '.[] | .name')

if [ ${#ALL_TOOLS[@]} -eq 0 ]; then
    log_error "No tools found"
    log_error "The MCP server may not be properly configured"
    log_error "Run: docker mcp catalog import ./catalog.yaml"
    log_error "Run: docker mcp server enable nextdns"
    exit 1
fi

# Filter to only include NextDNS tools (exclude MCP Gateway built-in tools)
TOOL_NAMES=()
FILTERED_COUNT=0
for TOOL in "${ALL_TOOLS[@]}"; do
    # Exclude MCP Gateway built-in tools
    if [[ "${TOOL}" =~ ^(mcp-|code-) ]]; then
        log_info "Filtering out MCP Gateway built-in tool: ${TOOL}"
        FILTERED_COUNT=$((FILTERED_COUNT + 1))
        continue
    fi
    TOOL_NAMES+=("${TOOL}")
done

TOTAL_TOOLS=${#ALL_TOOLS[@]}
TOOL_COUNT=${#TOOL_NAMES[@]}
log_success "Found ${TOTAL_TOOLS} tools total, ${TOOL_COUNT} NextDNS tools (filtered ${FILTERED_COUNT} non-NextDNS tools)"

# Step 1: Create a test profile if writes are enabled
CREATED_PROFILE_ID=""
if [ "${ALLOW_WRITES}" = "true" ]; then
    log_info "Step 1: Creating test profile..."
    TIMESTAMP=$(date +%s)
    PROFILE_NAME="E2E Test Profile ${TIMESTAMP}"
    
    # Call createProfile using key=value syntax (Docker MCP CLI format)
    PROFILE_RESULT=$(docker mcp tools call createProfile "name=${PROFILE_NAME}" 2>&1 || echo "")
    CREATE_EXIT_CODE=$?
    
    # Extract profile ID from response - filter out Docker MCP timing info, then parse JSON
    # API returns {data: {id: "..."}} wrapped in timing output
    CREATED_PROFILE_ID=$(echo "${PROFILE_RESULT}" | grep -E '^\{' | jq -r '.data.id // .id // empty' 2>/dev/null || echo "")
    
    if [ ${CREATE_EXIT_CODE} -eq 0 ] && [ -n "${CREATED_PROFILE_ID}" ] && [ "${CREATED_PROFILE_ID}" != "null" ]; then
        log_success "Created test profile: ${CREATED_PROFILE_ID}"
        echo "${CREATED_PROFILE_ID}" >"${ARTIFACTS_DIR}/test_profile_id.txt"
        
        # Record successful creation
        jq -n \
            --arg tool "createProfile" \
            --arg status "OK" \
            --arg profile_id "${CREATED_PROFILE_ID}" \
            --arg profile_name "${PROFILE_NAME}" \
            '{tool: $tool, status: $status, profile_id: $profile_id, profile_name: $profile_name, phase: "setup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        # Use the created profile for all tests
        PROFILE_ID="${CREATED_PROFILE_ID}"
    else
        log_error "Failed to create test profile"
        log_error "Response: ${PROFILE_RESULT}"
        
        # Record failed creation
        jq -n \
            --arg tool "createProfile" \
            --arg status "FAILED" \
            --arg error "${PROFILE_RESULT}" \
            '{tool: $tool, status: $status, error: $error, phase: "setup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        exit 1
    fi
else
    log_info "Step 1: Skipping profile creation (ALLOW_WRITES=false)"
    
    # In read-only mode, get first available profile
    log_info "Fetching existing profile for read-only tests..."
    
    PROFILES_RESULT=$(docker mcp tools call listProfiles '{}' 2>&1 | grep -E '^\{' || echo "")
    
    if [ -z "${PROFILES_RESULT}" ]; then
        log_error "Failed to fetch profiles"
        exit 1
    fi
    
    PROFILE_ID=$(echo "${PROFILES_RESULT}" | jq -r '.data[0].id' 2>/dev/null || echo "")
    
    if [ -z "${PROFILE_ID}" ] || [ "${PROFILE_ID}" = "null" ]; then
        log_error "No profiles available for testing"
        log_error "Run with ALLOW_WRITES=true to create a test profile"
        exit 1
    fi
    
    log_success "Using existing profile: ${PROFILE_ID}"
fi

log_info "Step 2: Testing ${TOOL_COUNT} tools with profile ${PROFILE_ID}..."

# Define read-only tools
declare -A READ_ONLY_TOOLS=(
    ["listProfiles"]=1
    ["getProfile"]=1
    ["getSettings"]=1
    ["getAllowlist"]=1
    ["getDenylist"]=1
    ["getPrivacyBlocklists"]=1
    ["getPrivacyNatives"]=1
    ["getPrivacySettings"]=1
    ["getSecurityTLDs"]=1
    ["getSecuritySettings"]=1
    ["getParentalControlCategories"]=1
    ["getParentalControlServices"]=1
    ["getParentalControlSettings"]=1
    ["getBlockPageSettings"]=1
    ["getPerformanceSettings"]=1
    ["getLogsSettings"]=1
    ["getLogs"]=1
    ["getAnalyticsDomains"]=1
    ["getAnalyticsStatus"]=1
    ["getAnalyticsDevices"]=1
    ["getAnalyticsProtocols"]=1
    ["getAnalyticsEncryption"]=1
    ["getAnalyticsIPVersions"]=1
    ["getAnalyticsDNSSEC"]=1
    ["getAnalyticsIPs"]=1
    ["getAnalyticsQueryTypes"]=1
    ["getAnalyticsReasons"]=1
    ["getAnalyticsDestinations"]=1
    ["dohLookup"]=1
)

# Function to get test arguments for a tool (returns space-separated key=value pairs)
get_tool_args() {
    local TOOL_NAME="$1"
    local FROM_TIMESTAMP=$(date -d '1 day ago' +%s 2>/dev/null || date -v-1d +%s 2>/dev/null || echo "1704067200")
    local HOURS_AGO_TIMESTAMP=$(date -d '1 hour ago' +%s 2>/dev/null || date -v-1H +%s 2>/dev/null || echo "$(date +%s)")
    
    case "${TOOL_NAME}" in
        createProfile)
            echo 'name=E2E-Test-Profile-Tool-Validation'
            ;;
        deleteProfile)
            # Delete the profile created by createProfile test
            if [ -n "${CREATED_TEST_PROFILE_ID:-}" ]; then
                echo "profile_id=${CREATED_TEST_PROFILE_ID}"
            else
                # Fallback: use a non-existent ID (will return 404, which is expected)
                echo "profile_id=000000"
            fi
            ;;
        getProfile)
            echo "profile_id=${PROFILE_ID}"
            ;;
        getSettings|getAllowlist|getDenylist|getPrivacyBlocklists|getPrivacyNatives|getPrivacySettings|getSecurityTLDs|getSecuritySettings|getParentalControlCategories|getParentalControlServices|getParentalControlSettings|getBlockPageSettings|getPerformanceSettings|getLogsSettings)
            echo "profile_id=${PROFILE_ID}"
            ;;
        getAnalyticsDomains|getAnalyticsStatus|getAnalyticsDevices|getAnalyticsProtocols|getAnalyticsEncryption|getAnalyticsIPVersions|getAnalyticsDNSSEC|getAnalyticsIPs|getAnalyticsQueryTypes|getAnalyticsReasons)
            echo "profile_id=${PROFILE_ID}" "from=${FROM_TIMESTAMP}"
            ;;
        getAnalyticsDNSSECSeries|getAnalyticsDevicesSeries|getAnalyticsEncryptionSeries|getAnalyticsIPVersionsSeries|getAnalyticsIPsSeries|getAnalyticsProtocolsSeries|getAnalyticsQueryTypesSeries|getAnalyticsReasonsSeries|getAnalyticsStatusSeries)
            echo "profile_id=${PROFILE_ID}" "from=${FROM_TIMESTAMP}"
            ;;
        getAnalyticsDestinations|getAnalyticsDestinationsSeries)
            echo "profile_id=${PROFILE_ID}" "from=${FROM_TIMESTAMP}" "type=countries"
            ;;
        getLogs|downloadLogs)
            echo "profile_id=${PROFILE_ID}" "from=${HOURS_AGO_TIMESTAMP}" "limit=10"
            ;;
        clearLogs)
            echo "profile_id=${PROFILE_ID}"
            ;;
        dohLookup)
            echo "domain=example.com" "profile_id=${PROFILE_ID}" "record_type=A"
            ;;
        listProfiles)
            echo "{}"
            ;;
        addToAllowlist|addToDenylist)
            echo "profile_id=${PROFILE_ID}" "id=test-example.com"
            ;;
        removeFromAllowlist|removeFromDenylist)
            echo "profile_id=${PROFILE_ID}" "entry_id=test-example.com"
            ;;
        addPrivacyBlocklist)
            echo "profile_id=${PROFILE_ID}" "id=nextdns-recommended"
            ;;
        removePrivacyBlocklist)
            echo "profile_id=${PROFILE_ID}" "entry_id=nextdns-recommended"
            ;;
        addPrivacyNative)
            echo "profile_id=${PROFILE_ID}" "id=apple"
            ;;
        removePrivacyNative)
            echo "profile_id=${PROFILE_ID}" "entry_id=apple"
            ;;
        addSecurityTLD)
            echo "profile_id=${PROFILE_ID}" "id=zip"
            ;;
        removeSecurityTLD)
            echo "profile_id=${PROFILE_ID}" "entry_id=zip"
            ;;
        addToParentalControlCategories|removeFromParentalControlCategories)
            echo "profile_id=${PROFILE_ID}" "id=gambling"
            ;;
        addToParentalControlServices|removeFromParentalControlServices)
            echo "profile_id=${PROFILE_ID}" "id=tiktok"
            ;;
        updateAllowlistEntry|updateDenylistEntry)
            echo "profile_id=${PROFILE_ID}" "entry_id=test-example.com" "active=true"
            ;;
        updateProfile)
            echo "profile_id=${PROFILE_ID}" "name=UpdatedTestProfile"
            ;;
        updateSettings)
            # Note: Nested objects may not work with key=value format - skip for now
            echo "profile_id=${PROFILE_ID}"
            ;;
        updateBlockPageSettings)
            echo "profile_id=${PROFILE_ID}" "enabled=true"
            ;;
        updateLogsSettings)
            echo "profile_id=${PROFILE_ID}" "enabled=true" "retention=86400"
            ;;
        updatePerformanceSettings)
            echo "profile_id=${PROFILE_ID}" "ecs=true" "cacheBoost=true"
            ;;
        updatePrivacySettings)
            echo "profile_id=${PROFILE_ID}" "disguisedTrackers=true" "allowAffiliate=false"
            ;;
        updateSecuritySettings)
            echo "profile_id=${PROFILE_ID}" "threatIntelligenceFeeds=true" "googleSafeBrowsing=true"
            ;;
        updateParentalControlSettings)
            echo "profile_id=${PROFILE_ID}" "safeSearch=true" "youtubeRestrictedMode=true"
            ;;
        updateParentalControlCategoryEntry)
            echo "profile_id=${PROFILE_ID}" "id=gambling" "active=true"
            ;;
        updateParentalControlServiceEntry)
            echo "profile_id=${PROFILE_ID}" "id=tiktok" "active=true"
            ;;
        updateAllowlist)
            echo "profile_id=${PROFILE_ID}" 'entries=["test1.com","test2.com"]'
            ;;
        updateDenylist)
            echo "profile_id=${PROFILE_ID}" 'entries=["block1.com","block2.com"]'
            ;;
        updatePrivacyBlocklists)
            echo "profile_id=${PROFILE_ID}" 'blocklists=["nextdns-recommended","oisd"]'
            ;;
        updatePrivacyNatives)
            echo "profile_id=${PROFILE_ID}" 'natives=["apple","windows"]'
            ;;
        updateSecurityTlds)
            echo "profile_id=${PROFILE_ID}" 'tlds=["zip","mov"]'
            ;;
        updateParentalControlCategories)
            echo "profile_id=${PROFILE_ID}" 'categories=["gambling","dating"]'
            ;;
        updateParentalControlServices)
            echo "profile_id=${PROFILE_ID}" 'services=["tiktok","fortnite"]'
            ;;
        *)
            echo "{}"
            ;;
    esac
}

# Execution tracking
EXECUTED_COUNT=0
FAILED_COUNT=0
SKIPPED_COUNT=0

log_info "Executing tools..."

# Execute each tool
for TOOL_NAME in "${TOOL_NAMES[@]}"; do
    # Reset tool args for this iteration
    TOOL_ARGS=""
    
    # Skip if tool is not read-only and writes are disabled
    if [ "${ALLOW_WRITES}" != "true" ] && [ -z "${READ_ONLY_TOOLS[${TOOL_NAME}]:-}" ]; then
        log_warn "Skipping ${TOOL_NAME}: Write operations disabled (ALLOW_LIVE_WRITES=false)"
        
        # Record skipped tool
        jq -n \
            --arg tool "${TOOL_NAME}" \
            --arg status "SKIPPED" \
            --arg reason "Write operations disabled" \
            '{tool: $tool, status: $status, reason: $reason, timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi
    
    log_info "Executing: ${TOOL_NAME}"
    
    # Pre-execution setup for tools that need existing resources
    case "${TOOL_NAME}" in
        updateAllowlistEntry)
            # Ensure entry exists before trying to update it
            log_info "  Pre-setup: Adding allowlist entry for update test"
            docker mcp tools call addToAllowlist "profile_id=${PROFILE_ID}" "id=test-example.com" >/dev/null 2>&1 || true
            ;;
        updateDenylistEntry)
            # Ensure entry exists before trying to update it
            log_info "  Pre-setup: Adding denylist entry for update test"
            docker mcp tools call addToDenylist "profile_id=${PROFILE_ID}" "id=test-example.com" >/dev/null 2>&1 || true
            ;;
        updateParentalControlCategoryEntry)
            # Ensure category exists before trying to update it
            log_info "  Pre-setup: Adding parental control category for update test"
            docker mcp tools call addToParentalControlCategories "profile_id=${PROFILE_ID}" "id=gambling" >/dev/null 2>&1 || true
            ;;
        updateParentalControlServiceEntry)
            # Ensure service exists before trying to update it
            log_info "  Pre-setup: Adding parental control service for update test"
            docker mcp tools call addToParentalControlServices "profile_id=${PROFILE_ID}" "id=tiktok" >/dev/null 2>&1 || true
            ;;
    esac
    
    # Get test arguments as key=value pairs (unless overridden in pre-setup)
    if [ -z "${TOOL_ARGS:-}" ]; then
        TOOL_ARGS=$(get_tool_args "${TOOL_NAME}")
    fi
    
    # Execute tool and capture result
    START_TIME=$(date +%s)
    set +e
    TOOL_OUTPUT=$(docker mcp tools call "${TOOL_NAME}" ${TOOL_ARGS} 2>&1)
    EXIT_CODE=$?
    set -e
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    
    # Parse output and status
    if [ ${EXIT_CODE} -eq 0 ]; then
        log_success "${TOOL_NAME}: OK"
        
        # Track profile created by createProfile test for use by deleteProfile
        if [ "${TOOL_NAME}" = "createProfile" ]; then
            log_info "  Parsing createProfile output for profile ID..."
            CREATED_TEST_PROFILE_ID=$(echo "${TOOL_OUTPUT}" | jq -r '.data.id // empty' 2>/dev/null || echo "")
            if [ -n "${CREATED_TEST_PROFILE_ID}" ] && [ "${CREATED_TEST_PROFILE_ID}" != "null" ]; then
                log_info "  Tracked test profile ID for deletion: ${CREATED_TEST_PROFILE_ID}"
            else
                log_warn "  Could not extract profile ID from createProfile output"
                log_warn "  Output was: ${TOOL_OUTPUT}"
            fi
        fi
        
        # Record successful execution
        jq -n \
            --arg tool "${TOOL_NAME}" \
            --arg status "OK" \
            --arg args "${TOOL_ARGS}" \
            --arg duration "${DURATION}s" \
            '{tool: $tool, status: $status, args: $args, duration: $duration, timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        EXECUTED_COUNT=$((EXECUTED_COUNT + 1))
    else
        log_error "${TOOL_NAME}: FAILED (exit code ${EXIT_CODE})"
        
        # Extract error message
        ERROR_MSG=$(echo "${TOOL_OUTPUT}" | grep -E 'error|Error|ERROR' | head -1 || echo "Unknown error")
        
        # Record failed execution
        jq -n \
            --arg tool "${TOOL_NAME}" \
            --arg status "FAILED" \
            --arg args "${TOOL_ARGS}" \
            --arg error "${ERROR_MSG}" \
            --arg exit_code "${EXIT_CODE}" \
            --arg duration "${DURATION}s" \
            '{tool: $tool, status: $status, args: $args, error: $error, exit_code: ($exit_code | tonumber), duration: $duration, timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        FAILED_COUNT=$((FAILED_COUNT + 1))
    fi
done

# Step 3: Clean up test profile if we created one
if [ -n "${CREATED_PROFILE_ID}" ]; then
    log_info "Step 3: Cleaning up test profile..."
    
    # Call deleteProfile using key=value syntax
    DELETE_RESULT=$(docker mcp tools call deleteProfile "profile_id=${CREATED_PROFILE_ID}" 2>&1 || echo "")
    DELETE_EXIT_CODE=$?
    
    if [ ${DELETE_EXIT_CODE} -eq 0 ]; then
        log_success "Deleted test profile: ${CREATED_PROFILE_ID}"
        
        # Record successful deletion
        jq -n \
            --arg tool "deleteProfile" \
            --arg status "OK" \
            --arg profile_id "${CREATED_PROFILE_ID}" \
            '{tool: $tool, status: $status, profile_id: $profile_id, phase: "cleanup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"
    else
        log_error "Failed to delete test profile: ${CREATED_PROFILE_ID}"
        log_error "Response: ${DELETE_RESULT}"
        
        # Record failed deletion
        jq -n \
            --arg tool "deleteProfile" \
            --arg status "FAILED" \
            --arg profile_id "${CREATED_PROFILE_ID}" \
            --arg error "${DELETE_RESULT}" \
            '{tool: $tool, status: $status, profile_id: $profile_id, error: $error, phase: "cleanup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        FAILED_COUNT=$((FAILED_COUNT + 1))
    fi
    
    # Clean up marker file
    rm -f "${ARTIFACTS_DIR}/test_profile_id.txt"
else
    log_info "Step 3: No test profile to clean up (read-only mode)"
fi

# Step 4: Report outcomes
log_info ""
log_info "================================"
log_info "Step 4: Execution Summary"
log_info "================================"
log_info "Total tools: ${TOOL_COUNT}"
log_success "Executed: ${EXECUTED_COUNT}"
log_warn "Skipped: ${SKIPPED_COUNT}"
log_error "Failed: ${FAILED_COUNT}"
log_info "Report: ${REPORT_FILE}"

# Exit with error if any tools failed
if [ ${FAILED_COUNT} -gt 0 ]; then
    exit 1
else
    exit 0
fi
