#!/usr/bin/env bash
#
# gateway_e2e_run.sh - End-to-end validation of NextDNS MCP Gateway
#
# This script performs a complete E2E test of the Docker MCP Gateway:
# 1. Loads configuration from .env file
# 2. Builds the Docker image
# 3. Imports the catalog.yaml
# 4. Enables the NextDNS server
# 5. Configures API key secret
# 6. Runs all tools via run_all_tools.sh
# 7. Cleans up gateway configuration
#
# Usage:
#   ./gateway_e2e_run.sh [env_file]
#
# Arguments:
#   env_file - Path to environment file (default: .env, fallback: .env.test.example)
#
# Environment Variables:
#   NEXTDNS_API_KEY - Required: NextDNS API key
#   ALLOW_LIVE_WRITES - Enable write operations (default: false)
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
ENV_FILE="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
ARTIFACTS_DIR="${PROJECT_DIR}/artifacts"

# Load environment file
if [ -z "${ENV_FILE}" ]; then
    if [ -f "${PROJECT_DIR}/.env" ]; then
        ENV_FILE="${PROJECT_DIR}/.env"
        log_info "Using default .env file"
    elif [ -f "${PROJECT_DIR}/.env.test.example" ]; then
        ENV_FILE="${PROJECT_DIR}/.env.test.example"
        log_warn "Using .env.test.example as fallback"
    else
        log_error "No environment file found"
        log_error "Please create .env or provide env file path as argument"
        exit 1
    fi
else
    ENV_FILE="$(realpath "${ENV_FILE}")"
fi

if [ ! -f "${ENV_FILE}" ]; then
    log_error "Environment file not found: ${ENV_FILE}"
    exit 1
fi

log_info "Loading environment from: ${ENV_FILE}"

# Export variables from env file
set -a
source "${ENV_FILE}"
set +a

# Validate required variables
if [ -z "${NEXTDNS_API_KEY:-}" ] || [ "${NEXTDNS_API_KEY}" = "your-api-key-here" ]; then
    log_error "NEXTDNS_API_KEY is not set or is the default placeholder"
    log_error "Please set your NextDNS API key in ${ENV_FILE}"
    exit 1
fi

# Set defaults
ALLOW_LIVE_WRITES="${ALLOW_LIVE_WRITES:-false}"

log_info "================================"
log_info "NextDNS MCP Gateway E2E Test"
log_info "================================"
log_info "Allow writes: ${ALLOW_LIVE_WRITES}"
log_info "Artifacts: ${ARTIFACTS_DIR}"

# Ensure artifacts directory exists
mkdir -p "${ARTIFACTS_DIR}"

