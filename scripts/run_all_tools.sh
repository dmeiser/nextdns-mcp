#!/usr/bin/env bash
#
# run_all_tools.sh - Execute the grouped NextDNS MCP tools via Docker MCP Gateway
#
# This script exercises the small set of domain-grouped CRUD tools that replace
# the ~80 atomic OpenAPI-generated tools.
#
# Usage:
#   ./run_all_tools.sh [allow_live_writes] [variant]
#
# Arguments:
#   allow_live_writes - Enable write operations and profile creation (default: false)
#   variant           - Docker image variant to test: slim or alpine (default: slim)
#
# Environment:
#   ALLOW_LIVE_WRITES - Alternative way to enable writes (default: false)
#   NEXTDNS_PLOT_PROFILE - Profile with analytics data for plotting tools
#
# Output:
#   - Console: Colored progress output with step markers
#   - File: artifacts/tools_report_<variant>.jsonl (one JSON object per line per call)
#
# Exit Codes:
#   0 - All executed calls succeeded
#   1 - One or more calls failed or script error
#

# Do not use "set -e". execute_call is intentionally allowed to fail so that the
# script can continue exercising the remaining tools and report a summary. Critical
# setup steps explicitly check their exit codes and abort when required.
set -uo pipefail

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
ALLOW_LIVE_WRITES="${ALLOW_LIVE_WRITES:-${1:-false}}"
VARIANT="${2:-slim}"

# Validate variant to prevent unexpected report paths or image selections
if [ "${VARIANT}" != "slim" ] && [ "${VARIANT}" != "alpine" ]; then
    log_error "Invalid variant '${VARIANT}'. Must be 'slim' or 'alpine'."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
ARTIFACTS_DIR="${PROJECT_DIR}/artifacts"
REPORT_FILE="${ARTIFACTS_DIR}/tools_report_${VARIANT}.jsonl"

# Ensure artifacts directory exists
mkdir -p "${ARTIFACTS_DIR}"

# Build gateway-arg flags for legacy catalog mode.
# NEXTDNS_GATEWAY_ARGS is set by gateway_e2e_run.sh; falls back to empty so
# this script can still be invoked standalone against a pre-configured default profile.
_MCP_GATEWAY_ARGS=()
if [ -n "${NEXTDNS_GATEWAY_ARGS:-}" ]; then
    for _arg in ${NEXTDNS_GATEWAY_ARGS}; do
        _MCP_GATEWAY_ARGS+=("--gateway-arg=${_arg}")
    done
fi

mcp_tools() {
    docker mcp tools "${_MCP_GATEWAY_ARGS[@]}" "$@"
}

# Check whether an analytics metric has non-empty time-series data.
metric_has_series() {
    local profile_id="$1"
    local from_timestamp="$2"
    local metric="$3"
    local output
    local json_output
    local times_len
    local extra_args=()

    if [ "${metric}" = "destinations" ]; then
        extra_args=("destination_type=countries")
    fi

    output=$(mcp_tools call --format json queryAnalytics "metric=${metric}" "profile_id=${profile_id}" "series=true" "from_time=${from_timestamp}" "${extra_args[@]}" 2>&1 || true)
    json_output=$(echo "${output}" | grep -E '^\{' | head -1 || echo "")
    if [ -z "${json_output}" ]; then
        return 1
    fi
    times_len=$(echo "${json_output}" | jq -r '.meta.series.times | length // 0' 2>/dev/null || echo 0)
    if [ "${times_len}" -gt 0 ]; then
        return 0
    fi
    return 1
}

# Validate that a plotting tool returned a non-empty ImageContent payload rather
# than a JSON error object. The Docker MCP CLI renders ImageContent as a Go
# struct containing a non-empty byte slice; JSON payloads indicate an error.
validate_plot_tool_output() {
    local tool_name="$1"
    local output="$2"
    local json_output

    json_output=$(echo "${output}" | grep -E '^\{' | head -1 || echo "")
    if [ -n "${json_output}" ] && echo "${json_output}" | jq -e '.error' >/dev/null 2>&1; then
        log_error "  ${tool_name} returned an error payload: ${json_output}"
        return 1
    fi

    # Non-empty image: Go repr contains a byte slice with at least one byte value.
    if echo "${output}" | grep -qE '&\{map\[\] <nil> \[[0-9]'; then
        return 0
    fi

    log_error "  ${tool_name} did not return a non-empty image"
    return 1
}

