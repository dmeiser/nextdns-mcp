# Multi-stage build for NextDNS MCP Server
FROM python:3.13-slim AS builder

# OCI labels for Docker MCP Gateway compatibility
LABEL org.opencontainers.image.title="NextDNS MCP Server"
LABEL org.opencontainers.image.description="Model Context Protocol server for NextDNS API - manage DNS profiles, analytics, security, and privacy settings"
LABEL org.opencontainers.image.authors="NextDNS MCP Contributors"
LABEL org.opencontainers.image.source="https://github.com/yourusername/nextdns-mcp"
LABEL org.opencontainers.image.documentation="https://github.com/yourusername/nextdns-mcp/blob/main/README.md"
LABEL org.opencontainers.image.version="2.0.0"
LABEL org.opencontainers.image.licenses="MIT"

# MCP-specific labels
LABEL com.docker.mcp.server.type="stdio"
LABEL com.docker.mcp.server.protocol="mcp"
LABEL com.docker.mcp.server.category="dns,api,networking"

# Install poetry
RUN pip install --no-cache-dir poetry==1.7.1

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Configure poetry to not create virtual environment (we're in a container)
RUN poetry config virtualenvs.create false

# Install dependencies
RUN poetry install --no-dev --no-interaction --no-ansi

# Final stage
FROM python:3.13-slim

# Copy OCI labels to final image
LABEL org.opencontainers.image.title="NextDNS MCP Server"
LABEL org.opencontainers.image.description="Model Context Protocol server for NextDNS API - manage DNS profiles, analytics, security, and privacy settings"
LABEL org.opencontainers.image.authors="NextDNS MCP Contributors"
LABEL org.opencontainers.image.source="https://github.com/yourusername/nextdns-mcp"
LABEL org.opencontainers.image.documentation="https://github.com/yourusername/nextdns-mcp/blob/main/README.md"
LABEL org.opencontainers.image.version="2.0.0"
LABEL org.opencontainers.image.licenses="MIT"

# MCP-specific labels
LABEL com.docker.mcp.server.type="stdio"
LABEL com.docker.mcp.server.protocol="mcp"
LABEL com.docker.mcp.server.category="dns,api,networking"

# Set working directory
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code and OpenAPI spec
COPY src/ /app/src/

# Create non-root user for security
RUN useradd -m -u 1000 mcpuser && \
    chown -R mcpuser:mcpuser /app

# Switch to non-root user
USER mcpuser

# Set Python path
ENV PYTHONPATH=/app/src

# MCP servers use stdio transport, no port needed

# Run the MCP server
CMD ["python", "-m", "nextdns_mcp.server"]
