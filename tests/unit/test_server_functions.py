"""Tests for server.py helper functions and tools."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from nextdns_mcp.server import (
    _build_doh_metadata,
    _bulk_update_helper,
    _dohLookup_impl,
    _get_target_profile,
    _validate_record_type,
    create_access_denied_response,
    create_nextdns_client,
    load_openapi_spec,
    mcp,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


class TestLoadOpenAPISpec:
    """Tests for load_openapi_spec function."""

    def test_loads_valid_spec(self):
        """Test loading valid OpenAPI spec."""
        spec = load_openapi_spec()

        assert isinstance(spec, dict)
        assert "openapi" in spec
        assert "info" in spec
        assert "paths" in spec
        assert spec["info"]["title"] == "NextDNS API"

    def test_spec_has_paths(self):
        """Test that spec has expected paths."""
        spec = load_openapi_spec()

        # Check for some expected paths
        assert "/profiles" in spec["paths"]
        assert "/profiles/{profile_id}" in spec["paths"]


class TestCreateNextDNSClient:
    """Tests for create_nextdns_client function."""

    def test_creates_client_with_api_key(self, clean_env):
        """Test creating client with API key."""
        clean_env("NEXTDNS_API_KEY", "test-key")
        clean_env("NEXTDNS_HTTP_TIMEOUT", "30")

        client = create_nextdns_client()

        assert isinstance(client, httpx.AsyncClient)
        assert client.headers["X-Api-Key"] == "test-key"
        assert client.base_url == "https://api.nextdns.io"

    def test_client_has_correct_headers(self, clean_env):
        """Test client has all required headers."""
        clean_env("NEXTDNS_API_KEY", "test-key")

        client = create_nextdns_client()

        assert client.headers["Accept"] == "application/json"
        assert client.headers["Content-Type"] == "application/json"


class TestCreateAccessDeniedResponse:
    """Tests for create_access_denied_response function."""

    def test_creates_403_response(self):
        """Test creates response with 403 status."""
        response = create_access_denied_response(
            "GET", "/profiles/abc123", "Access denied", "abc123"
        )

        assert response.status_code == 403
        assert response.headers["content-type"] == "application/json"

    def test_response_contains_error_details(self):
        """Test response contains error information."""
        response = create_access_denied_response(
            "POST", "/profiles/abc123/denylist", "Write denied", "abc123"
        )

        data = response.json()
        assert "error" in data
        assert data["error"] == "Write denied"
        assert data["profile_id"] == "abc123"
        # Method and URL are in the request, not the response body
        assert response.request.method == "POST"
        assert "/profiles/abc123/denylist" in str(response.request.url)


class TestGetTargetProfile:
    """Tests for _get_target_profile function."""

    def test_returns_provided_profile(self, clean_env):
        """Test returns profile_id when provided."""
        result = _get_target_profile("abc123")
        assert result == "abc123"

    def test_returns_default_when_not_provided(self, clean_env):
        """Test returns default profile when profile_id is None."""
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        result = _get_target_profile(None)
        assert result == "def456"

    def test_returns_none_when_no_default(self, clean_env):
        """Test returns None when no profile_id and no default."""
        result = _get_target_profile(None)
        assert result is None


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
    async def test_successful_lookup(self, clean_env):
        """Test successful DNS lookup."""
        clean_env("NEXTDNS_HTTP_TIMEOUT", "30")

        # Mock the httpx response
        mock_response = MagicMock()
        mock_response.json.return_value = {"Status": 0, "Answer": [{"data": "1.2.3.4"}]}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = await _dohLookup_impl("example.com", "abc123", "A")

        assert "Status" in result
        assert "_metadata" in result

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self, clean_env):
        """Test HTTP error returns error dict."""
        clean_env("NEXTDNS_HTTP_TIMEOUT", "30")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.side_effect = httpx.HTTPError("Connection failed")
            mock_client_class.return_value = mock_client

            result = await _dohLookup_impl("example.com", "abc123", "A")

        assert "error" in result
        assert "HTTP error" in result["error"]


class TestBulkUpdateHelper:
    """Tests for _bulk_update_helper function."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self):
        """Test invalid JSON returns error."""
        result = await _bulk_update_helper("abc123", "invalid json", "/endpoint", "entries")

        assert "error" in result
        assert "Invalid JSON" in result["error"]

    @pytest.mark.asyncio
    async def test_non_array_returns_error(self):
        """Test non-array JSON returns error."""
        result = await _bulk_update_helper("abc123", '{"key": "value"}', "/endpoint", "entries")

        assert "error" in result
        assert "must be a JSON array" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_update(self):
        """Test successful bulk update."""
        # Mock the response
        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        mock_response.raise_for_status = MagicMock()  # No exception

        # Patch the actual client instance that mcp uses
        with patch.object(mcp._client, "put", new_callable=AsyncMock) as mock_put:
            mock_put.return_value = mock_response

            result = await _bulk_update_helper(
                "abc123",
                '["example.com", "test.com"]',
                "/profiles/{profile_id}/denylist",
                "entries",
            )

        assert "success" in result
        assert result["success"] is True
        mock_put.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self):
        """Test HTTP error returns error dict."""
        # Patch the actual client instance to raise an exception
        with patch.object(mcp._client, "put", new_callable=AsyncMock) as mock_put:
            mock_put.side_effect = httpx.HTTPError("Connection failed")

            result = await _bulk_update_helper(
                "abc123", '["example.com"]', "/profiles/{profile_id}/denylist", "entries"
            )

        assert "error" in result
        assert "HTTP error" in result["error"]