log_info "Starting NextDNS MCP grouped tools execution"
log_info "Variant: ${VARIANT}"
log_info "Allow live writes: ${ALLOW_LIVE_WRITES}"
log_info "Report file: ${REPORT_FILE}"

# Clear previous report
>"${REPORT_FILE}"

# Preflight checks
log_info "Performing preflight checks..."

# Check Docker MCP is responding
if ! mcp_tools ls >/dev/null 2>&1; then
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
mapfile -t ALL_TOOLS < <(docker mcp tools "${_MCP_GATEWAY_ARGS[@]}" ls 2>/dev/null | grep '^ - ' | awk '{print $2}')

if [ ${#ALL_TOOLS[@]} -eq 0 ]; then
    log_error "No tools found"
    log_error "The MCP server may not be properly configured"
    log_error "Run: docker mcp catalog import ./catalog.yaml"
    log_error "Run: scripts/gateway_e2e_run.sh to configure the server"
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

# Verify the server exposes exactly the grouped tool set
EXPECTED_TOOLS=(dohLookup manageLists manageLogs manageProfiles manageRewrites manageSettings plotAnalytics queryAnalytics)
MISSING_TOOLS=()
for EXPECTED in "${EXPECTED_TOOLS[@]}"; do
    if ! printf '%s\n' "${TOOL_NAMES[@]}" | grep -qx "${EXPECTED}"; then
        MISSING_TOOLS+=("${EXPECTED}")
    fi
done
if [ ${#MISSING_TOOLS[@]} -gt 0 ]; then
    log_error "Missing expected grouped tools: ${MISSING_TOOLS[*]}"
    exit 1
fi
if [ "${TOOL_COUNT}" -ne "${#EXPECTED_TOOLS[@]}" ]; then
    log_error "Expected ${#EXPECTED_TOOLS[@]} NextDNS tools, found ${TOOL_COUNT}: ${TOOL_NAMES[*]}"
    exit 1
fi
log_success "Tool registry exposes the expected grouped tools"

# Step 1: Profile setup
CREATED_PROFILE_ID=""
if [ "${ALLOW_LIVE_WRITES}" = "true" ]; then
    log_info "Step 1: Creating test profile..."
    TIMESTAMP=$(date +%s)
    PROFILE_NAME="E2E Test Profile ${TIMESTAMP}"

    set +e
    PROFILE_RESULT=$(mcp_tools call --format json manageProfiles "operation=create" "name=${PROFILE_NAME}" 2>&1)
    CREATE_EXIT_CODE=$?
    CREATED_PROFILE_ID=$(echo "${PROFILE_RESULT}" | grep -E '^\{' | jq -r '.data.id // .id // empty' 2>/dev/null || echo "")

    if [ ${CREATE_EXIT_CODE} -eq 0 ] && [ -n "${CREATED_PROFILE_ID}" ] && [ "${CREATED_PROFILE_ID}" != "null" ]; then
        log_success "Created test profile: ${CREATED_PROFILE_ID}"
        echo "${CREATED_PROFILE_ID}" >"${ARTIFACTS_DIR}/test_profile_id.txt"

        jq -n \
            --arg tool "manageProfiles" \
            --arg status "OK" \
            --arg profile_id "${CREATED_PROFILE_ID}" \
            --arg profile_name "${PROFILE_NAME}" \
            '{tool: $tool, status: $status, profile_id: $profile_id, profile_name: $profile_name, phase: "setup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"

        PROFILE_ID="${CREATED_PROFILE_ID}"
    else
        log_error "Failed to create test profile"
        log_error "Response: ${PROFILE_RESULT}"

        jq -n \
            --arg tool "manageProfiles" \
            --arg status "FAILED" \
            --arg error "${PROFILE_RESULT}" \
            '{tool: $tool, status: $status, error: $error, phase: "setup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"

        exit 1
    fi
else
    log_info "Step 1: Skipping profile creation (ALLOW_LIVE_WRITES=false)"

    log_info "Fetching existing profile for read-only tests..."
    PROFILES_RESULT=$(mcp_tools call --format json manageProfiles "operation=list" 2>&1 | grep -E '^\{' || echo "")

    if [ -z "${PROFILES_RESULT}" ]; then
        log_error "Failed to fetch profiles"
        exit 1
    fi

    PROFILE_ID=$(echo "${PROFILES_RESULT}" | jq -r '.data[0].id' 2>/dev/null || echo "")

    if [ -z "${PROFILE_ID}" ] || [ "${PROFILE_ID}" = "null" ]; then
        log_error "No profiles available for testing"
        log_error "Run with ALLOW_LIVE_WRITES=true to create a test profile"
        exit 1
    fi

    log_success "Using existing profile: ${PROFILE_ID}"
fi

PLOT_PROFILE_ID="${NEXTDNS_PLOT_PROFILE:-${PROFILE_ID}}"
FROM_TIMESTAMP=$(date -d '1 day ago' +%s 2>/dev/null || date -v-1d +%s 2>/dev/null || echo "1704067200")
HOURS_AGO_TIMESTAMP=$(date -d '1 hour ago' +%s 2>/dev/null || date -v-1H +%s 2>/dev/null || echo "$(date +%s)")

if [ -n "${NEXTDNS_PLOT_PROFILE:-}" ]; then
    if metric_has_series "${PLOT_PROFILE_ID}" "${FROM_TIMESTAMP}" "status"; then
        log_success "Plot profile ${PLOT_PROFILE_ID} has analytics data"
    else
        log_error "NEXTDNS_PLOT_PROFILE=${PLOT_PROFILE_ID} has no analytics series data"
        log_error "Set NEXTDNS_PLOT_PROFILE to a profile with query history, or unset it to skip plotting tools"
        exit 1
    fi
else
    log_warn "NEXTDNS_PLOT_PROFILE not set; plot tools will be skipped to avoid false positives from empty charts"
fi

# Execution tracking
EXECUTED_COUNT=0
FAILED_COUNT=0
SKIPPED_COUNT=0
SCHEMA_ERRORS=0

# Record a call result to the report and update counters.
record_result() {
    local tool="$1"
    local status="$2"
    local schema_status="${3:-SKIPPED}"
    local schema_error="${4:-}"
    local args="${5:-}"
    local duration="${6:-0}"

    jq -n \
        --arg tool "${tool}" \
        --arg status "${status}" \
        --arg schema_status "${schema_status}" \
        --arg schema_error "${schema_error}" \
        --arg args "${args}" \
        --arg duration "${duration}s" \
        '{tool: $tool, status: $status, schema_validation: $schema_status, schema_error: $schema_error, args: $args, duration: $duration, timestamp: now | todate}' \
        >>"${REPORT_FILE}"
}

# Record a skipped call to the report.
record_skip() {
    local tool="$1"
    local reason="$2"

    jq -n \
        --arg tool "${tool}" \
        --arg status "SKIPPED" \
        --arg reason "${reason}" \
        '{tool: $tool, status: $status, reason: $reason, timestamp: now | todate}' \
        >>"${REPORT_FILE}"
    SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
}

# Execute a single tool call with retry logic and schema validation.
execute_call() {
    local tool_name="$1"
    shift
    local args=("$@")
    local args_str
    args_str=$(printf '%s ' "${args[@]}")
    args_str="${args_str% }"

    log_info "Executing: ${tool_name} ${args_str}"

    local start_time end_time duration
    start_time=$(date +%s)

    local max_retries=3
    local retry_delay=5
    local attempt=1
    local exit_code=0
    local output=""

    while [ ${attempt} -le ${max_retries} ]; do
        set +e
        output=$(mcp_tools call --format json "${tool_name}" "${args[@]}" 2>&1)
        exit_code=$?

        if [ ${exit_code} -ne 0 ] && [ ${attempt} -lt ${max_retries} ]; then
            if echo "${output}" | grep -qiE "(Temporary failure in name resolution|network|timeout|connection|timed out|Request error:$)"; then
                log_warn "  Network error on attempt ${attempt}/${max_retries}, retrying in ${retry_delay}s..."
                sleep ${retry_delay}
                attempt=$((attempt + 1))
                continue
            fi
        fi
        break
    done

    end_time=$(date +%s)
    duration=$((end_time - start_time))

    # Some failures are returned as a 200-style JSON payload containing an
    # "error" key (e.g., access denied or validation errors). Treat those as
    # failures even when the Docker MCP CLI exits 0.
    local json_output
    json_output=$(echo "${output}" | grep -E '^\{' | head -1 || echo "")
    if [ -n "${json_output}" ] && echo "${json_output}" | jq -e '.error' >/dev/null 2>&1; then
        exit_code=1
    fi

    # Validate plotting tool image output
    if [ ${exit_code} -eq 0 ] && [ "${tool_name}" = "plotAnalytics" ]; then
        if ! validate_plot_tool_output "${tool_name}" "${output}"; then
            exit_code=1
        fi
    fi

    if [ ${exit_code} -eq 0 ]; then
        log_success "${tool_name}: OK"

        local schema_status="SKIPPED"
        local schema_error=""
        if [ -n "${json_output}" ] && [ "${json_output}" != '{"success":true}' ]; then
            local validation_result
            local python_cmd="python3"
            if command -v uv >/dev/null 2>&1; then
                python_cmd="uv run python"
            fi
            validation_result=$(${python_cmd} "${SCRIPT_DIR}/validate_schema.py" "${tool_name}" "${json_output}" 2>&1 || echo "SCHEMA_VALIDATION_FAILED")

            if echo "${validation_result}" | grep -q "VALID"; then
                schema_status="VALID"
            elif echo "${validation_result}" | grep -q "SKIPPED"; then
                schema_status="SKIPPED"
            else
                schema_status="INVALID"
                schema_error=$(echo "${validation_result}" | grep "SCHEMA_ERRORS" || echo "${validation_result}")
                log_warn "  Schema validation failed: ${schema_error}"
                SCHEMA_ERRORS=$((SCHEMA_ERRORS + 1))
            fi
        fi

        record_result "${tool_name}" "OK" "${schema_status}" "${schema_error}" "${args_str}" "${duration}"
        EXECUTED_COUNT=$((EXECUTED_COUNT + 1))
        return 0
    else
        log_error "${tool_name}: FAILED (exit code ${exit_code})"

        local error_msg
        error_msg=$(echo "${output}" | grep -Ei 'error' | head -1 || echo "Unknown error")

        jq -n \
            --arg tool "${tool_name}" \
            --arg status "FAILED" \
            --arg args "${args_str}" \
            --arg error "${error_msg}" \
            --arg exit_code "${exit_code}" \
            --arg duration "${duration}s" \
            '{tool: $tool, status: $status, args: $args, error: $error, exit_code: ($exit_code | tonumber), duration: $duration, timestamp: now | todate}' \
            >>"${REPORT_FILE}"

        FAILED_COUNT=$((FAILED_COUNT + 1))
        return 0
    fi
}

log_info "Step 2: Testing grouped tools with profile ${PROFILE_ID}..."

# Read-only calls (always executed)
execute_call manageProfiles operation=list
execute_call manageProfiles operation=get "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=general "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=privacy "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=security "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=parental "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=performance "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=logs "profile_id=${PROFILE_ID}"
execute_call manageSettings operation=get category=blockpage "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=allowlist operation=get "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=denylist operation=get "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=privacy_blocklists operation=get "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=privacy_natives operation=get "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=security_tlds operation=get "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=parental_categories operation=get "profile_id=${PROFILE_ID}"
execute_call manageLists list_type=parental_services operation=get "profile_id=${PROFILE_ID}"
execute_call manageRewrites operation=list "profile_id=${PROFILE_ID}"
execute_call manageLogs operation=get "profile_id=${PROFILE_ID}" "from_time=${HOURS_AGO_TIMESTAMP}" limit=10
execute_call manageLogs operation=download "profile_id=${PROFILE_ID}"
execute_call dohLookup domain=example.com "profile_id=${PROFILE_ID}" record_type=A

# Exercise every analytics aggregate metric exposed by the grouped tool.
QUERY_ANALYTICS_METRICS=(status domains queryTypes reasons ips dnssec encryption ipVersions protocols devices destinations)
for METRIC in "${QUERY_ANALYTICS_METRICS[@]}"; do
    if [ "${METRIC}" = "destinations" ]; then
        execute_call queryAnalytics metric=destinations "profile_id=${PLOT_PROFILE_ID}" "from_time=${FROM_TIMESTAMP}" destination_type=countries
    else
        execute_call queryAnalytics metric="${METRIC}" "profile_id=${PLOT_PROFILE_ID}" "from_time=${FROM_TIMESTAMP}"
    fi
done

# Exercise the time-series variants of analytics metrics (domains;series is a known API gap).
SERIES_METRICS=(status queryTypes reasons ips dnssec encryption ipVersions protocols devices destinations)
for METRIC in "${SERIES_METRICS[@]}"; do
    if [ "${METRIC}" = "destinations" ]; then
        execute_call queryAnalytics metric=destinations "profile_id=${PLOT_PROFILE_ID}" "from_time=${FROM_TIMESTAMP}" series=true destination_type=countries
    else
        execute_call queryAnalytics metric="${METRIC}" "profile_id=${PLOT_PROFILE_ID}" "from_time=${FROM_TIMESTAMP}" series=true
    fi
done

# Plot every supported metric that has series data available.
PLOT_METRICS=(status devices protocols queryTypes ipVersions dnssec encryption reasons ips)
if [ -n "${NEXTDNS_PLOT_PROFILE:-}" ]; then
    for METRIC in "${PLOT_METRICS[@]}"; do
        if metric_has_series "${PLOT_PROFILE_ID}" "${FROM_TIMESTAMP}" "${METRIC}"; then
            execute_call plotAnalytics metric="${METRIC}" "profile_id=${PLOT_PROFILE_ID}" "from_time=${FROM_TIMESTAMP}"
        else
            log_warn "Skipping plotAnalytics metric=${METRIC}: no series data available"
            record_skip "plotAnalytics" "metric=${METRIC}: no series data"
        fi
    done
else
    log_warn "Skipping plotAnalytics: NEXTDNS_PLOT_PROFILE not set"
    record_skip "plotAnalytics" "NEXTDNS_PLOT_PROFILE not set"
fi

# Write calls (only when live writes are enabled)
if [ "${ALLOW_LIVE_WRITES}" = "true" ]; then
    TIMESTAMP=$(date +%s)
    ALLOW_ENTRY="e2e-${TIMESTAMP}-allow.example.com"
    DENY_ENTRY="e2e-${TIMESTAMP}-deny.example.com"

    execute_call manageProfiles operation=update "profile_id=${PROFILE_ID}" name="Updated E2E Profile ${TIMESTAMP}"
    execute_call manageSettings operation=update category=general "profile_id=${PROFILE_ID}" 'settings={"web3":true}'
    execute_call manageSettings operation=update category=logs "profile_id=${PROFILE_ID}" 'settings={"enabled":true,"retention":86400}'
    execute_call manageSettings operation=update category=blockpage "profile_id=${PROFILE_ID}" 'settings={"enabled":true}'
    execute_call manageSettings operation=update category=performance "profile_id=${PROFILE_ID}" 'settings={"ecs":true,"cacheBoost":true}'
    execute_call manageSettings operation=update category=privacy "profile_id=${PROFILE_ID}" 'settings={"disguisedTrackers":true,"allowAffiliate":false}'
    execute_call manageSettings operation=update category=security "profile_id=${PROFILE_ID}" 'settings={"threatIntelligenceFeeds":true,"googleSafeBrowsing":true}'
    execute_call manageSettings operation=update category=parental "profile_id=${PROFILE_ID}" 'settings={"safeSearch":true,"youtubeRestrictedMode":true}'

    # allowlist: replace -> update -> remove -> add -> remove
    execute_call manageLists list_type=allowlist operation=replace "profile_id=${PROFILE_ID}" "entries=[{\"id\":\"${ALLOW_ENTRY}\"}]"
    execute_call manageLists list_type=allowlist operation=update "profile_id=${PROFILE_ID}" "entry_id=${ALLOW_ENTRY}" 'entry={"active":true}'
    execute_call manageLists list_type=allowlist operation=remove "profile_id=${PROFILE_ID}" "entry_id=${ALLOW_ENTRY}"
    execute_call manageLists list_type=allowlist operation=add "profile_id=${PROFILE_ID}" "entry={\"id\":\"${ALLOW_ENTRY}\"}"
    execute_call manageLists list_type=allowlist operation=remove "profile_id=${PROFILE_ID}" "entry_id=${ALLOW_ENTRY}"

    # denylist: replace -> update -> remove -> add -> remove
    execute_call manageLists list_type=denylist operation=replace "profile_id=${PROFILE_ID}" "entries=[{\"id\":\"${DENY_ENTRY}\"}]"
    execute_call manageLists list_type=denylist operation=update "profile_id=${PROFILE_ID}" "entry_id=${DENY_ENTRY}" 'entry={"active":true}'
    execute_call manageLists list_type=denylist operation=remove "profile_id=${PROFILE_ID}" "entry_id=${DENY_ENTRY}"
    execute_call manageLists list_type=denylist operation=add "profile_id=${PROFILE_ID}" "entry={\"id\":\"${DENY_ENTRY}\"}"
    execute_call manageLists list_type=denylist operation=remove "profile_id=${PROFILE_ID}" "entry_id=${DENY_ENTRY}"

    # privacy_blocklists: replace -> remove -> add -> remove
    execute_call manageLists list_type=privacy_blocklists operation=replace "profile_id=${PROFILE_ID}" 'entries=[{"id":"nextdns-recommended"}]'
    execute_call manageLists list_type=privacy_blocklists operation=remove "profile_id=${PROFILE_ID}" entry_id=nextdns-recommended
    execute_call manageLists list_type=privacy_blocklists operation=add "profile_id=${PROFILE_ID}" 'entry={"id":"nextdns-recommended"}'
    execute_call manageLists list_type=privacy_blocklists operation=remove "profile_id=${PROFILE_ID}" entry_id=nextdns-recommended

    # privacy_natives: replace -> remove -> add -> remove
    execute_call manageLists list_type=privacy_natives operation=replace "profile_id=${PROFILE_ID}" 'entries=[{"id":"apple"}]'
    execute_call manageLists list_type=privacy_natives operation=remove "profile_id=${PROFILE_ID}" entry_id=apple
    execute_call manageLists list_type=privacy_natives operation=add "profile_id=${PROFILE_ID}" 'entry={"id":"apple"}'
    execute_call manageLists list_type=privacy_natives operation=remove "profile_id=${PROFILE_ID}" entry_id=apple

    # security_tlds: replace -> remove -> add -> remove
    execute_call manageLists list_type=security_tlds operation=replace "profile_id=${PROFILE_ID}" 'entries=[{"id":"zip"}]'
    execute_call manageLists list_type=security_tlds operation=remove "profile_id=${PROFILE_ID}" entry_id=zip
    execute_call manageLists list_type=security_tlds operation=add "profile_id=${PROFILE_ID}" 'entry={"id":"zip"}'
    execute_call manageLists list_type=security_tlds operation=remove "profile_id=${PROFILE_ID}" entry_id=zip

    # parental_categories: replace -> update -> remove -> add -> remove
    execute_call manageLists list_type=parental_categories operation=replace "profile_id=${PROFILE_ID}" 'entries=[{"id":"gambling"}]'
    execute_call manageLists list_type=parental_categories operation=update "profile_id=${PROFILE_ID}" entry_id=gambling 'entry={"active":true}'
    execute_call manageLists list_type=parental_categories operation=remove "profile_id=${PROFILE_ID}" entry_id=gambling
    execute_call manageLists list_type=parental_categories operation=add "profile_id=${PROFILE_ID}" 'entry={"id":"gambling"}'
    execute_call manageLists list_type=parental_categories operation=remove "profile_id=${PROFILE_ID}" entry_id=gambling

    # parental_services: replace -> update -> remove -> add -> remove
    execute_call manageLists list_type=parental_services operation=replace "profile_id=${PROFILE_ID}" 'entries=[{"id":"tiktok"}]'
    execute_call manageLists list_type=parental_services operation=update "profile_id=${PROFILE_ID}" entry_id=tiktok 'entry={"active":true}'
    execute_call manageLists list_type=parental_services operation=remove "profile_id=${PROFILE_ID}" entry_id=tiktok
    execute_call manageLists list_type=parental_services operation=add "profile_id=${PROFILE_ID}" 'entry={"id":"tiktok"}'
    execute_call manageLists list_type=parental_services operation=remove "profile_id=${PROFILE_ID}" entry_id=tiktok

    # rewrites: add -> delete
    set +e
    REWRITE_CREATE_OUTPUT=$(mcp_tools call --format json manageRewrites operation=add "profile_id=${PROFILE_ID}" name="e2e-${TIMESTAMP}.example.com" content=192.0.2.1 2>&1)
    REWRITE_CREATE_EXIT=$?
    REWRITE_ENTRY_ID=$(echo "${REWRITE_CREATE_OUTPUT}" | grep -E '^\{' | jq -r '.data.id // empty' 2>/dev/null || echo "")
    if [ -n "${REWRITE_ENTRY_ID}" ]; then
        execute_call manageRewrites operation=delete "profile_id=${PROFILE_ID}" "entry_id=${REWRITE_ENTRY_ID}"
    else
        log_warn "Could not create rewrite entry for delete test (exit=${REWRITE_CREATE_EXIT}, output=${REWRITE_CREATE_OUTPUT})"
    fi

    execute_call manageLogs operation=clear "profile_id=${PROFILE_ID}"
else
    log_info "Skipping write operations (ALLOW_LIVE_WRITES=${ALLOW_LIVE_WRITES})"
fi

# Step 3: Clean up test profile if we created one
if [ -n "${CREATED_PROFILE_ID}" ]; then
    log_info "Step 3: Cleaning up test profile..."

    set +e
    DELETE_RESULT=$(mcp_tools call --format json manageProfiles operation=delete "profile_id=${CREATED_PROFILE_ID}" 2>&1)
    DELETE_EXIT_CODE=$?

    if [ ${DELETE_EXIT_CODE} -eq 0 ]; then
        log_success "Deleted test profile: ${CREATED_PROFILE_ID}"

        jq -n \
            --arg tool "manageProfiles" \
            --arg status "OK" \
            --arg profile_id "${CREATED_PROFILE_ID}" \
            '{tool: $tool, status: $status, profile_id: $profile_id, phase: "cleanup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"
    else
        log_error "Failed to delete test profile: ${CREATED_PROFILE_ID}"
        log_error "Response: ${DELETE_RESULT}"

        jq -n \
            --arg tool "manageProfiles" \
            --arg status "FAILED" \
            --arg profile_id "${CREATED_PROFILE_ID}" \
            --arg error "${DELETE_RESULT}" \
            '{tool: $tool, status: $status, profile_id: $profile_id, error: $error, phase: "cleanup", timestamp: now | todate}' \
            >>"${REPORT_FILE}"

        FAILED_COUNT=$((FAILED_COUNT + 1))
    fi

    rm -f "${ARTIFACTS_DIR}/test_profile_id.txt"
else
    log_info "Step 3: No test profile to clean up (read-only mode)"
fi

# Step 4: Report outcomes
log_info ""
log_info "================================"
log_info "Step 4: Execution Summary"
log_info "================================"
log_info "Expected NextDNS tools: ${#EXPECTED_TOOLS[@]}"
log_success "Executed calls: ${EXECUTED_COUNT}"
log_warn "Skipped: ${SKIPPED_COUNT}"
log_error "Failed: ${FAILED_COUNT}"
if [ ${SCHEMA_ERRORS} -gt 0 ]; then
    log_warn "Schema validation errors: ${SCHEMA_ERRORS}"
fi
log_info "Report: ${REPORT_FILE}"

if [ ${FAILED_COUNT} -gt 0 ]; then
    log_error "E2E test failed: Tool execution failed"
    exit 1
elif [ ${SCHEMA_ERRORS} -gt 0 ]; then
    log_error "E2E test failed: Schema validation errors detected"
    exit 1
else
    exit 0
fi
