#!/usr/bin/env bash
#
# gateway_e2e_run.sh - End-to-end validation of NextDNS MCP Gateway
#
# This script performs a complete E2E test of the Docker MCP Gateway:
# 1. Loads configuration from .env file
# 2. Builds the Docker image
# 3. Imports the catalog.yaml
# 4. Configures gateway args for legacy catalog mode (replaces 'server enable')
# 5. Configures API key secret
# 6. Runs all tools via run_all_tools.sh
# 7. Cleans up gateway configuration
#
# Usage:
#   ./gateway_e2e_run.sh [env_file]
#
# Arguments:
#   env_file - Path to environment file (default: .env)
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
VARIANT="${2:-slim}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
ARTIFACTS_DIR="${PROJECT_DIR}/artifacts"
TEMP_CATALOG=""  # Initialized early so cleanup can safely reference it

# Validate variant
case "${VARIANT}" in
    slim|alpine)
        ;;
    *)
        log_error "Invalid variant: ${VARIANT}"
        log_error "Usage: $0 [env_file] [slim|alpine]"
        exit 1
        ;;
esac

# Determine Dockerfile and image tag based on variant
if [ "${VARIANT}" = "alpine" ]; then
    DOCKERFILE="Dockerfile.alpine"
else
    DOCKERFILE="Dockerfile"
fi
IMAGE_TAG="nextdns-mcp:${VARIANT}"

# Load environment file
if [ -z "${ENV_FILE}" ]; then
    if [ -f "${PROJECT_DIR}/.env" ]; then
        ENV_FILE="${PROJECT_DIR}/.env"
        log_info "Using default .env file"
    else
        log_error "No .env file found"
        log_error "Please create .env file (copy from .env.example)"
        exit 1
    fi
else
    # Resolve relative paths against the project directory first, then cwd
    if [ ! -f "${ENV_FILE}" ] && [ ! -e "${ENV_FILE}" ]; then
        if [ -f "${PROJECT_DIR}/${ENV_FILE}" ]; then
            ENV_FILE="${PROJECT_DIR}/${ENV_FILE}"
        fi
    fi
    if command -v realpath >/dev/null 2>&1; then
        ENV_FILE="$(realpath "${ENV_FILE}")"
    else
        # Fallback for macOS and minimal systems without realpath
        ENV_FILE="$(cd "$(dirname "${ENV_FILE}")" && pwd)/$(basename "${ENV_FILE}")"
    fi
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
log_info "Variant: ${VARIANT}"
log_info "================================"
log_info "Allow writes: ${ALLOW_LIVE_WRITES}"
log_info "Artifacts: ${ARTIFACTS_DIR}"

# Ensure artifacts directory exists
mkdir -p "${ARTIFACTS_DIR}"

