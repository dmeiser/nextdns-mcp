"""Unit tests for the custom dohLookup tool."""

import logging
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import nextdns_mcp.tools.doh as doh_module
from nextdns_mcp import utils as utils_module
from nextdns_mcp.tools.doh import _dohLookup_impl as dohLookup


@pytest.fixture(autouse=True)
def allow_doh_read_access(monkeypatch):
    """Allow all DoH lookups by opening the readable profile gate via the env."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


@pytest.fixture(autouse=True)
async def mock_doh_client(monkeypatch):
    """Install a mock persistent DoH client so no test hits the network.

    doh.py holds its persistent AsyncClient behind an explicit owner for
    connection reuse (issues #149, #642), so the fixture adopts the mock into
    one owner and publishes it for the test's context.
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = Mock()
    mock_response.json.return_value = {
        "Status": 0,
        "Question": [{"name": "google.com.", "type": 1}],
        "Answer": [{"name": "google.com.", "type": 1, "TTL": 300, "data": "142.250.190.46"}],
    }
    mock_client.get.return_value = mock_response
    with doh_module.doh_client_owned(doh_module.DohClientOwner(client=mock_client)):
        yield mock_client


class TestDohLookup:
    """Test the dohLookup custom tool."""

    @pytest.mark.asyncio
    async def test_doh_lookup_denies_unreadable_profile(self, mock_profile_id, monkeypatch):
        """Test that dohLookup respects read access controls."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "zzz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "zzz999")
        result = await dohLookup("example.com", mock_profile_id, "A")
        assert "error" in result
        assert "Read access denied" in result["error"]
        assert result["code"] == "read_access_denied"

    @pytest.mark.asyncio
    async def test_doh_lookup_denies_before_validating_record_type(self, mock_profile_id, monkeypatch, mock_doh_client):
        """Access control precedes argument validation on the DoH path.

        A profile that is not readable is reported as read_access_denied even
        when the record type is also invalid, so the error code callers branch
        on stays the authorization one.
        """
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "zzz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "zzz999")

        result = await dohLookup("example.com", mock_profile_id, "BOGUS")

        assert result["code"] == "read_access_denied"
        assert "Read access denied" in result["error"]
        assert mock_doh_client.get.await_count == 0

    @pytest.mark.asyncio
    async def test_doh_lookup_rejects_invalid_profile_id(self, monkeypatch):
        """Test that dohLookup rejects unsafe profile IDs."""
        result = await dohLookup("example.com", "abc/def", "A")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]
        assert result["code"] == "invalid_profile_id"

    @pytest.mark.asyncio
    async def test_doh_lookup_basic_query(self, mock_profile_id):
        """Test basic DoH lookup."""
        result = await dohLookup("google.com", mock_profile_id, "A")

        assert "Status" in result["data"]
        assert result["data"]["Status"] == 0
        assert "_metadata" in result
        assert result["_metadata"]["profile_id"] == mock_profile_id
        assert result["_metadata"]["query_domain"] == "google.com"
        assert result["_metadata"]["query_type"] == "A"

    @pytest.mark.asyncio
    async def test_doh_lookup_uses_default_profile(self, monkeypatch, mock_doh_client):
        """Test that dohLookup uses NEXTDNS_DEFAULT_PROFILE when profile_id not provided."""
        test_profile = "abc123"

        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", test_profile)
        monkeypatch.setenv("NEXTDNS_API_KEY", "dummy_key")

        mock_response = Mock()
        mock_response.json.return_value = {"Status": 0, "Answer": []}
        mock_doh_client.get.return_value = mock_response

        result = await dohLookup("example.com")

        assert "data" in result
        assert "_metadata" in result
        assert result["_metadata"]["profile_id"] == test_profile
        assert result["_metadata"]["query_domain"] == "example.com"
        assert result["_metadata"]["query_type"] == "A"

    @pytest.mark.asyncio
    async def test_doh_lookup_no_profile_error(self, monkeypatch):
        """Test error when no profile_id provided and no default set."""
        monkeypatch.delenv("NEXTDNS_DEFAULT_PROFILE", raising=False)

        result = await dohLookup("example.com", None, "A")

        assert "error" in result
        assert "No profile_id provided" in result["error"]
        assert "NEXTDNS_DEFAULT_PROFILE" in result["hint"]

    @pytest.mark.asyncio
    async def test_doh_lookup_invalid_record_type(self, mock_profile_id):
        """Test error handling for invalid record type."""
        result = await dohLookup("example.com", mock_profile_id, "INVALID")

        assert "error" in result
        assert "Invalid record type" in result["error"]
        assert result["code"] == "invalid_record_type"
        assert "valid_types" in result

    @pytest.mark.asyncio
    async def test_doh_lookup_valid_record_types(self, mock_profile_id):
        """Test all valid DNS record types are accepted."""
        valid_types = ["A", "AAAA", "CNAME", "MX", "NS", "PTR", "SOA", "TXT", "SRV", "CAA"]

        for record_type in valid_types:
            result = await dohLookup("example.com", mock_profile_id, record_type)
            assert "error" not in result
            assert "_metadata" in result
            assert result["_metadata"]["query_type"] == record_type

    @pytest.mark.asyncio
    async def test_doh_lookup_adds_metadata(self, mock_profile_id):
        """Test that dohLookup adds helpful metadata to response."""
        result = await dohLookup("example.com", mock_profile_id, "A")

        assert "_metadata" in result
        assert "profile_id" in result["_metadata"]
        assert "query_domain" in result["_metadata"]
        assert "query_type" in result["_metadata"]
        assert "doh_endpoint" in result["_metadata"]

    @pytest.mark.asyncio
    async def test_doh_lookup_status_descriptions(self, mock_profile_id, mock_doh_client):
        """Test that status codes get human-readable descriptions."""
        status_tests = [
            (0, "NOERROR - Success"),
            (2, "SERVFAIL - Server failure"),
            (3, "NXDOMAIN - Non-existent domain"),
        ]

        for status_code, expected_desc in status_tests:
            mock_response = Mock()
            mock_response.json.return_value = {"Status": status_code, "Answer": []}
            mock_doh_client.get.return_value = mock_response

            result = await dohLookup("example.com", mock_profile_id, "A")

            assert "_metadata" in result
            assert result["_metadata"]["status_description"] == expected_desc

    @pytest.mark.asyncio
    async def test_doh_lookup_without_a_status_field(self, mock_profile_id, mock_doh_client):
        """A resolver reply that omits ``Status`` still returns its data.

        ``Status`` is optional in practice, so a response without it must not
        raise and must not carry a status description.
        """
        mock_response = Mock()
        mock_response.json.return_value = {"Answer": [{"name": "example.com.", "type": 1}]}
        mock_doh_client.get.return_value = mock_response

        result = await dohLookup("example.com", mock_profile_id, "A")

        assert "error" not in result
        assert result["data"] == {"Answer": [{"name": "example.com.", "type": 1}]}
        assert "status_description" not in result["_metadata"]

    @pytest.mark.asyncio
    async def test_doh_lookup_http_error(self, mock_profile_id, mock_doh_client):
        """Test error handling for HTTP errors surfaces a typed http_error payload."""
        http_exc = httpx.HTTPError("Connection failed")
        # ``content`` is bytes on a real httpx.Response (the bounded body read
        # slices it), so the mock stays faithful to that shape.
        http_exc.response = Mock(status_code=500, text="", content=b"")
        mock_doh_client.get.side_effect = http_exc

        result = await dohLookup("example.com", mock_profile_id, "A")

        assert "error" in result
        assert "HTTP error" in result["error"]
        assert result["code"] == "http_error"
        assert result["status_code"] == 500
        assert result["profile_id"] == mock_profile_id

    @pytest.mark.asyncio
    async def test_doh_lookup_status_error_log_hides_the_merged_query_string(
        self, mock_profile_id, mock_doh_client, caplog
    ):
        """A failing DoH status must not log the merged query string built from the domain."""
        domain = "alice-bank.com"
        request = httpx.Request(
            "GET", f"https://dns.nextdns.io/{mock_profile_id}/dns-query", params={"name": domain, "type": "A"}
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            httpx.Response(429, request=request).raise_for_status()
        assert f"?name={domain}" in str(exc_info.value)

        mock_doh_client.get.return_value.raise_for_status.side_effect = exc_info.value

        with caplog.at_level(logging.DEBUG, logger="nextdns_mcp.tools.doh"):
            result = await dohLookup(domain, mock_profile_id, "A")

        assert result["code"] == "http_error"
        assert result["status_code"] == 429
        assert result["domain"] == domain
        error_messages = [record.message for record in caplog.records if record.levelno >= logging.WARNING]
        assert error_messages
        for msg in error_messages:
            assert "?" not in msg, f"Query string leaked into the DoH error log: {msg}"
        assert any("429" in msg for msg in error_messages)

    @pytest.mark.asyncio
    async def test_doh_lookup_generic_exception(self, mock_profile_id, mock_doh_client):
        """Test error handling for unexpected exceptions."""
        mock_doh_client.get.side_effect = Exception("Unexpected error")

        result = await dohLookup("example.com", mock_profile_id, "A")

        assert "error" in result
        assert "Unexpected error" in result["error"]
        assert result["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_doh_lookup_correct_url_format(self, mock_profile_id):
        """Test that DoH endpoint URL is correctly formatted."""
        result = await dohLookup("example.com", mock_profile_id, "A")

        expected_url = f"https://dns.nextdns.io/{mock_profile_id}/dns-query?name=example.com&type=A"
        assert "_metadata" in result
        assert result["_metadata"]["doh_endpoint"] == expected_url

    @pytest.mark.asyncio
    async def test_doh_lookup_case_insensitive_record_type(self, mock_profile_id):
        """Test that record type is case-insensitive."""
        # Test lowercase
        result = await dohLookup("example.com", mock_profile_id, "a")
        assert "_metadata" in result
        assert result["_metadata"]["query_type"] == "A"

        # Test mixed case
        result = await dohLookup("example.com", mock_profile_id, "AaAa")
        assert "_metadata" in result
        assert result["_metadata"]["query_type"] == "AAAA"


class TestMetadataKeyCollision:
    """Regression tests for issue #166: wrapper shape prevents collisions."""

    @pytest.mark.asyncio
    async def test_doh_response_with_metadata_record_does_not_collide(self, mock_profile_id, mock_doh_client):
        """A DoH answer literally named ``_metadata`` must not be overwritten by injected metadata."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "Status": 0,
            "Answer": [{"name": "_metadata.example.com.", "type": 16, "TTL": 60, "data": "opaque"}],
            "_metadata": "attacker-supplied-value",
        }
        mock_doh_client.get.return_value = mock_response

        result = await dohLookup("example.com", mock_profile_id, "A")

        # The raw DoH payload is preserved verbatim under "data".
        assert "_metadata" in result["data"]
        assert result["data"]["_metadata"] == "attacker-supplied-value"
        # Injected metadata lives in the wrapper and is intact.
        assert result["_metadata"]["profile_id"] == mock_profile_id
        assert result["_metadata"]["query_domain"] == "example.com"
        assert result["_metadata"]["query_type"] == "A"


class TestDohClientReuse:
    """Regression tests for issue #149: the DoH tool must reuse one client."""

    @pytest.mark.asyncio
    async def test_doh_lookup_reuses_client_across_calls(self, mock_profile_id, mock_doh_client):
        """Consecutive lookups must share the one client their owner holds."""
        first = await dohLookup("example.com", mock_profile_id, "A")
        second = await dohLookup("example.org", mock_profile_id, "A")

        assert "error" not in first
        assert "error" not in second
        assert mock_doh_client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_owner_get_creates_client_once(self, monkeypatch):
        """An owner must build exactly one client and reuse it."""
        created = []

        def fake_client(timeout):
            client = AsyncMock()
            created.append(client)
            return client

        monkeypatch.setattr(doh_module.httpx, "AsyncClient", fake_client)

        owner = doh_module.DohClientOwner()
        first = owner.get()
        second = owner.get()

        assert first is second
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_owner_get_applies_configured_timeout(self, monkeypatch):
        """The persistent client must honor NEXTDNS_HTTP_TIMEOUT."""
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "7.5")

        client = doh_module.DohClientOwner().get()

        assert client.timeout is not None
        assert client.timeout.connect == 7.5

    @pytest.mark.asyncio
    async def test_aclose_closes_and_resets(self, monkeypatch):
        """aclose closes the owner's client and resets it for a fresh one."""
        owner = doh_module.DohClientOwner()

        client = owner.get()
        assert not client.is_closed

        await owner.aclose()
        assert client.is_closed
        assert owner._client is None

    @pytest.mark.asyncio
    async def test_aclose_noop_when_none(self, monkeypatch):
        """aclose is a safe no-op when no client was created."""
        await doh_module.DohClientOwner().aclose()

    @pytest.mark.asyncio
    async def test_close_doh_client_closes_the_published_owner(self, monkeypatch):
        """The module delegate closes the currently published owner's client."""
        owner = doh_module.DohClientOwner()
        with doh_module.doh_client_owned(owner):
            client = owner.get()
            assert not client.is_closed
            await doh_module.close_doh_client()
            assert client.is_closed

    @pytest.mark.asyncio
    async def test_close_doh_client_without_an_owner_raises(self, monkeypatch):
        """The module delegate fails loudly when no owner is published."""
        from nextdns_mcp.errors import StateOwnershipError

        token = doh_module.doh_client_owner.set(None)
        try:
            with pytest.raises(StateOwnershipError):
                await doh_module.close_doh_client()
        finally:
            doh_module.doh_client_owner.reset(token)


