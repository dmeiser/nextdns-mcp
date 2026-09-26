"""Tests for server.py helper functions and tools."""

import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextdns_mcp.client import AccessDeniedError, create_nextdns_client
from nextdns_mcp.tools import doh as doh_module
from nextdns_mcp.tools.doh import (
    _build_doh_metadata,
    _dohLookup_impl,
    _validate_record_type,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


@pytest.fixture(autouse=True)
def allow_doh_read_access(monkeypatch):
    """Allow all DoH lookups by bypassing the can_read_profile gate.

    Patches the function's global namespace directly so the bypass survives
    module reloads performed by other tests.
    """
    monkeypatch.setitem(_dohLookup_impl.__globals__, "can_read_profile", lambda _profile_id: True)


@pytest.fixture(autouse=True)
async def mock_doh_client(monkeypatch):
    """Install a mock persistent DoH client so no test hits the network."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.json.return_value = {"Status": 0, "Answer": [{"data": "1.2.3.4"}]}
    mock_client.get.return_value = mock_response
    monkeypatch.setattr(doh_module, "_doh_client", mock_client)
    return mock_client


class TestCreateNextDNSClient:
    """Tests for create_nextdns_client function."""

    def test_creates_client_with_api_key(self, clean_env):
        """Test creating client with API key set in static headers at initialization."""
        clean_env("NEXTDNS_API_KEY", "test-key")
        clean_env("NEXTDNS_HTTP_TIMEOUT", "30")

        client = create_nextdns_client()

        assert isinstance(client, httpx.AsyncClient)
        # API key is set at initialization in static headers
        assert "X-Api-Key" in client.headers
        assert client.base_url == "https://api.nextdns.io"

    def test_client_has_correct_headers(self, clean_env):
        """Test client has all required headers."""
        clean_env("NEXTDNS_API_KEY", "test-key")

        client = create_nextdns_client()

        assert client.headers["Accept"] == "application/json"
        assert client.headers["Content-Type"] == "application/json"


class TestAccessDeniedError:
    """Tests for the typed AccessDeniedError raised by the ACL layer (issue #178)."""

    def test_carries_reason_code_and_profile(self):
        """The exception carries the denial reason, typed code, and profile id."""
        error = AccessDeniedError("Write denied", code="write_access_denied", profile_id="abc123")

        assert str(error) == "Write denied"
        assert error.code == "write_access_denied"
        assert error.profile_id == "abc123"

    def test_default_code_and_profile(self):
        """The typed code defaults to access_denied and profile_id to empty."""
        error = AccessDeniedError("denied")

        assert error.code == "access_denied"
        assert error.profile_id == ""

    def test_is_plain_exception_not_httpx(self):
        """ACL denials are typed exceptions, not fake httpx responses."""
        import httpx

        assert not isinstance(AccessDeniedError("denied"), httpx.HTTPError)


class TestValidateRecordType:
    """Tests for _validate_record_type function."""

    def test_validates_valid_types(self):
        """Test validates valid DNS record types."""
        valid_types = ["A", "AAAA", "CNAME", "MX", "TXT"]

        for record_type in valid_types:
            is_valid, normalized = _validate_record_type(record_type)
            assert is_valid is True
            assert normalized == record_type.upper()

    def test_validates_lowercase_types(self):
        """Test validates lowercase record types."""
        is_valid, normalized = _validate_record_type("a")
        assert is_valid is True
        assert normalized == "A"

    def test_rejects_invalid_types(self):
        """Test rejects invalid DNS record types."""
        is_valid, normalized = _validate_record_type("INVALID")
        assert is_valid is False
        assert normalized == "INVALID"


class TestBuildDohMetadata:
    """Tests for _build_doh_metadata function."""

    def test_builds_metadata_dict(self):
        """Test builds metadata dictionary."""
        doh_url = "https://dns.nextdns.io/abc123"
        metadata = _build_doh_metadata("abc123", "example.com", "A", doh_url, 0)

        assert isinstance(metadata, dict)
        assert metadata["profile_id"] == "abc123"
        assert metadata["query_domain"] == "example.com"
        assert metadata["query_type"] == "A"
        assert metadata["doh_endpoint"] == f"{doh_url}?name=example.com&type=A"
        assert metadata["status_description"] == "NOERROR - Success"

    def test_includes_doh_url(self):
        """Test includes DoH URL in metadata."""
        doh_url = "https://dns.nextdns.io/abc123"
        metadata = _build_doh_metadata("abc123", "example.com", "A", doh_url, None)

        assert "doh_endpoint" in metadata
        assert doh_url in metadata["doh_endpoint"]
        # When status is None, no status_description should be added
        assert "status_description" not in metadata


class TestDohLookupImpl:
    """Tests for _dohLookup_impl function."""

    @pytest.mark.asyncio
    async def test_no_profile_returns_error(self, clean_env):
        """Test returns error when no profile_id and no default."""
        result = await _dohLookup_impl("example.com", None, "A")

        assert "error" in result
        assert "No profile_id provided" in result["error"]
        assert "hint" in result

    @pytest.mark.asyncio
    async def test_invalid_record_type_returns_error(self, clean_env):
        """Test returns error for invalid record type."""
        clean_env("NEXTDNS_DEFAULT_PROFILE", "abc123")

        result = await _dohLookup_impl("example.com", "abc123", "INVALID")

        assert "error" in result
        assert "Invalid record type: INVALID" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_lookup(self, clean_env, mock_doh_client):
        """Test successful DNS lookup."""
        clean_env("NEXTDNS_HTTP_TIMEOUT", "30")

        result = await _dohLookup_impl("example.com", "abc123", "A")

        assert "Status" in result["data"]
        assert "_metadata" in result

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self, clean_env, mock_doh_client):
        """Test HTTP error returns error dict."""
        clean_env("NEXTDNS_HTTP_TIMEOUT", "30")

        mock_doh_client.get.side_effect = httpx.HTTPError("Connection failed")

        result = await _dohLookup_impl("example.com", "abc123", "A")

        assert "error" in result
        assert "HTTP error" in result["error"]