# Cleanup function
cleanup() {
    log_info ""
    
    # Cleanup validation profile if created
    if [ -f "${ARTIFACTS_DIR}/test_profile_id.txt" ]; then
        VALIDATION_PROFILE=$(cat "${ARTIFACTS_DIR}/test_profile_id.txt")
        log_info "Validation profile created: ${VALIDATION_PROFILE}"
        
            # Non-interactive cleanup for CI or when ALLOW_LIVE_WRITES is false
            # If CI=true, or ALLOW_LIVE_WRITES is not "true", perform auto-delete (best-effort)
            if [ "${CI:-false}" = "true" ] || [ "${ALLOW_LIVE_WRITES}" != "true" ]; then
                log_info "Auto-deleting validation profile (CI or non-writes mode)"
                docker mcp tools --gateway-arg="--catalog=${TEMP_CATALOG:-${CATALOG_NAME:-nextdns-mcp}.yaml}" --gateway-arg="--servers=${SERVER_NAME:-nextdns}" call manageProfiles "operation=delete" "profile_id=${VALIDATION_PROFILE}" \
                    >/dev/null 2>&1 || log_warn "Failed to delete validation profile"
                log_success "Validation profile deletion attempted"
            else
                read -p "Delete validation profile ${VALIDATION_PROFILE}? (yes/no): " -r
                echo
                if [[ $REPLY = "yes" ]]; then
                    log_info "Deleting validation profile..."
                    docker mcp tools --gateway-arg="--catalog=${TEMP_CATALOG:-${CATALOG_NAME:-nextdns-mcp}.yaml}" --gateway-arg="--servers=${SERVER_NAME:-nextdns}" call manageProfiles "operation=delete" "profile_id=${VALIDATION_PROFILE}" \
                        >/dev/null 2>&1 || log_warn "Failed to delete validation profile"
                    log_success "Validation profile deleted"
                else
                    log_info "Keeping validation profile for manual inspection"
                fi
            fi
        
        rm -f "${ARTIFACTS_DIR}/test_profile_id.txt"
    fi
    
    # Cleanup injected catalog temp file (after profile deletion which may use gateway)
    if [ -n "${TEMP_CATALOG}" ] && [ -f "${TEMP_CATALOG}" ]; then
        rm -f "${TEMP_CATALOG}"
    fi
    
    log_success "Cleanup complete"
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

# Step 1: Build Docker image
log_info ""
log_info "Step 1: Building Docker image (${VARIANT})..."

cd "${PROJECT_DIR}"
if docker build -f "${DOCKERFILE}" -t "${IMAGE_TAG}" . >/dev/null 2>&1; then
    log_success "Docker image built (${IMAGE_TAG})"
else
    log_error "Failed to build Docker image (${IMAGE_TAG})"
    exit 1
fi

# Step 2: Prepare catalog with API key (CI-specific)
log_info ""
log_info "Step 2: Preparing catalog..."

# Copy catalog to temp location (must resolve within ~/.docker/mcp/catalogs/ to pass Docker MCP validation checks)
mkdir -p "$HOME/.docker/mcp/catalogs"
TEMP_CATALOG="$HOME/.docker/mcp/catalogs/catalog-temp.yaml"
cp "${PROJECT_DIR}/catalog.yaml" "${TEMP_CATALOG}"

# Determine whether to inject the API key directly into the catalog env section.
#
# Rationale:
# - On Linux (no Docker Desktop), the Docker MCP secrets engine may be unavailable.
# - When secrets are unavailable, the server starts without NEXTDNS_API_KEY and all tool calls fail with authRequired.
#
# Injection keeps credentials ephemeral (catalog is copied to artifacts, imported, then deleted).
INJECT_API_KEY="${CI:-false}"
if [ "${INJECT_API_KEY}" != "true" ]; then
    if [ ! -f "$HOME/.docker/mcp/secrets.env" ]; then
        if ! docker mcp secret ls >/dev/null 2>&1; then
            INJECT_API_KEY="true"
            log_warn "Docker MCP secrets engine unavailable and no file-based secrets detected; injecting NEXTDNS_API_KEY into catalog env for this run"
        fi
    fi
fi

# Always point the temp catalog at the per-variant image built by this script.
# Conditionally inject the API key when the Docker MCP secrets engine is unavailable.
log_info "Patching catalog image to ${IMAGE_TAG}"
if (
    cd "${PROJECT_DIR}" \
    && TEMP_CATALOG="${TEMP_CATALOG}" NEXTDNS_API_KEY="${NEXTDNS_API_KEY}" IMAGE_TAG="${IMAGE_TAG}" INJECT_API_KEY="${INJECT_API_KEY}" uv run python3 - <<'PY'
import os
import sys

import yaml

temp_catalog = os.environ["TEMP_CATALOG"]
image_tag = os.environ["IMAGE_TAG"]
inject_api_key = os.environ.get("INJECT_API_KEY") == "true"

with open(temp_catalog, "r", encoding="utf-8") as f:
    catalog = yaml.safe_load(f)

registry = catalog.get("registry", {}) if isinstance(catalog, dict) else {}
nextdns = registry.get("nextdns") if isinstance(registry, dict) else None
if not isinstance(nextdns, dict):
    sys.exit(1)

# Point the catalog at the per-variant image built by this script
nextdns["image"] = image_tag

if inject_api_key:
    api_key = os.environ["NEXTDNS_API_KEY"]
    env_list = nextdns.setdefault("env", [])
    if not isinstance(env_list, list):
        sys.exit(1)

    for env_var in env_list:
        if isinstance(env_var, dict) and env_var.get("name") == "NEXTDNS_API_KEY":
            env_var["value"] = api_key
            env_var["description"] = env_var.get("description") or "NextDNS API key (injected at runtime)"
            break
    else:
        env_list.insert(
            0,
            {
                "name": "NEXTDNS_API_KEY",
                "value": api_key,
                "description": "NextDNS API key (injected at runtime)",
            },
        )

    # Strip secrets declarations so docker-mcp v0.42+ does not generate unresolvable
    # se:// fallback URIs when the Docker Desktop secrets engine is absent (CI/Linux).
    # The env: injection above is the sole source of NEXTDNS_API_KEY in this mode.
    nextdns.pop("secrets", None)

with open(temp_catalog, "w", encoding="utf-8") as f:
    yaml.dump(catalog, f, default_flow_style=False, sort_keys=False)
PY
); then
    if [ "${INJECT_API_KEY}" = "true" ]; then
        log_success "Catalog patched and API key injected"
    else
        log_success "Catalog patched to use ${IMAGE_TAG}"
    fi
else
    log_error "Failed to patch catalog"
    rm -f "${TEMP_CATALOG}"
    exit 1
fi

# Import catalog (also keeps the injected file available for direct-path gateway access)
log_info "Importing catalog..."
if docker mcp catalog import "${TEMP_CATALOG}" >/dev/null 2>&1; then
    log_success "Catalog imported"
else
    log_error "Failed to import catalog"
    rm -f "${TEMP_CATALOG}"
    exit 1
fi

# Step 3: Configure additional environment variables (if not in CI)
log_info ""
log_info "Step 3: Configuring additional environment variables..."

# In CI, all env vars are already in the catalog, so skip this step
if [ "${CI:-false}" != "true" ]; then
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
        log_warn "docker mcp config write unavailable — using catalog defaults for env vars"
        rm -f "${ARTIFACTS_DIR}/config-temp.yaml"
    fi
else
    log_success "CI mode - all configuration in catalog"
fi

# Step 4: Configure gateway args for legacy catalog mode (AFTER config is set)
#
# Note: Recent versions of docker-mcp always enable the profiles feature, which makes
# 'docker mcp server enable' an obsolete command that exits with an error.  We bypass
# the profiles system by passing --catalog / --servers gateway args to every tool call,
# which forces the gateway into legacy catalog mode regardless of the profiles setting.
log_info ""
log_info "Step 4: Configuring server..."

CATALOG_NAME="nextdns-mcp"
SERVER_NAME="nextdns"
# Use the absolute path of the injected catalog file so the gateway reads it directly
# (bypasses ~/.docker/mcp/catalogs/ lookup which can fail silently in CI environments)
export NEXTDNS_GATEWAY_ARGS="--catalog=${TEMP_CATALOG} --servers=${SERVER_NAME}"
log_success "Server configured (catalog: ${CATALOG_NAME}, server: ${SERVER_NAME})"

# Debug: Show API key length (not the actual key)
log_info "API key length: ${#NEXTDNS_API_KEY} characters"

# Check if we're using injected env, CI/file-based secrets, or Docker Desktop secrets.
if [ "${INJECT_API_KEY:-false}" = "true" ]; then
    log_info "API key provided via catalog env injection"
    log_success "API key configured via catalog env"
elif [ "${CI:-false}" = "true" ]; then
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

# Step 5: Wait for server readiness
log_info ""
log_info "Step 5: Waiting for server readiness..."

MAX_ATTEMPTS=30
ATTEMPT=0
while [ ${ATTEMPT} -lt ${MAX_ATTEMPTS} ]; do
    TOOL_COUNT=$(docker mcp tools --gateway-arg="--catalog=${TEMP_CATALOG}" --gateway-arg="--servers=${SERVER_NAME}" ls 2>/dev/null | grep -c '^ - ' || true)
    if [ "${TOOL_COUNT}" -gt 0 ]; then
        log_success "Server is ready (${TOOL_COUNT} tools available)"
        break
    fi
    
    ATTEMPT=$((ATTEMPT + 1))
    if [ ${ATTEMPT} -ge ${MAX_ATTEMPTS} ]; then
        log_error "Server readiness timeout — no tools found after ${MAX_ATTEMPTS} attempts"
        log_error "Catalog: ${TEMP_CATALOG}"
        log_error "Debug output:"
        docker mcp tools --gateway-arg="--catalog=${TEMP_CATALOG}" --gateway-arg="--servers=${SERVER_NAME}" ls 2>&1 | head -20 >&2 || true
        cat "${TEMP_CATALOG}" | head -30 >&2 || true
        exit 1
    fi
    
    sleep 1
done

# Step 6: Run all tools
log_info ""
log_info "Step 6: Running all tools..."

if bash "${SCRIPT_DIR}/run_all_tools.sh" "${ALLOW_LIVE_WRITES}" "${VARIANT}"; then
    log_success "E2E test completed successfully (${VARIANT})"
    exit 0
else
    log_error "E2E test failed: Tool execution failed (${VARIANT})"
    exit 1
fi
