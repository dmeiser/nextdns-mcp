# Multi-stage build for a lean final image

# 1. Builder stage: Install dependencies
FROM python:3.13-slim AS builder

# Update system packages for security
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install poetry
RUN pip install --no-cache-dir poetry

# Configure poetry to create the virtualenv in the project directory,
# so we know where to copy from.
RUN poetry config virtualenvs.in-project true

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Install dependencies into the project's .venv
RUN poetry install --without dev --no-interaction --no-ansi --no-root

# 2. Final stage: Create the runtime image
FROM python:3.13-slim

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

# Set working directory
WORKDIR /app

# Copy installed packages from the builder stage's virtual environment
# This path is predictable because of `poetry config virtualenvs.in-project true`
COPY --from=builder /app/.venv/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# Copy application code
COPY src/ /app/src/

# Create and switch to a non-root user for security
RUN useradd --create-home appuser
USER appuser

# Set PYTHONPATH to include /app/src so Python can find the nextdns_mcp module
ENV PYTHONPATH=/app/src

# Command to run the application
CMD ["python", "-m", "nextdns_mcp.server"]