class TestOptionalProfileIdCoercion:
    """Test OptionalProfileId coerces int profile_id to str via FastMCP TypeAdapter."""

    @pytest.mark.asyncio
    async def test_integer_profile_id_coerced_to_str(self):
        """Numeric-only profile ID (e.g. 315244) arriving as int must be coerced to str."""
        from fastmcp import FastMCP

        from nextdns_mcp.coercion import OptionalProfileId

        mcp = FastMCP("test", strict_input_validation=False)

        @mcp.tool()
        async def dohLookup(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> str:
            return f"{profile_id!r}"

        tool = await mcp.get_tool("dohLookup")
        result = await tool.run({"domain": "example.com", "profile_id": 315244, "record_type": "A"})
        assert result.content[0].text == "'315244'"

    @pytest.mark.asyncio
    async def test_none_profile_id_stays_none(self):
        """None profile_id must not be coerced to the string 'None'."""
        from fastmcp import FastMCP

        from nextdns_mcp.coercion import OptionalProfileId

        mcp = FastMCP("test", strict_input_validation=False)

        @mcp.tool()
        async def dohLookup(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> str:
            return f"{profile_id!r}"

        tool = await mcp.get_tool("dohLookup")
        result = await tool.run({"domain": "example.com", "record_type": "A"})
        assert result.content[0].text == "None"

    @pytest.mark.asyncio
    async def test_string_profile_id_unchanged(self):
        """String profile IDs (the normal case) pass through unchanged."""
        from fastmcp import FastMCP

        from nextdns_mcp.coercion import OptionalProfileId

        mcp = FastMCP("test", strict_input_validation=False)

        @mcp.tool()
        async def dohLookup(domain: str, profile_id: OptionalProfileId = None, record_type: str = "A") -> str:
            return f"{profile_id!r}"

        tool = await mcp.get_tool("dohLookup")
        result = await tool.run({"domain": "example.com", "profile_id": "4faf86", "record_type": "A"})
        assert result.content[0].text == "'4faf86'"


class TestProfileResolutionGuard:
    """A lookup with no profile to resolve must not go to the resolver anyway.

    ``resolve_profile_id`` returns ``(profile, error)``, and every real failure path
    fills the error. The fail-closed ``internal_error`` lives in the shared
    ``resolve_default_profile`` wrapper now, and this test pins it through the
    tool: without it the lookup would build a DoH query for an empty profile
    subdomain, which is a request to a URL nobody chose.
    """

    @pytest.mark.asyncio
    async def test_a_resolution_with_no_profile_and_no_error_is_an_internal_error(self, monkeypatch):
        monkeypatch.setattr(utils_module, "resolve_profile_id", lambda profile_id, **kwargs: (None, None))

        def no_client(self) -> httpx.AsyncClient:
            raise AssertionError("a lookup with no resolved profile must not build a DoH client")

        monkeypatch.setattr(doh_module.DohClientOwner, "get", no_client)

        result = await dohLookup("example.com")
        assert result["code"] == "internal_error"
        assert "profile resolution failed" in result["error"].lower()