# Cleanup function
cleanup() {
    log_info ""
    
    # Cleanup validation profile if created
    if [ -f "${ARTIFACTS_DIR}/validation_profile_id.txt" ]; then
        VALIDATION_PROFILE=$(cat "${ARTIFACTS_DIR}/validation_profile_id.txt")
        log_info "Validation profile created: ${VALIDATION_PROFILE}"
        
            # Non-interactive cleanup for CI or when ALLOW_LIVE_WRITES is false
            # If CI=true, or ALLOW_LIVE_WRITES is not "true", perform auto-delete (best-effort)
            if [ "${CI:-false}" = "true" ] || [ "${ALLOW_LIVE_WRITES}" != "true" ]; then
                log_info "Auto-deleting validation profile (CI or non-writes mode)"
                docker mcp tools call deleteProfile "profile_id=${VALIDATION_PROFILE}" \
                    >/dev/null 2>&1 || log_warn "Failed to delete validation profile"
                log_success "Validation profile deletion attempted"
            else
                read -p "Delete validation profile ${VALIDATION_PROFILE}? (yes/no): " -r
                echo
                if [[ $REPLY = "yes" ]]; then
                    log_info "Deleting validation profile..."
                    docker mcp tools call deleteProfile "profile_id=${VALIDATION_PROFILE}" \
                        >/dev/null 2>&1 || log_warn "Failed to delete validation profile"
                    log_success "Validation profile deleted"
                else
                    log_info "Keeping validation profile for manual inspection"
                fi
            fi
        
        rm -f "${ARTIFACTS_DIR}/validation_profile_id.txt"
    fi
    
    log_success "Cleanup complete"
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

# Step 1: Build Docker image
log_info ""
log_info "Step 1: Building Docker image..."

cd "${PROJECT_DIR}"
if docker build -t nextdns-mcp:latest . >/dev/null 2>&1; then
    log_success "Docker image built"
else
    log_error "Failed to build Docker image"
    exit 1
fi

# Step 2: Import catalog
log_info ""
log_info "Step 2: Importing catalog..."

# Copy catalog to temp location for import
TEMP_CATALOG="${ARTIFACTS_DIR}/catalog-temp.yaml"
cp "${PROJECT_DIR}/catalog.yaml" "${TEMP_CATALOG}"

if docker mcp catalog import "${TEMP_CATALOG}" >/dev/null 2>&1; then
    log_success "Catalog imported"
    rm -f "${TEMP_CATALOG}"
else
    log_error "Failed to import catalog"
    rm -f "${TEMP_CATALOG}"
    exit 1
fi

# Step 3: Enable server
log_info ""
log_info "Step 3: Enabling server..."

if docker mcp server enable nextdns >/dev/null 2>&1; then
    log_success "Server enabled"
else
    log_error "Failed to enable server"
    exit 1
fi

# Step 4: Configure API key secret
log_info ""
log_info "Step 4: Configuring API key secret..."

# Debug: Show API key length (not the actual key)
log_info "API key length: ${#NEXTDNS_API_KEY} characters"

# Check if we're in CI or if file-based secrets already exist
if [ "${CI:-false}" = "true" ]; then
    log_info "CI environment detected - using file-based secrets from ~/.docker/mcp/secrets.env"
    log_success "API key configured via file-based secrets"
elif [ -f "$HOME/.docker/mcp/secrets.env" ]; then
    log_info "File-based secrets detected at ~/.docker/mcp/secrets.env"
    log_success "API key configured via file-based secrets"
else
    # Try to set the secret via Docker Desktop API, capture any errors
    if SECRET_OUTPUT=$(echo "${NEXTDNS_API_KEY}" | docker mcp secret set nextdns.api_key 2>&1); then
        log_success "API key configured via Docker Desktop"
    else
        log_error "Failed to configure API key via Docker Desktop"
        log_error "Docker MCP output: ${SECRET_OUTPUT}"
        exit 1
    fi
fi

# Step 4.5: Configure environment variables
log_info ""
log_info "Step 4.5: Configuring environment variables..."

# Set NEXTDNS_READABLE_PROFILES if provided
if [ -n "${NEXTDNS_READABLE_PROFILES:-}" ]; then
    READABLE_PROFILES="${NEXTDNS_READABLE_PROFILES}"
else
    READABLE_PROFILES="ALL"
fi

# Set NEXTDNS_WRITABLE_PROFILES if provided
if [ -n "${NEXTDNS_WRITABLE_PROFILES:-}" ]; then
    WRITABLE_PROFILES="${NEXTDNS_WRITABLE_PROFILES}"
else
    WRITABLE_PROFILES="ALL"
fi

# Create config YAML for environment variables
cat > "${ARTIFACTS_DIR}/config-temp.yaml" <<EOF
nextdns:
  env:
    NEXTDNS_READABLE_PROFILES: "${READABLE_PROFILES}"
    NEXTDNS_WRITABLE_PROFILES: "${WRITABLE_PROFILES}"
    NEXTDNS_READ_ONLY: "false"
EOF

# Write config
if docker mcp config write "$(cat "${ARTIFACTS_DIR}/config-temp.yaml")" >/dev/null 2>&1; then
    log_success "Environment variables configured"
    rm -f "${ARTIFACTS_DIR}/config-temp.yaml"
else
    log_error "Failed to configure environment variables"
    rm -f "${ARTIFACTS_DIR}/config-temp.yaml"
    exit 1
fi

# Step 5: Wait for server readiness
log_info ""
log_info "Step 5: Waiting for server readiness..."

MAX_ATTEMPTS=30
ATTEMPT=0
while [ ${ATTEMPT} -lt ${MAX_ATTEMPTS} ]; do
    if docker mcp tools ls >/dev/null 2>&1; then
        log_success "Server is ready"
        break
    fi
    
    ATTEMPT=$((ATTEMPT + 1))
    if [ ${ATTEMPT} -ge ${MAX_ATTEMPTS} ]; then
        log_error "Server readiness timeout"
        log_error "Check logs: docker mcp logs"
        exit 1
    fi
    
    sleep 1
done

# Step 6: Run all tools
log_info ""
log_info "Step 6: Running all tools..."

if bash "${SCRIPT_DIR}/run_all_tools.sh" "${ALLOW_LIVE_WRITES}"; then
    log_success "E2E test completed successfully"
    exit 0
else
    log_error "E2E test failed: Tool execution failed"
    exit 1
fi
