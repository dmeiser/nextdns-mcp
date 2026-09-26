# Multi-stage build for a lean final image

# 1. Builder stage: Install dependencies
FROM python:3.14-slim AS builder

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files and source code for build
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install dependencies into a version-independent directory
# --frozen: use exact versions from uv.lock without updating
# --no-dev: exclude development dependencies
# --no-emit-project: skip installing nextdns-mcp itself; the runtime stage copies
#   src/ onto PYTHONPATH directly
# Installing into /install keeps the layout free of a python3.x path component, so
# the runtime stage never has to name the builder's Python minor version.
RUN uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt && \
    uv pip install --target /install --requirement /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt

# 2. Final stage: Create the runtime image
FROM python:3.14-slim

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# OCI labels for container metadata
LABEL org.opencontainers.image.title="NextDNS MCP Server"
LABEL org.opencontainers.image.description="Model Context Protocol server for NextDNS API"
LABEL org.opencontainers.image.authors="NextDNS MCP Contributors"
LABEL org.opencontainers.image.source="https://github.com/dmeiser/nextdns-mcp"
LABEL org.opencontainers.image.documentation="https://github.com/dmeiser/nextdns-mcp/blob/main/README.md"
LABEL org.opencontainers.image.version="3.1.18"
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

# Create and switch to a non-root user for security
RUN useradd --create-home appuser
USER appuser

# Set PYTHONPATH to include the dependency install dir and /app/src so Python can
# find the installed packages and the nextdns_mcp module
ENV PYTHONPATH=/install:/app/src \
    FASTMCP_CHECK_FOR_UPDATES=off

# Command to run the application
CMD ["python", "-m", "nextdns_mcp.server"]
