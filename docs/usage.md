# Usage

How to run the NextDNS MCP Server locally, in Docker, and with Docker MCP Gateway. Includes examples for invoking tools.

## Running Locally (Development)

Prerequisites:
- Python 3.13+
- Poetry

Steps:

```bash
poetry install
poetry run python -m nextdns_mcp.server
```

Environment variables can be set via a `.env` file (see Configuration) or your shell.

## Running with Docker

Build the image:

```bash
docker build -t nextdns-mcp:latest .
```

Run with environment variables:

```bash
docker run -i --rm \
  -e NEXTDNS_API_KEY=your_api_key_here \
  -e NEXTDNS_DEFAULT_PROFILE=your_profile_id \
  nextdns-mcp:latest
```

Run with a `.env` file (development):

```bash
docker run -i --rm --env-file .env nextdns-mcp:latest
```

Run with Docker secret file (recommended):

```bash
echo "your_api_key_here" > /tmp/api_key.txt
chmod 600 /tmp/api_key.txt
docker run -i --rm \
  -v /tmp/api_key.txt:/run/secrets/nextdns_api_key:ro \
  -e NEXTDNS_API_KEY_FILE=/run/secrets/nextdns_api_key \
  nextdns-mcp:latest
```

Note: MCP servers communicate via stdio; no network ports are exposed.

## Using Docker MCP Gateway (Recommended)

1) Build and tag the image:

```bash
docker build -t nextdns-mcp:latest .
```

2) Import the catalog to register the server:

```bash
docker mcp catalog import ./catalog.yaml
```

3) Enable the server:

```bash
docker mcp server enable nextdns
```

During enablement, set the required secret `nextdns.api_key`.

4) Set or update the secret later (optional):

```bash
echo "your_api_key_here" | docker mcp secret set nextdns.api_key
docker mcp secret ls
```

## Invoking Tools

Example: DNS-over-HTTPS lookup through a profile

```bash
docker mcp tools call nextdns dohLookup \
  --domain "adwords.google.com" \
  --profile_id "abc123" \
  --record_type "A"
```

Other examples:

```bash
# IPv6 lookup
docker mcp tools call nextdns dohLookup --domain "google.com" --record_type "AAAA"

# MX records
docker mcp tools call nextdns dohLookup --domain "gmail.com" --record_type "MX"
```

## Access Control Examples

Read-only mode (no writes allowed):

```env
NEXTDNS_READ_ONLY=true
```

Restrict reading to specific profiles:

```env
NEXTDNS_READABLE_PROFILES=home123,work456
```

Allow writing to a dedicated test profile only:

```env
NEXTDNS_WRITABLE_PROFILES=test123
```
