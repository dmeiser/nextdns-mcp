"""Integration tests for AccessControlledClient HTTP interception."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nextdns_mcp.server import AccessControlledClient


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


@pytest.fixture
def mock_super_request():
    """Mock the parent AsyncClient.request method."""
    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock:
        # Create a successful mock response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": "success"}
        mock.return_value = mock_response
        yield mock


class TestAccessControlledClientReadAccess:
    """Test read access control in AccessControlledClient."""

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_read_profile", return_value=True)
    async def test_allows_read_when_permitted(self, mock_can_read, mock_super_request):
        """Test that read requests are allowed when profile is readable."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/abc123/settings")

        # Should call the parent request method
        mock_super_request.assert_called_once()
        mock_can_read.assert_called_once_with("abc123")
        assert response.status_code == 200

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_read_profile", return_value=False)
    async def test_denies_read_when_not_permitted(self, mock_can_read, mock_super_request):
        """Test that read requests are denied when profile is not readable."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/abc123/settings")

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        mock_can_read.assert_called_once_with("abc123")
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_allows_list_profiles_without_check(self, mock_super_request):
        """Test that /profiles without ID is allowed (listProfiles)."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles")

        # Should call the parent request method without access checks
        mock_super_request.assert_called_once()
        assert response.status_code == 200


class TestAccessControlledClientWriteAccess:
    """Test write access control in AccessControlledClient."""

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_write_profile", return_value=True)
    async def test_allows_write_when_permitted(self, mock_can_write, mock_super_request, clean_env):
        """Test that write requests are allowed when profile is writable."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request(
                "PATCH", "/profiles/abc123/settings", json={"name": "Test"}
            )

        # Should call the parent request method
        mock_super_request.assert_called_once()
        mock_can_write.assert_called_once_with("abc123")
        assert response.status_code == 200

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_write_profile", return_value=False)
    async def test_denies_write_when_not_permitted(self, mock_can_write, mock_super_request, clean_env):
        """Test that write requests are denied when profile is not writable."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request(
                "POST", "/profiles/abc123/denylist", json={"id": "example.com"}
            )

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        mock_can_write.assert_called_once_with("abc123")
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_write_profile", return_value=False)
    @patch("nextdns_mcp.server.is_read_only", return_value=True)
    async def test_denies_all_writes_in_read_only_mode(self, mock_is_readonly, mock_can_write, mock_super_request, clean_env):
        """Test that all write requests are denied in read-only mode."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("DELETE", "/profiles/abc123")

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        mock_can_write.assert_called_once_with("abc123")
        assert response.status_code == 403
        assert "read-only mode" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_allows_create_profile_without_check(self, mock_super_request, clean_env):
        """Test that POST /profiles (createProfile) requires access check."""
        # Creating a profile doesn't have a profile_id in the URL yet
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("POST", "/profiles", json={"name": "New Profile"})

        # Should call the parent request since URL doesn't contain profile_id
        mock_super_request.assert_called_once()
        assert response.status_code == 200


class TestAccessControlledClientMethods:
    """Test different HTTP methods."""

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_write_profile", return_value=True)
    async def test_put_is_write_operation(self, mock_can_write, mock_super_request, clean_env):
        """Test that PUT is treated as a write operation."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("PUT", "/profiles/abc123/denylist", json=[])

        mock_can_write.assert_called_once_with("abc123")
        assert response.status_code == 200

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_write_profile", return_value=True)
    async def test_patch_is_write_operation(self, mock_can_write, mock_super_request, clean_env):
        """Test that PATCH is treated as a write operation."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request(
                "PATCH", "/profiles/abc123/settings", json={"name": "Test"}
            )

        mock_can_write.assert_called_once_with("abc123")
        assert response.status_code == 200

    @pytest.mark.asyncio
    @patch("nextdns_mcp.server.can_write_profile", return_value=True)
    async def test_delete_is_write_operation(self, mock_can_write, mock_super_request, clean_env):
        """Test that DELETE is treated as a write operation."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("DELETE", "/profiles/abc123")

        mock_can_write.assert_called_once_with("abc123")
        assert response.status_code == 200
