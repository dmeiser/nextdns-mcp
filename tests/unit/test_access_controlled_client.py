"""Integration tests for AccessControlledClient HTTP interception."""

import logging
import os
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nextdns_mcp.client import AccessControlledClient


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
    async def test_uppercase_config_allows_lowercase_query(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Regression test for #168: uppercase NEXTDNS_READABLE_PROFILES allows lowercase query."""
        clean_env("NEXTDNS_READABLE_PROFILES", "2F4A9B")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles/2f4a9b/settings")

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
        assert response.json()["code"] == "read_access_denied"
        assert "Read access denied" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_allows_list_profiles_without_check(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that /profiles without ID is allowed when reads are permitted (listProfiles)."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles")

        # Should call the parent request method without access checks
        mock_super_request.assert_called_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_denies_list_profiles_when_no_readable_profiles(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Regression test: GET /profiles with readable set unset (deny-all reads) must be denied."""
        # Mirrors the per-tool list check in tools/profiles.py: readable set is None only
        # when both readable and writable are unset (writable implies readable).
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("GET", "/profiles")

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "no profiles are readable" in response.json()["error"]
        assert response.json()["code"] == "read_access_denied"


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
        assert response.json()["code"] == "write_access_denied"

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
        assert response.json()["code"] == "write_access_denied"

    @pytest.mark.asyncio
    async def test_denies_create_profile_when_no_writable_profiles(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that POST /profiles is denied when no profiles are writable (issue #132)."""
        # Collection endpoint with no profile_id in the URL; writable set unset = deny all
        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("POST", "/profiles", json={"name": "New Profile"})

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "error" in response.json()
        assert response.json()["code"] == "write_access_denied"

    @pytest.mark.asyncio
    async def test_denies_create_profile_in_read_only_mode(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Regression test for issue #132: POST /profiles under NEXTDNS_READ_ONLY=true must be denied."""
        clean_env("NEXTDNS_READ_ONLY", "true")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("POST", "/profiles", json={"name": "New Profile"})

        # Should NOT call the parent request method
        mock_super_request.assert_not_called()
        assert response.status_code == 403
        assert "read-only mode" in response.json()["error"]
        assert response.json()["code"] == "write_access_denied"

    @pytest.mark.asyncio
    async def test_allows_create_profile_when_writable_set_allows(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None]
    ) -> None:
        """Test that POST /profiles is allowed when a writable profile set is configured."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")

        async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
            response = await client.request("POST", "/profiles", json={"name": "New Profile"})

        # Should call the parent request method
        mock_super_request.assert_called_once()
        assert response.status_code == 200


class TestAccessControlledClientStreaming:
    """Test that stream() enforces the same access control as request()."""

    @pytest.mark.asyncio
    async def test_stream_allows_read_when_permitted(self, clean_env: Callable[[str, str], None]) -> None:
        """Test that streaming is allowed when the profile is readable."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = httpx.Response(200, content=b"date,time\n2024-01-01,12:00:00\n")
            async with (
                AccessControlledClient(base_url="https://api.nextdns.io") as client,
                client.stream("GET", "/profiles/abc123/logs/download") as response,
            ):
                body = await response.aread()

        mock_send.assert_called_once()
        assert response.status_code == 200
        assert body == b"date,time\n2024-01-01,12:00:00\n"

    @pytest.mark.asyncio
    async def test_stream_denies_read_when_not_permitted(self, clean_env: Callable[[str, str], None]) -> None:
        """Test that streaming is denied (no network request) for unreadable profiles."""
        clean_env("NEXTDNS_READABLE_PROFILES", "xyz999")

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
                async with client.stream("GET", "/profiles/abc123/logs/download") as response:
                    assert response.status_code == 403
                    with pytest.raises(httpx.HTTPStatusError) as exc_info:
                        response.raise_for_status()
                    assert exc_info.value.response.status_code == 403

        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_denies_write_in_read_only_mode(self, clean_env: Callable[[str, str], None]) -> None:
        """Test that streaming a write is denied in read-only mode."""
        clean_env("NEXTDNS_WRITABLE_PROFILES", "abc123")
        clean_env("NEXTDNS_READ_ONLY", "true")

        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
                async with client.stream("DELETE", "/profiles/abc123/logs") as response:
                    assert response.status_code == 403

        mock_send.assert_not_called()


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


class TestAccessControlledClientRequestLogging:
    """Regression tests for issue #139: query-string PII must not be logged at INFO."""

    @pytest.mark.asyncio
    async def test_query_string_not_logged_at_info(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None], caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO request logs must contain only method + path, never the query string."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")

        sensitive_query = "search=secret-search-term&device=secret-device-id&cursor=secret-cursor-token"
        request_url = f"/profiles/abc123/logs?{sensitive_query}"

        with caplog.at_level(logging.DEBUG, logger="nextdns_mcp.client"):
            async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
                await client.request("GET", request_url)

        info_messages = [record.message for record in caplog.records if record.levelno == logging.INFO]
        # The PII must not appear in any INFO-level record.
        joined_info = "".join(info_messages)
        assert "secret-search-term" not in joined_info
        assert "secret-device-id" not in joined_info
        assert "secret-cursor-token" not in joined_info
        # No INFO request log may contain a query string at all.
        for record in caplog.records:
            if record.levelno == logging.INFO:
                assert "?" not in record.message, f"Query string leaked at INFO: {record.message}"

    @pytest.mark.asyncio
    async def test_full_url_logged_at_debug_not_info(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The full URL (with query) may only appear at DEBUG level."""
        clean_env("NEXTDNS_READABLE_PROFILES", "abc123")

        sensitive_query = "search=secret-search-term&device=secret-device-id&cursor=secret-cursor-token"
        request_url = f"/profiles/abc123/logs?{sensitive_query}"

        with caplog.at_level(logging.DEBUG, logger="nextdns_mcp.client"):
            async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
                await client.request("GET", request_url)

        info_messages = [record.message for record in caplog.records if record.levelno == logging.INFO]
        debug_messages = [record.message for record in caplog.records if record.levelno == logging.DEBUG]

        # INFO has the path without the query.
        assert any("/profiles/abc123/logs" in msg and "?" not in msg for msg in info_messages)
        # DEBUG carries the full URL including the sensitive query string.
        assert any(sensitive_query in msg for msg in debug_messages)

    @pytest.mark.asyncio
    async def test_denied_request_query_string_not_logged_at_warning(
        self, mock_super_request: Any, clean_env: Callable[[str, str], None], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail-closed denial WARNING must log only method + path, never the query string."""
        clean_env("NEXTDNS_READABLE_PROFILES", "ALL")

        sensitive_query = "search=secret-search-term&device=secret-device-id"
        request_url = f"/profiles/abc.def/logs?{sensitive_query}"

        with caplog.at_level(logging.DEBUG, logger="nextdns_mcp.client"):
            async with AccessControlledClient(base_url="https://api.nextdns.io") as client:
                response = await client.request("GET", request_url)

        mock_super_request.assert_not_called()
        assert response.status_code == 403
        # Response body behavior is unchanged: the error still names the full URL.
        assert response.json()["error"] == f"Forbidden URL: {request_url}"
        # No WARNING record may contain the query string.
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert warning_messages
        for msg in warning_messages:
            assert "secret-search-term" not in msg
            assert "secret-device-id" not in msg
            assert "?" not in msg, f"Query string leaked at WARNING: {msg}"
        assert any("/profiles/abc.def/logs" in msg for msg in warning_messages)


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
