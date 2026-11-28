"""Unit tests for MCP server creation (openapi.py create_mcp_server)."""

import logging
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import client as client_module
from nextdns_mcp.lifespan import server_lifespan
from nextdns_mcp.openapi import create_mcp_server
from nextdns_mcp.relaxation import StripExtraFieldsMiddleware
from nextdns_mcp.tools import doh as doh_module
from nextdns_mcp.tools import logs as logs_module


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

    def test_composite_registration_drift_is_refused(self, mock_api_key, monkeypatch):
        """A catalog that disagrees with the registered functions fails loudly.

        The names in ``catalog`` drive the initialize instructions and the
        validation scripts; a silent drift would describe tools that are not
        registered. The guard in ``build_mcp_server`` is the one place that
        compares the two, so exercise its failing branch directly.
        """
        from nextdns_mcp import server as server_module

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setattr(server_module, "composite_tool_names", lambda: ("plotAnalytics",))
        with pytest.raises(RuntimeError, match="Composite tool registration drifted from the catalog"):
            server_module.build_mcp_server()

    def test_reading_the_catalog_imports_no_prefab_ui(self):
        """``published_tool_names()`` is metadata: reading it renders nothing (issue #780).

        Schema validators and E2E scripts call it in processes that never serve
        a render; the composite tool functions are imported at the registration
        site instead, so the catalog read must not pull the Prefab UI component
        tree in. The probe runs in a fresh interpreter because this test module
        has already imported the server.
        """
        import subprocess
        import sys

        code = "import sys\n"
        code += "from nextdns_mcp.server import published_tool_names\n"
        code += "assert published_tool_names()\n"
        code += "assert not any(m == 'prefab_ui' or m.startswith('prefab_ui.') for m in sys.modules)\n"
        code += "print('ok')\n"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"

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
        ):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
        # The generated side still flags writes, so the comparison is meaningful.
        assert tools["deleteProfile"].annotations.destructive_hint is True

    @pytest.mark.asyncio
    async def test_generative_ui_tools_omitted_by_default(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_GENERATIVE_UI", raising=False)
        names = await self._published_names()
        assert "generate_prefab_ui" not in names
        assert "search_prefab_components" not in names

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
    @pytest.mark.parametrize(
        "arguments, bad_field",
        [
            ({"code": "print(1)", "data": ["value"]}, "data"),
            ({"code": 123}, "code"),
        ],
    )
    async def test_generate_ui_wrong_type_is_a_typed_error_over_mcp(
        self, mock_api_key, monkeypatch, arguments, bad_field
    ):
        """Signature-level refusals must reach callers as the typed contract.

        FastMCP validates a function tool's arguments before its body runs, so
        calling ``gen_tool.fn`` directly with a list bypasses the very check a
        real MCP client hits. Go through ``Client.call_tool``: the refusal must
        be a flagged ``invalid_argument`` with structured content, not a bare
        ValidationError with ``structured_content=None``.
        """
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool("generate_prefab_ui", arguments, raise_on_error=False)

        assert result.is_error is True
        structured = result.structured_content or {}
        assert structured["code"] == "invalid_argument"
        assert bad_field in structured["error"]

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
        """Server lifespan exit must close every long-lived client owned by the server."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp import lifespan as lifespan_module

        server = create_mcp_server()
        resources = lifespan_module.server_resources(server)

        with (
            client_module.api_client_owned(resources.api_client),
            doh_module.doh_client_owned(resources.doh_client),
        ):
            async with server._lifespan_manager():
                api_client = client_module.get_api_client()
                probe_client = resources.probe_client.get()
                doh_client = doh_module.current_doh_client_owner().get()
                redirect_client = resources.redirect_client.get()
                assert not api_client.is_closed
                assert not probe_client.is_closed
                assert not doh_client.is_closed
                assert not redirect_client.is_closed

        assert api_client.is_closed
        assert probe_client.is_closed
        assert doh_client.is_closed
        assert redirect_client.is_closed, "_server_lifespan left the download redirect client open"
        assert resources.api_client._client is None
        assert resources.probe_client._client is None
        assert resources.doh_client._client is None
        assert resources.redirect_client._client is None

    @pytest.mark.asyncio
    async def test_lifespan_cleanup_continues_after_a_close_fails(self, mock_api_key, monkeypatch):
        """A close that raises must not skip the closes after it (issue #774).

        The teardown runs through ``ServerResources.aclose()``'s
        ``AsyncExitStack``: every close is attempted, the first failure is
        re-raised only after the last close has run, and the lifespan's
        ``finally`` still resets the owner context vars.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp import lifespan as lifespan_module

        server = create_mcp_server()
        resources = lifespan_module.server_resources(server)

        async def boom() -> None:
            raise RuntimeError("injected probe close failure")

        monkeypatch.setattr(resources.probe_client, "aclose", boom)

        ambient_redirect_owner = logs_module._redirect_client_owner.get()
        with pytest.raises(RuntimeError, match="injected probe close failure"):
            async with server._lifespan_manager():
                api_client = resources.api_client.get()
                doh_client = resources.doh_client.get()
                redirect_client = resources.redirect_client.get()
                # The lifespan published the server's owners into this context;
                # the resets in its finally must run even though aclose raised.
                assert client_module.api_client_owner.get() is resources.api_client
                assert doh_module.doh_client_owner.get() is resources.doh_client
                assert logs_module._download_registry.get() is resources.downloads
                assert logs_module._redirect_client_owner.get() is resources.redirect_client

        assert api_client.is_closed, "the close before the failure must still have run"
        assert doh_client.is_closed, "a failed probe close skipped the DoH client close"
        assert redirect_client.is_closed, "a failed probe close skipped the redirect client close"
        assert client_module.api_client_owner.get() is None
        assert doh_module.doh_client_owner.get() is None
        assert logs_module._download_registry.get() is None
        assert logs_module._redirect_client_owner.get() is ambient_redirect_owner

    @pytest.mark.asyncio
    async def test_real_client_session_closes_http_clients_on_disconnect(self, mock_api_key, monkeypatch):
        """A real MCP client session (the public surface) closes both clients when it ends.

        The lifecycle above drives FastMCP's private ``_lifespan_manager``; this
        one goes through ``fastmcp.Client``, so it also proves the hook is wired
        into the transport the server actually runs under.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp import lifespan as lifespan_module

        server = create_mcp_server()
        resources = lifespan_module.server_resources(server)

        async with Client(server) as mcp_client:
            await mcp_client.list_tools()
            api_client = resources.api_client.get()
            doh_client = resources.doh_client.get()
            redirect_client = resources.redirect_client.get()
            assert not api_client.is_closed
            assert not doh_client.is_closed
            assert not redirect_client.is_closed

        assert api_client.is_closed
        assert doh_client.is_closed

        assert redirect_client.is_closed, "the Client session did not close the download redirect client"
        assert resources.redirect_client._client is None

    @pytest.mark.asyncio
    async def test_a_sibling_servers_shutdown_leaves_this_servers_clients_open(self, mock_api_key, monkeypatch):
        """One embedded server's shutdown must not close another live server's clients."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        from nextdns_mcp import lifespan as lifespan_module

        server_a = create_mcp_server()
        server_b = create_mcp_server()
        resources_a = lifespan_module.server_resources(server_a)
        resources_b = lifespan_module.server_resources(server_b)

        async with server_a._lifespan_manager():
            a_api_client = resources_a.api_client.get()
            a_probe_client = resources_a.probe_client.get()
            a_redirect_client = resources_a.redirect_client.get()
            # Server B starts and finishes WHILE server A is live:
            async with server_b._lifespan_manager():
                b_api_client = resources_b.api_client.get()
                b_probe_client = resources_b.probe_client.get()
                b_redirect_client = resources_b.redirect_client.get()
                assert a_api_client is not b_api_client
                assert a_probe_client is not b_probe_client, "two servers shared one probe transport"
                assert a_redirect_client is not b_redirect_client, "two servers shared one redirect client"
            # B's shutdown closed only B's clients:
            assert b_api_client.is_closed
            assert b_probe_client.is_closed
            assert b_redirect_client.is_closed
            assert not a_api_client.is_closed, "a sibling server's shutdown closed this server's client"
            assert not a_probe_client.is_closed, "a sibling server's shutdown closed this server's probe transport"
            assert not a_redirect_client.is_closed, "a sibling server's shutdown closed this server's redirect client"

        assert a_api_client.is_closed
        assert a_probe_client.is_closed
        assert a_redirect_client.is_closed


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
    async def test_plotAnalytics_enum_violation_is_a_typed_error_result(self, mock_api_key, monkeypatch):
        """A bogus Literal enum must not escape as a bare pydantic error (issue #778).

        FastMCP's argument model used to reject ``metric='bogus'`` before the
        tool ran, so the caller got ``structured_content=None`` and the strip
        middleware never annotated the extra argument it dropped. The refusal
        now comes back as the typed ``invalid_argument`` payload with
        ``ignored_arguments`` naming the dropped key.
        """
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool("plotAnalytics", {"metric": "bogus", "extra": True}, raise_on_error=False)

        assert result.is_error is True
        structured = result.structured_content or {}
        assert structured["code"] == "invalid_argument"
        assert "metric" in structured["error"]
        assert structured["ignored_arguments"] == ["extra"]

    @pytest.mark.asyncio
    async def test_overviewDashboard_enum_violation_is_a_typed_error_result(self, mock_api_key, monkeypatch):
        """The same contract for overviewDashboard's view_type enum (issue #778)."""
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool(
                "overviewDashboard", {"view_type": "bogus", "extra": True}, raise_on_error=False
            )

        assert result.is_error is True
        structured = result.structured_content or {}
        assert structured["code"] == "invalid_argument"
        assert "view_type" in structured["error"]
        assert structured["ignored_arguments"] == ["extra"]

    @pytest.mark.asyncio
    async def test_call_tool_alias_keeps_the_typed_error_and_resolved_from(self, mock_api_key, monkeypatch):
        """An alias resolved by call_tool keeps resolved_from on the typed refusal (issue #778)."""
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool(
                "call_tool",
                {"name": "plot analytics", "arguments": {"metric": "bogus", "extra": True}},
                raise_on_error=False,
            )

        assert result.is_error is True
        structured = result.structured_content or {}
        assert structured["code"] == "invalid_argument"
        assert structured["ignored_arguments"] == ["extra"]
        assert structured["resolved_from"] == "plot analytics"
        assert structured["resolved_name"] == "plotAnalytics"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("plotAnalytics", {"metric": "status", "profile_id": 315244}),
            ("overviewDashboard", {"profile_id": 315244}),
            ("dohLookup", {"domain": "example.com", "profile_id": 315244}),
        ],
    )
    async def test_an_integer_profile_id_is_still_coerced_and_accepted(
        self, mock_api_key, monkeypatch, tool_name, arguments
    ):
        """An all-digit profile id sent as a JSON integer must keep working (issue #778 review).

        ``OptionalProfileId`` declares a ``BeforeValidator`` that coerces an
        integer profile id to a string, and clients do send it that way. The
        typed-error handling must not refuse what the signature accepts: the
        call has to pass validation, coerce, and reach the access-control
        layer — where this profile, outside the readable set, is denied. A
        schema-level ``invalid_argument`` here is the regression this guards.
        """
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool(tool_name, arguments, raise_on_error=False)

        structured = result.structured_content or {}
        assert structured.get("code") == "read_access_denied", (
            f"the integer profile_id must be coerced and reach access control, got: {structured}"
        )

    @pytest.mark.asyncio
    async def test_composite_schemas_still_publish_their_enums(self, mock_api_key, monkeypatch):
        """In-tool validation must not cost the published JSON Schema enums (issue #778)."""
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        server = build_mcp_server()
        tools = {tool.name: tool for tool in await server._list_tools()}
        assert tools["plotAnalytics"].parameters["properties"]["metric"]["enum"] == [
            "status",
            "devices",
            "protocols",
            "queryTypes",
            "ipVersions",
            "dnssec",
            "encryption",
            "reasons",
            "ips",
        ]
        assert tools["overviewDashboard"].parameters["properties"]["view_type"]["enum"] == [
            "auto",
            "bar",
            "table",
            "pie",
        ]

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
        monkeypatch.setattr(doh_module.DohClientOwner, "get", lambda self: mock_client)

        server = build_mcp_server()
        async with Client(server) as client:
            result = await client.call_tool("dohLookup", {"domain": "example.com"}, raise_on_error=False)

        assert result.is_error is False
        assert result.structured_content["data"]["Answer"][0]["data"] == "142.250.190.46"


class TestDownloadOwnershipIsPerServer:
    """One embedded server's shutdown must not delete another live server's CSVs."""

    @pytest.mark.asyncio
    async def test_a_sibling_servers_shutdown_leaves_this_servers_downloads_alive(
        self, mock_api_key, monkeypatch, tmp_path
    ):
        server_a_csv = tmp_path / "a" / "download.csv"
        server_b_csv = tmp_path / "b" / "download.csv"
        for csv in (server_a_csv, server_b_csv):
            csv.parent.mkdir(parents=True)
            csv.write_text("csv")

        server_a = create_mcp_server()
        server_b = create_mcp_server()

        async with server_lifespan(server_a):
            logs_module._note_successful_download(str(server_a_csv), True)
            # Server B starts and finishes WHILE server A is live:
            async with server_lifespan(server_b):
                logs_module._note_successful_download(str(server_b_csv), True)
            # B's shutdown reclaims only B's registry:
            assert not server_b_csv.exists()
            assert server_a_csv.exists(), "a sibling server's shutdown deleted this server's tracked CSV"

        # A's own shutdown reclaims its own file:
        assert not server_a_csv.exists()
