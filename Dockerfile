# Multi-stage build for a lean final image

# 1. Builder stage: Install dependencies
FROM python:3.15-slim AS builder

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy only the dependency files: the export and install below read nothing else,
# so src/ stays out of the builder and its dependency layer cache
COPY pyproject.toml uv.lock ./

# Install dependencies into a version-independent directory
# --frozen: use exact versions from uv.lock without updating
# --no-dev: exclude development dependencies
# --no-emit-project: skip installing nextdns-mcp itself; the runtime stage copies
#   src/ onto PYTHONPATH directly
# Installing into /install keeps the layout free of a python3.x path component, so
# the runtime stage never has to name the builder's Python minor version.
RUN uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt && \
    uv pip install --target /install --requirement /tmp/requirements.txt

# Deno runtime required by Prefab's sandbox when NEXTDNS_GENERATIVE_UI=on.
# Installed in the builder stage only, so curl/unzip never reach the runtime image.
# The versioned release archive is downloaded and verified against a pinned SHA256
# for the build architecture before extraction; the resulting binary is copied
# out via COPY --from=builder below.
ARG DENO_VERSION=2.3.7
ARG DENO_X86_64_SHA256=63f55a621441fbbc8830324baf3d9c7a94928c7fb6b8ad54265dc9442dcd1dd2
ARG DENO_AARCH64_SHA256=fbf420aee4469094bd91e595dd78814c94737dfb1d26e640b13aa938a9663cf8
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && case "$(uname -m)" in \
         x86_64) DENO_ARCH=x86_64; DENO_SHA256="${DENO_X86_64_SHA256}" ;; \
         aarch64) DENO_ARCH=aarch64; DENO_SHA256="${DENO_AARCH64_SHA256}" ;; \
         *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_ARCH}-unknown-linux-gnu.zip" -o /tmp/deno.zip \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && mkdir /deno && unzip -q /tmp/deno.zip -d /deno \
    && chmod +x /deno/deno \
    && rm -f /tmp/deno.zip && rm -rf /var/lib/apt/lists/*

# 2. Final stage: Create the runtime image
FROM python:3.15-slim

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# OCI labels for container metadata
LABEL org.opencontainers.image.title="NextDNS MCP Server"
LABEL org.opencontainers.image.description="Model Context Protocol server for NextDNS API"
LABEL org.opencontainers.image.authors="NextDNS MCP Contributors"
LABEL org.opencontainers.image.source="https://github.com/dmeiser/nextdns-mcp"
LABEL org.opencontainers.image.documentation="https://github.com/dmeiser/nextdns-mcp/blob/main/README.md"
LABEL org.opencontainers.image.version="4.2.0"
LABEL org.opencontainers.image.licenses="MIT"

# Transport modes:
# - stdio (default): For Claude Desktop, CLI tools
# - http: For network services and web-based clients. The HTTP endpoint has NO
#   built-in authentication. It binds to 127.0.0.1 (loopback-only) by default.
#   To reach it across the network you must explicitly set MCP_HOST to a
#   non-loopback address AND put an authenticating reverse proxy in front of it.
#   Set MCP_TRANSPORT=http, optionally MCP_HOST and MCP_PORT (see README).

# Expose port for HTTP transport mode (optional)
EXPOSE 8000

# Set working directory
WORKDIR /app

# Copy installed packages from the builder stage
COPY --from=builder /install /install

# Copy application code
COPY src/ /app/src/

# Deno runtime required by Prefab's sandbox when NEXTDNS_GENERATIVE_UI=on.
# The binary was downloaded and SHA256-verified in the builder stage, so this
# stage only receives the verified executable and carries no curl or unzip.
COPY --from=builder /deno/deno /usr/local/bin/deno

# Create and switch to a non-root user for security
RUN useradd --create-home appuser
USER appuser

# Set PYTHONPATH to include the dependency install dir and /app/src so Python can
# find the installed packages and the nextdns_mcp module
ENV PYTHONPATH=/install:/app/src \
    PATH=/usr/local/bin:$PATH \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=auto

# Command to run the application
CMD ["python", "-m", "nextdns_mcp.server"]
