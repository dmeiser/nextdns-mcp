"""Unit tests for MCP server creation (openapi.py create_mcp_server)."""

import logging
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import client as client_module
from nextdns_mcp.openapi import StripExtraFieldsMiddleware, create_mcp_server
from nextdns_mcp.tools import doh as doh_module


class TestCreateMcpServer:
    """Tests for the create_mcp_server() function."""

    def test_returns_named_fastmcp_instance(self, mock_api_key, monkeypatch):
        """create_mcp_server returns a configured FastMCP instance."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        result = create_mcp_server()

        assert isinstance(result, FastMCP)
        assert result.name == "NextDNS MCP Server"

    def test_registers_strip_extra_fields_middleware(self, mock_api_key, monkeypatch):
        """create_mcp_server installs the StripExtraFieldsMiddleware."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        result = create_mcp_server()

        middlewares = getattr(result, "middleware", None) or []
        assert any(isinstance(m, StripExtraFieldsMiddleware) for m in middlewares)

    @pytest.mark.asyncio
    async def test_no_tools_until_registered_by_server(self, mock_api_key, monkeypatch):
        """The server starts with no tools.

        ``create_mcp_server`` only builds the machinery around the catalog
        (disclosure, argument relaxation, the call log, the health endpoint);
        ``server.build_mcp_server`` registers the generated tools afterwards, so
        this function must expose none of them.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        # The machinery under test is the bare server; disclosure (the default
        # now) would list its synthetic pair before any catalog is registered.
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

        result = create_mcp_server()

        tools = await result.list_tools()
        assert tools == []

    def test_logs_default_profile_when_set(self, mock_api_key, monkeypatch, caplog):
        """create_mcp_server logs the default profile when one is configured."""
        caplog.set_level(logging.INFO)
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        create_mcp_server()

        assert "Default profile: abc123" in caplog.text

    def test_no_default_profile_log(self, mock_api_key, monkeypatch, caplog):
        """create_mcp_server does not log a default profile when none is set."""
        caplog.set_level(logging.INFO)
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.delenv("NEXTDNS_DEFAULT_PROFILE", raising=False)

        create_mcp_server()

        assert "Default profile:" not in caplog.text


class TestProductionServerTools:
    """The production server's catalog is the generated one, and nothing else."""

    @pytest.fixture(autouse=True)
    def _full_listing(self, monkeypatch):
        """The catalog's shape is only visible under the full listing.

        Disclosure defaults to bm25 now, which lists just the synthetic pair;
        these tests assert the shape of the catalog itself, so they pin the
        explicit opt-out and build the server fresh (the lazy singleton would
        carry whatever mode the first server in the process saw).
        """
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

    @staticmethod
    async def _published_names() -> set[str]:
        from nextdns_mcp.server import build_mcp_server

        return {tool.name for tool in await build_mcp_server().list_tools()}

    @pytest.mark.asyncio
    async def test_the_catalog_is_what_the_server_says_it_is(self):
        """``published_tool_names()`` is the catalog, not a description of it.

        The ``initialize`` instructions are written from that list before the tools
        exist, so a catalog that drifts from it would have the server describing
        tools it never registered.
        """
        from nextdns_mcp.server import published_tool_names

        assert await self._published_names() == set(published_tool_names())
        assert len(published_tool_names()) == len(set(published_tool_names()))

    @pytest.mark.asyncio
    async def test_the_api_operations_are_published_by_their_own_names(self):
        """The endpoints are what a caller is given: the spec's names, one per call."""
        names = await self._published_names()
        assert {"listProfiles", "createProfile", "addToDenylist", "getLogs", "getAnalyticsDomains"} <= names

    @pytest.mark.asyncio
    async def test_the_operations_the_server_performs_itself_are_published(self):
        """No endpoint describes a chart or a DoH lookup, so they are registered by hand."""
        names = await self._published_names()
        assert {"plotAnalytics", "dohLookup"} <= names

    @pytest.mark.asyncio
    async def test_the_composite_tools_announce_themselves_as_read_only(self):
        """The generated tools derive their hints from the endpoint method; the
        composites have no endpoint, so their annotations are stated by hand —
        a caller scanning for destructive tools must see them as safe."""
        from nextdns_mcp.server import build_mcp_server

        tools = {tool.name: tool for tool in await build_mcp_server().list_tools()}
        for name in (
            "plotAnalytics",
            "dohLookup",
            "overviewDashboard",
            "gafamAnalytics",
            "securityAnalytics",
            "reasonsAnalytics",
            "deviceAnalytics",
            "ipAnalytics",
            "rootDomainAnalytics",
            "countryAnalytics",
        ):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
        # The generated side still flags writes, so the comparison is meaningful.
        assert tools["deleteProfile"].annotations.destructive_hint is True

    @pytest.mark.asyncio
    async def test_generative_ui_tools_published_when_enabled(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
        names = await self._published_names()
        assert "generate_prefab_ui" in names
        assert "search_prefab_components" in names

    @pytest.mark.asyncio
    async def test_generative_ui_tools_omitted_when_disabled(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "off")
        names = await self._published_names()
        assert "generate_prefab_ui" not in names
        assert "search_prefab_components" not in names

    @pytest.mark.asyncio
    async def test_the_grouped_area_tools_are_not_published(self):
        """The eight area tools are gone, and so is the second choice they imposed.

        A caller that found both ``manageLists`` and ``addToDenylist`` had to decide
        which spelling of one API call to make; publishing both is what this removes.
        """
        grouped = {
            "manageProfiles",
            "manageSettings",
            "manageLists",
            "manageRewrites",
            "manageLogs",
            "queryAnalytics",
        }
        assert not (await self._published_names()) & grouped, (
            f"Grouped tools still registered: {await self._published_names() & grouped}"
        )

    @pytest.mark.asyncio
    async def test_the_streaming_endpoint_is_not_published(self):
        """``streamLogs`` is a Server-Sent Events stream; a tool call cannot carry it."""
        assert "streamLogs" not in await self._published_names()

    @pytest.mark.asyncio
    async def test_a_generated_tool_names_the_endpoint_it_calls(self):
        """The description says which API call a name executes, so the name is checkable."""
        from nextdns_mcp.server import build_mcp_server

        tools = {tool.name: tool for tool in await build_mcp_server().list_tools()}
        assert tools["addToDenylist"].description.rstrip().endswith("NextDNS API: POST /profiles/{profile_id}/denylist")


class TestServerLifespan:
    """Test that server lifespan shuts down long-lived HTTP clients (issue #277)."""

    @pytest.mark.asyncio
    async def test_server_lifespan_closes_http_clients(self, mock_api_key, monkeypatch):
        """Server lifespan exit must close both API and DoH clients and reset singletons."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_client", None)
        monkeypatch.setattr(doh_module, "_doh_client", None)

        server = create_mcp_server()
        api_client = client_module.get_api_client()
        doh_client = doh_module._get_doh_client()

        try:
            assert not api_client.is_closed
            assert not doh_client.is_closed

            async with server._lifespan_manager():
                pass

            assert api_client.is_closed
            assert doh_client.is_closed
            assert client_module._client is None
            assert doh_module._doh_client is None
        finally:
            if not api_client.is_closed:
                await api_client.aclose()
            if not doh_client.is_closed:
                await doh_client.aclose()

    @pytest.mark.asyncio
    async def test_real_client_session_closes_http_clients_on_disconnect(self, mock_api_key, monkeypatch):
        """A real MCP client session (the public surface) closes both clients when it ends.

        The lifecycle above drives FastMCP's private ``_lifespan_manager``; this
        one goes through ``fastmcp.Client``, so it also proves the hook is wired
        into the transport the server actually runs under.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(client_module, "_client", None)
        monkeypatch.setattr(doh_module, "_doh_client", None)

        server = create_mcp_server()

        async with Client(server) as mcp_client:
            await mcp_client.list_tools()
            api_client = client_module.get_api_client()
            doh_client = doh_module._get_doh_client()
            assert not api_client.is_closed
            assert not doh_client.is_closed

        assert api_client.is_closed
        assert doh_client.is_closed
        assert client_module._client is None
        assert doh_module._doh_client is None


class TestCompositeErrorResultsAreFlagged:
    """The hand-written composites must surface typed failures as ``isError``.

    A bare dict return is wrapped by FastMCP as a SUCCESSFUL result, so every
    typed error payload the composites build used to reach callers with
    ``isError=False`` - a refusal masquerading as a normal answer. The
    generated tools set the flag explicitly; the composites now do too.
    """

    @pytest.mark.asyncio
    async def test_dohLookup_denial_is_an_error_result(self, mock_api_key, monkeypatch):
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "zzz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "zzz999")

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool(
                "dohLookup", {"domain": "example.com", "profile_id": "abc123"}, raise_on_error=False
            )

        assert result.is_error is True, "a read-access denial must surface as isError, not a success"
        assert result.structured_content["code"] == "read_access_denied"

    @pytest.mark.asyncio
    async def test_plotAnalytics_argument_refusal_is_an_error_result(self, mock_api_key, monkeypatch):
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        server = build_mcp_server()
        async with Client(server) as client:
            # A partials value the endpoint does not accept is refused by the
            # tool itself, after the schema check: the refusal is a typed error
            # payload, not a successful result.
            result = await client.call_tool(
                "plotAnalytics", {"metric": "status", "partials": "bogus"}, raise_on_error=False
            )

        assert result.is_error is True, "an argument refusal must surface as isError, not a success"
        assert "code" in result.structured_content

    @pytest.mark.asyncio
    async def test_dohLookup_success_stays_a_normal_result(self, mock_api_key, monkeypatch, mock_doh_client=None):
        """The wrapper must not flag successful payloads - only typed errors."""
        from nextdns_mcp.server import build_mcp_server
        from nextdns_mcp.tools import doh as doh_module

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = Mock()
        mock_response.json.return_value = {
            "Status": 0,
            "Question": [{"name": "example.com.", "type": 1}],
            "Answer": [{"name": "example.com.", "type": 1, "TTL": 300, "data": "142.250.190.46"}],
        }
        mock_client.get.return_value = mock_response
        monkeypatch.setattr(doh_module, "_doh_client", mock_client)

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool("dohLookup", {"domain": "example.com"}, raise_on_error=False)

        assert result.is_error is False
        assert result.structured_content["data"]["Answer"][0]["data"] == "142.250.190.46"


class TestDownloadOwnershipIsPerServer:
    """One embedded server's shutdown must not delete another live server's CSVs."""

    @staticmethod
    async def _noop() -> None:
        return None

    @pytest.mark.asyncio
    async def test_a_sibling_servers_shutdown_leaves_this_servers_downloads_alive(
        self, mock_api_key, monkeypatch, tmp_path
    ):
        from nextdns_mcp import openapi as openapi_module
        from nextdns_mcp.tools import logs as logs_module

        monkeypatch.setattr(openapi_module, "close_api_client", self._noop)
        monkeypatch.setattr(openapi_module, "close_doh_client", self._noop)

        server_a_csv = tmp_path / "a" / "download.csv"
        server_b_csv = tmp_path / "b" / "download.csv"
        for csv in (server_a_csv, server_b_csv):
            csv.parent.mkdir(parents=True)
            csv.write_text("csv")

        async with openapi_module._server_lifespan(None):  # type: ignore[arg-type]
            logs_module._note_successful_download(str(server_a_csv), True)
            # Server B starts and finishes WHILE server A is live:
            async with openapi_module._server_lifespan(None):  # type: ignore[arg-type]
                logs_module._note_successful_download(str(server_b_csv), True)
            # B's shutdown reclaims only B's registry:
            assert not server_b_csv.exists()
            assert server_a_csv.exists(), "a sibling server's shutdown deleted this server's tracked CSV"

        # A's own shutdown reclaims its own file:
        assert not server_a_csv.exists()
