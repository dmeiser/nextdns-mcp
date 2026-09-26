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
    # Clear the profile cache to prevent test pollution
    import nextdns_mcp.config

    nextdns_mcp.config._readable_profiles_cache = None
    nextdns_mcp.config._writable_profiles_cache = None
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
            response = await client.request("PATCH", "/profiles/abc123/settings", json={"name": "Test"})

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
            response = await client.request("POST", "/profiles/abc123/denylist", json={"id": "example.com"})

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


class TestAccessControlledClientFailsClosed:
    """Test that the client fails closed (403) on URLs that bypass the profile ACL.

    Regression tests for issue #131: traversal payloads and absolute URLs used to
    skip the access check entirely because extract_profile_id_from_url returned
    None and None was treated as 'no profile, no check'.
    """

    @pytest.mark.asyncio
    async def test_denies_path_traversal_bypassing_acl(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that traversal payloads are denied even when they point at a denied profile."""
        # Only the 'allowed123' profile is readable; the traversal target is denied.
        clean_env("NEXTDNS_READABLE_PROFILES", "allowed123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/allowed123/../../profiles/denied456/settings")

        # The request must NOT reach the transport; it must be denied with 403.
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_denies_absolute_url_with_profile_path(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that absolute URLs carrying a profile path are denied."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "https://evil.example/profiles/abc123/settings")

        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_denies_unclassifiable_profiles_path(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that /profiles paths without a safe profile id are denied."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/abc.def/settings")

        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_denies_relative_profile_path_when_not_readable(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that a relative profile path without a leading slash is denied when unreadable."""
        clean_env("NEXTDNS_READABLE_PROFILES", "allowed123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "profiles/denied456/settings")

        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_allows_relative_profile_path_when_readable(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that a relative profile path without a leading slash passes when readable."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "profiles/abc123/settings")

        mock_super_request.assert_called_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_denies_traversal_even_when_all_profiles_readable(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that traversal payloads are denied even when every profile is readable."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/allowed123/../../profiles/denied456/settings")

        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()


class TestAccessControlledClientMethods:
    """Test different HTTP methods."""

    @pytest.mark.asyncio
    async def test_put_is_write_operation(self, mock_super_request: Any, clean_env: Callable[[str, str], None]) -> None:
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
            response = await client.request("PATCH", "/profiles/abc123/settings", json={"name": "Test"})

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


class TestAccessControlledClientBodyPassthrough:
    """Regression tests for issue #145.

    The client must NOT blindly coerce string values in JSON request bodies:
    a profile name like ``"12345"`` or a password like ``"0012"`` would be
    corrupted into numbers, and ``"true"`` into a boolean, causing upstream
    400s. Schema-aware coercion already happens in
    StripExtraFieldsMiddleware, so bodies pass through unchanged.
    """

    @pytest.mark.asyncio
    async def test_numeric_string_body_values_unchanged(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Numeric-looking strings (names, passwords) must not become numbers."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            await client.request("PATCH", "/profiles/abc123", json={"name": "12345"})
            await client.request("POST", "/profiles/abc123/denylist", json={"password": "0012"})

        assert mock_super_request.call_args_list[0].kwargs["json"] == {"name": "12345"}
        assert isinstance(mock_super_request.call_args_list[0].kwargs["json"]["name"], str)
        assert mock_super_request.call_args_list[1].kwargs["json"] == {"password": "0012"}
        assert isinstance(mock_super_request.call_args_list[1].kwargs["json"]["password"], str)

    @pytest.mark.asyncio
    async def test_boolean_string_body_values_unchanged(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """A string like ``"true"`` in a body must not become a boolean."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            await client.request("PATCH", "/profiles/abc123/settings", json={"web3": "true"})

        body = mock_super_request.call_args.kwargs["json"]
        assert body == {"web3": "true"}
        assert isinstance(body["web3"], str)

    @pytest.mark.asyncio
    async def test_native_typed_body_values_unchanged(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Properly typed values must pass through as-is."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            await client.request("PUT", "/profiles/abc123/denylist", json=[{"id": "example.com", "blocked": True}])

        body = mock_super_request.call_args.kwargs["json"]
        assert body == [{"id": "example.com", "blocked": True}]
        assert isinstance(body[0]["blocked"], bool)
