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

# Install dependencies into the project's .venv
# --frozen: use exact versions from uv.lock without updating
# --no-dev: exclude development dependencies
RUN uv sync --frozen --no-dev

# 2. Final stage: Create the runtime image
FROM python:3.14-slim

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# OCI labels for Docker MCP Gateway compatibility
LABEL org.opencontainers.image.title="NextDNS MCP Server"
LABEL org.opencontainers.image.description="Model Context Protocol server for NextDNS API"
LABEL org.opencontainers.image.authors="NextDNS MCP Contributors"
LABEL org.opencontainers.image.source="https://github.com/dmeiser/nextdns-mcp"
LABEL org.opencontainers.image.documentation="https://github.com/dmeiser/nextdns-mcp/blob/main/README.md"
LABEL org.opencontainers.image.version="2.0.0"
LABEL org.opencontainers.image.licenses="MIT"

# MCP-specific labels
LABEL com.docker.mcp.server.type="stdio"
LABEL com.docker.mcp.server.protocol="mcp"
LABEL com.docker.mcp.server.category="dns,api,networking"

# Transport modes:
# - stdio (default): For Claude Desktop, MCP Gateway, CLI tools
# - http: For network services, web-based clients, production deployments
#   Set MCP_TRANSPORT=http, optionally MCP_HOST and MCP_PORT

# Expose port for HTTP transport mode (optional)
EXPOSE 8000

# Set working directory
WORKDIR /app

# Copy installed packages from the builder stage's virtual environment
COPY --from=builder /app/.venv/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages

# Copy application code
COPY src/ /app/src/

# Create and switch to a non-root user for security
RUN useradd --create-home appuser
USER appuser

# Set PYTHONPATH to include /app/src so Python can find the nextdns_mcp module
ENV PYTHONPATH=/app/src

# Command to run the application
CMD ["python", "-m", "nextdns_mcp.server"]
