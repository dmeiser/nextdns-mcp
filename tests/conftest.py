"""Pytest configuration and fixtures for NextDNS MCP Server tests."""

import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest
import yaml


@pytest.fixture
def mock_api_key() -> str:
    """Provide a mock API key for testing."""
    return "test_api_key_12345"


@pytest.fixture
def mock_profile_id() -> str:
    """Provide a mock profile ID for testing."""
    return "abc123"


@pytest.fixture
def clean_env(monkeypatch) -> None:
    """Clean environment variables before tests."""
    # Remove NextDNS-related env vars
    for key in [
        "NEXTDNS_API_KEY",
        "NEXTDNS_API_KEY_FILE",
        "NEXTDNS_DEFAULT_PROFILE",
        "NEXTDNS_HTTP_TIMEOUT",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def set_env_api_key(monkeypatch, mock_api_key: str) -> str:
    """Set NEXTDNS_API_KEY environment variable."""
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    return mock_api_key


@pytest.fixture
def temp_api_key_file(mock_api_key: str) -> Generator[Path, None, None]:
    """Create a temporary file with an API key."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write(mock_api_key)
        temp_path = Path(f.name)

    yield temp_path

    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def mock_openapi_spec() -> dict:
    """Provide a minimal valid OpenAPI spec for testing."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Test NextDNS API", "version": "1.0.0"},
        "servers": [{"url": "https://api.nextdns.io"}],
        "paths": {
            "/profiles": {
                "get": {
                    "operationId": "listProfiles",
                    "responses": {"200": {"description": "Success"}},
                }
            }
        },
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
            }
        },
    }


@pytest.fixture
def temp_openapi_file(mock_openapi_spec: dict) -> Generator[Path, None, None]:
    """Create a temporary OpenAPI spec file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(mock_openapi_spec, f)
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
