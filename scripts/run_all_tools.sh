#!/usr/bin/env bash
#
# run_all_tools.sh - Enumerate and execute all NextDNS MCP tools via Docker MCP Gateway
#
# This script:
# 1. Connects to Docker MCP Gateway via CLI
# 2. Enumerates all available MCP tools
# 3. Executes each tool with appropriate test arguments
# 4. Generates a machine-readable JSONL report
#
# Usage:
#   ./run_all_tools.sh [allow_writes] [profile_id]
#
# Arguments:
#   allow_writes - Enable write operations (default: false)
#   profile_id   - NextDNS profile ID (optional, will create if allow_writes=true)
#
# Output:
#   - Console: Colored progress output
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
PROFILE_ID="${2:-}"
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
mapfile -t TOOL_NAMES < <(docker mcp tools ls --format json 2>&1 | jq -r '.[] | .name')

if [ ${#TOOL_NAMES[@]} -eq 0 ]; then
    log_error "No tools found"
    log_error "The MCP server may not be properly configured"
    log_error "Run: docker mcp catalog import ./catalog.yaml"
    log_error "Run: docker mcp server enable nextdns"
    exit 1
fi

TOOL_COUNT=${#TOOL_NAMES[@]}
log_success "Found ${TOOL_COUNT} tools"

# Create or use existing profile for tests
if [ -z "${PROFILE_ID}" ]; then
    if [ "${ALLOW_WRITES}" = "true" ]; then
        log_info "Creating validation profile for testing..."
        TIMESTAMP=$(date +%s)
        PROFILE_NAME="Validation Profile ${TIMESTAMP}"
        
        PROFILE_RESULT=$(docker mcp tools call createProfile "name=${PROFILE_NAME}" 2>&1 || echo "")
        PROFILE_ID=$(echo "${PROFILE_RESULT}" | grep -o '{"id":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")
        
        if [ -z "${PROFILE_ID}" ]; then
            log_error "Failed to create validation profile"
            log_error "Output: ${PROFILE_RESULT}"
            exit 1
        fi
        
        log_success "Created validation profile: ${PROFILE_ID}"
        echo "${PROFILE_ID}" >"${ARTIFACTS_DIR}/validation_profile_id.txt"
    else
        # For read-only mode, try to get the first available profile
        log_info "Read-only mode: Looking for existing profile..."
        
        PROFILES_RESULT=$(docker mcp tools call listProfiles '{}' 2>&1 | grep -E '^\{' || echo "")
        
        if [ -z "${PROFILES_RESULT}" ]; then
            log_error "No JSON output from listProfiles"
            exit 1
        fi
        
        PROFILE_ID=$(echo "${PROFILES_RESULT}" | jq -r '.data[0].id' 2>/dev/null || echo "")
        
        if [ -z "${PROFILE_ID}" ] || [ "${PROFILE_ID}" = "null" ]; then
            log_error "No profiles found"
            log_error "Create a profile first or run with allow_writes=true to create one"
            exit 1
        fi
        
        log_success "Using existing profile: ${PROFILE_ID}"
    fi
else
    log_info "Using provided profile: ${PROFILE_ID}"
fi

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

# Function to get test arguments for a tool (returns JSON string)
get_tool_args() {
    local TOOL_NAME="$1"
    local FROM_TIMESTAMP=$(date -d '1 day ago' +%s 2>/dev/null || date -v-1d +%s 2>/dev/null || echo "1704067200")
    local HOURS_AGO_TIMESTAMP=$(date -d '1 hour ago' +%s 2>/dev/null || date -v-1H +%s 2>/dev/null || echo "$(date +%s)")
    
    case "${TOOL_NAME}" in
        getProfile)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid}'
            ;;
        getSettings|getAllowlist|getDenylist|getPrivacyBlocklists|getPrivacyNatives|getPrivacySettings|getSecurityTLDs|getSecuritySettings|getParentalControlCategories|getParentalControlServices|getParentalControlSettings|getBlockPageSettings|getPerformanceSettings|getLogsSettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid}'
            ;;
        getAnalyticsDomains|getAnalyticsStatus|getAnalyticsDevices|getAnalyticsProtocols|getAnalyticsEncryption|getAnalyticsIPVersions|getAnalyticsDNSSEC|getAnalyticsIPs|getAnalyticsQueryTypes|getAnalyticsReasons)
            jq -n --arg pid "${PROFILE_ID}" --arg from "${FROM_TIMESTAMP}" '{profile_id: $pid, from: $from}'
            ;;
        getAnalyticsDNSSECSeries|getAnalyticsDestinationsSeries|getAnalyticsDevicesSeries|getAnalyticsEncryptionSeries|getAnalyticsIPVersionsSeries|getAnalyticsIPsSeries|getAnalyticsProtocolsSeries|getAnalyticsQueryTypesSeries|getAnalyticsReasonsSeries|getAnalyticsStatusSeries)
            jq -n --arg pid "${PROFILE_ID}" --arg from "${FROM_TIMESTAMP}" '{profile_id: $pid, from: $from}'
            ;;
        getAnalyticsDestinations)
            jq -n --arg pid "${PROFILE_ID}" --arg from "${FROM_TIMESTAMP}" '{profile_id: $pid, from: $from, type: "countries"}'
            ;;
        getLogs|downloadLogs|clearLogs)
            jq -n --arg pid "${PROFILE_ID}" --arg from "${HOURS_AGO_TIMESTAMP}" '{profile_id: $pid, from: $from, limit: 10}'
            ;;
        dohLookup)
            jq -n --arg pid "${PROFILE_ID}" '{domain: "example.com", profile_id: $pid, record_type: "A"}'
            ;;
        listProfiles)
            echo "{}"
            ;;
        createProfile)
            jq -n --arg name "Test Profile $(date +%s)" '{name: $name}'
            ;;
        addToAllowlist|addToDenylist)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "test-example.com"}'
            ;;
        removeFromAllowlist|removeFromDenylist)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, entry_id: "test-example.com"}'
            ;;
        addPrivacyBlocklist|removePrivacyBlocklist)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "nextdns-recommended"}'
            ;;
        addPrivacyNative|removePrivacyNative)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "apple"}'
            ;;
        addSecurityTLD|removeSecurityTLD)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "zip"}'
            ;;
        addToParentalControlCategories|removeFromParentalControlCategories)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "gambling"}'
            ;;
        addToParentalControlServices|removeFromParentalControlServices)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "tiktok"}'
            ;;
        updateAllowlistEntry|updateDenylistEntry)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, entry_id: "example.com", active: true}'
            ;;
        updateProfile)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, name: "Updated Test Profile"}'
            ;;
        updateSettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, blockPage: {enabled: true}}'
            ;;
        updateBlockPageSettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, enabled: true}'
            ;;
        updateLogsSettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, enabled: true, retention: 1}'
            ;;
        updatePerformanceSettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, ecs: true, cache: true}'
            ;;
        updatePrivacySettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, disguisedTrackers: true, allowAffiliate: false}'
            ;;
        updateSecuritySettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, threatIntelligenceFeeds: true, googleSafeBrowsing: true}'
            ;;
        updateParentalControlSettings)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, safeSearch: true, youtubeRestrictedMode: true}'
            ;;
        updateParentalControlCategoryEntry)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "gambling", active: true}'
            ;;
        updateParentalControlServiceEntry)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, id: "tiktok", active: true}'
            ;;
        updateAllowlist)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, entries: "[\"test1.com\",\"test2.com\"]"}'
            ;;
        updateDenylist)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, entries: "[\"block1.com\",\"block2.com\"]"}'
            ;;
        updatePrivacyBlocklists)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, blocklists: "[\"nextdns-recommended\",\"oisd\"]"}'
            ;;
        updatePrivacyNatives)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, natives: "[\"apple\",\"windows\"]"}'
            ;;
        updateSecurityTlds)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, tlds: "[\"zip\",\"mov\"]"}'
            ;;
        updateParentalControlCategories)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, categories: "[\"gambling\",\"dating\"]"}'
            ;;
        updateParentalControlServices)
            jq -n --arg pid "${PROFILE_ID}" '{profile_id: $pid, services: "[\"tiktok\",\"fortnite\"]"}'
            ;;
        deleteProfile)
            jq -n '{profile_id: "dummy-profile-id"}'
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
    
    # Get test arguments as JSON
    TOOL_ARGS_JSON=$(get_tool_args "${TOOL_NAME}")
    
    # Execute tool and capture result
    START_TIME=$(date +%s)
    set +e
    TOOL_OUTPUT=$(docker mcp tools call "${TOOL_NAME}" "${TOOL_ARGS_JSON}" 2>&1)
    EXIT_CODE=$?
    set -e
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    
    # Parse output and status
    if [ ${EXIT_CODE} -eq 0 ]; then
        log_success "${TOOL_NAME}: OK"
        
        # Record successful execution
        jq -n \
            --arg tool "${TOOL_NAME}" \
            --arg status "OK" \
            --arg args "${TOOL_ARGS_JSON}" \
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
            --arg args "${TOOL_ARGS_JSON}" \
            --arg error "${ERROR_MSG}" \
            --arg exit_code "${EXIT_CODE}" \
            --arg duration "${DURATION}s" \
            '{tool: $tool, status: $status, args: $args, error: $error, exit_code: ($exit_code | tonumber), duration: $duration, timestamp: now | todate}' \
            >>"${REPORT_FILE}"
        
        FAILED_COUNT=$((FAILED_COUNT + 1))
    fi
done

# Summary
log_info "================================"
log_info "Execution Summary"
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
