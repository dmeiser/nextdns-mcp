"""Pytest configuration and fixtures for NextDNS MCP Server tests."""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def mock_api_key() -> str:
    """Provide a mock API key for testing."""
    return "test_api_key_12345"


@pytest.fixture
def mock_profile_id() -> str:
    """Provide a mock profile ID for testing."""
    return "abc123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch) -> None:
    """Remove every NextDNS/MCP/FastMCP env var so tests do not inherit ambient config.

    Autouse, and prefix-based, so the access-control variables
    (NEXTDNS_READ_ONLY, NEXTDNS_READABLE_PROFILES, NEXTDNS_WRITABLE_PROFILES)
    cannot leak in from the developer's shell and flip a test's code path, and
    so a key added to config.py later is covered without editing this file.
    """
    for key in list(os.environ):
        if key.startswith(("NEXTDNS_", "MCP_", "FASTMCP_")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def set_env_api_key(monkeypatch, mock_api_key: str) -> str:
    """Set NEXTDNS_API_KEY environment variable."""
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    return mock_api_key


@pytest.fixture
def temp_api_key_file(mock_api_key: str) -> Iterator[Path]:
    """Create a temporary file with an API key."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write(mock_api_key)
        temp_path = Path(f.name)

    yield temp_path

    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def mock_nextdns_base_url() -> str:
    """Provide the NextDNS base URL."""
    return "https://api.nextdns.io"


@pytest.fixture
def mock_doh_response() -> dict:
    """Provide a mock DoH response."""
    return {
        "Status": 0,
        "Question": [{"name": "google.com.", "type": 1}],
        "Answer": [{"name": "google.com.", "type": 1, "TTL": 300, "data": "142.250.190.46"}],
    }


@pytest.fixture
def mock_profiles_response() -> dict:
    """Provide a mock profiles list response."""
    return {
        "data": [
            {"id": "abc123", "name": "Home Network", "fingerprint": "fp:abc"},
            {"id": "def456", "name": "Mobile", "fingerprint": "fp:def"},
        ]
    }
