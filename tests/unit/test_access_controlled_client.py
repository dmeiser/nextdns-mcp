"""Integration tests for AccessControlledClient HTTP interception."""

import os
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nextdns_mcp.server import AccessControlledClient


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], None]:
    """Clean environment for each test - runs before all tests."""
    # Clear all environment variables to ensure clean state
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    # Set minimal required env for the module to load
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-key-12345")
    return monkeypatch.setenv


@pytest.fixture
def mock_super_request() -> Any:
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
    async def test_allows_read_when_permitted(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that read requests are allowed when profile is readable."""
        # Set up environment to restrict access
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/abc123/settings")

        # Should call the parent request method
        mock_super_request.assert_called_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_denies_read_when_not_permitted(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that read requests are denied when profile is not readable."""
        # Set up environment to restrict access to a different profile
        clean_env("NEXTDNS_READABLE_PROFILES", "xyz999")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/abc123/settings")

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_allows_list_profiles_without_check(self, mock_super_request: Any) -> None:
        """Test that /profiles without ID is allowed (listProfiles)."""
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles")

        # Should call the parent request method without access checks
        mock_super_request.assert_called_once()
        assert response.status_code == 200


class TestAccessControlledClientWriteAccess:
    """Test write access control in AccessControlledClient."""

    @pytest.mark.asyncio
    async def test_allows_write_when_permitted(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that write requests are allowed when profile is writable."""
        # Set up environment to allow writes
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request(
                "PATCH", "/profiles/abc123/settings", json={"name": "Test"}
            )

        # Should call the parent request method
        mock_super_request.assert_called_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_denies_write_when_not_permitted(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that write requests are denied when profile is not writable."""
        # Set up environment to restrict writes to a different profile
        clean_env("NEXTDNS_WRITABLE_PROFILES", "xyz999")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request(
                "POST", "/profiles/abc123/denylist", json={"id": "example.com"}
            )

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_denies_all_writes_in_read_only_mode(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that all write requests are denied in read-only mode."""
        # Set up environment for read-only mode (even if profile is writable)
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")
        clean_env("NEXTDNS_READ_ONLY", "true")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("DELETE", "/profiles/abc123")

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "read-only mode" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_allows_create_profile_without_check(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
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
    async def test_put_is_write_operation(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that PUT is treated as a write operation."""
        # Set up environment to allow writes
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("PUT", "/profiles/abc123/denylist", json=[])

        # Should call the parent request method
        mock_super_request.assert_called_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_is_write_operation(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that PATCH is treated as a write operation."""
        # Set up environment to allow writes
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request(
                "PATCH", "/profiles/abc123/settings", json={"name": "Test"}
            )

        # Should call the parent request method
        mock_super_request.assert_called_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_is_write_operation(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that DELETE is treated as a write operation."""
        # Set up environment to allow writes
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("DELETE", "/profiles/abc123")

        # Should call the parent request method
        mock_super_request.assert_called_once()
        assert response.status_code == 200
