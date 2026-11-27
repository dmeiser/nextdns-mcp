"""The five process-global singletons now have explicit owners (issue #642).

Every piece of mutable state the server used to keep in a module-global
singleton - the API client, the MCP server, the DoH client, the readiness
cache, and the standalone download registry - belongs to an explicit owner
that is published into the current context (by a server lifespan, or by a
standalone caller). These tests pin the contract:

- no read or write path falls back to process-global storage: with no owner
  active, the module-level accessors raise ``StateOwnershipError``;
- two independent owners in one process each use their own state, including
  when one owner shuts down.
"""

import os

import pytest
from fastmcp import Client, FastMCP

from nextdns_mcp import client as client_module
from nextdns_mcp import server as server_module
from nextdns_mcp import state as state_module
from nextdns_mcp.client import current_api_client_owner
from nextdns_mcp.errors import StateOwnershipError
from nextdns_mcp.lifespan import ServerResources, server_resources
from nextdns_mcp.openapi import create_mcp_server
from nextdns_mcp.tools import doh as doh_module
from nextdns_mcp.tools import logs as logs_module
from nextdns_mcp.tools.doh import current_doh_client_owner
from nextdns_mcp.tools.logs import current_download_registry


class TestRequestResourcesResolution:
    """The FastMCP request channel resolves the serving server's resources."""

    def test_no_request_in_flight_resolves_to_none(self):
        assert state_module.request_resources() is None

    def test_a_non_dict_lifespan_context_resolves_to_none(self):
        """A request bound without this package's lifespan yields no resources."""
        from fastmcp.server.dependencies import fastmcp_request_ctx

        class _BoundRequest:
            lifespan_context = None

        token = fastmcp_request_ctx.set(_BoundRequest())  # type: ignore[arg-type]
        try:
            assert state_module.request_resources() is None
        finally:
            fastmcp_request_ctx.reset(token)

    def test_a_bound_lifespan_context_resolves_the_resources(self):
        resources = ServerResources()
        from fastmcp.server.dependencies import fastmcp_request_ctx

        class _BoundRequest:
            def __init__(self, lifespan_context):
                self.lifespan_context = lifespan_context

        bound = fastmcp_request_ctx.set(_BoundRequest({state_module.RESOURCES_LIFESPAN_KEY: resources}))  # type: ignore[arg-type]
        try:
            assert state_module.request_resources() is resources
        finally:
            fastmcp_request_ctx.reset(bound)

    def test_the_active_context_takes_precedence_over_the_request_channel(self, monkeypatch):
        """A mounted child's active Context wins over the parent's request lifespan."""
        from fastmcp.server.dependencies import fastmcp_request_ctx

        own, parent = ServerResources(), ServerResources()

        class _Holder:
            def __init__(self, lifespan_context):
                self.lifespan_context = lifespan_context

        context = _Holder({state_module.RESOURCES_LIFESPAN_KEY: own})
        request = _Holder({state_module.RESOURCES_LIFESPAN_KEY: parent})

        monkeypatch.setattr(state_module, "get_context", lambda: context)
        token = fastmcp_request_ctx.set(request)  # type: ignore[arg-type]
        try:
            assert state_module.request_resources() is own
        finally:
            fastmcp_request_ctx.reset(token)

    def test_an_active_context_without_resources_falls_back_to_the_request_channel(self, monkeypatch):
        """A Context from another server must not hide a bound owner's resources."""
        from fastmcp.server.dependencies import fastmcp_request_ctx

        resources = ServerResources()

        class _Holder:
            def __init__(self, lifespan_context):
                self.lifespan_context = lifespan_context

        context = _Holder({})
        request = _Holder({state_module.RESOURCES_LIFESPAN_KEY: resources})

        monkeypatch.setattr(state_module, "get_context", lambda: context)
        token = fastmcp_request_ctx.set(request)  # type: ignore[arg-type]
        try:
            assert state_module.request_resources() is resources
        finally:
            fastmcp_request_ctx.reset(token)

    def test_the_registry_resolver_uses_the_request_channel(self):
        """A served request registers downloads with its own server's registry."""
        resources = ServerResources()
        from fastmcp.server.dependencies import fastmcp_request_ctx

        class _BoundRequest:
            def __init__(self, lifespan_context):
                self.lifespan_context = lifespan_context

        bound = fastmcp_request_ctx.set(_BoundRequest({state_module.RESOURCES_LIFESPAN_KEY: resources}))  # type: ignore[arg-type]
        try:
            assert logs_module.current_download_registry() is resources.downloads
        finally:
            fastmcp_request_ctx.reset(bound)

    def test_the_redirect_client_resolver_uses_the_request_channel(self):
        """A served request downloads through its own server's redirect client."""
        resources = ServerResources()
        from fastmcp.server.dependencies import fastmcp_request_ctx

        class _BoundRequest:
            def __init__(self, lifespan_context):
                self.lifespan_context = lifespan_context

        token = logs_module._redirect_client_owner.set(None)
        bound = fastmcp_request_ctx.set(_BoundRequest({state_module.RESOURCES_LIFESPAN_KEY: resources}))  # type: ignore[arg-type]
        try:
            assert logs_module.current_redirect_client_owner() is resources.redirect_client
        finally:
            fastmcp_request_ctx.reset(bound)
            logs_module._redirect_client_owner.reset(token)

    def test_server_resources_rejects_an_instance_without_them(self):
        class _Bare:
            pass

        with pytest.raises(RuntimeError):
            server_resources(_Bare())  # type: ignore[arg-type]


class TestMountedServerResourceResolution:
    """A mounted NextDNS child resolves its own state, not the parent's (issue #752)."""

    @staticmethod
    def _server_with_probe() -> FastMCP:
        child = create_mcp_server()
        expected = server_resources(child)

        @child.tool(name="state_owner_probe")
        def probe() -> dict[str, bool]:
            return {
                "api": current_api_client_owner() is expected.api_client,
                "doh": current_doh_client_owner() is expected.doh_client,
                "downloads": current_download_registry() is expected.downloads,
            }

        return child

    async def test_a_mounted_child_resolves_all_three_owners(self, mock_api_key, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        parent = FastMCP("parent")
        parent.mount(self._server_with_probe())

        async with Client(parent) as client:
            await client.list_tools()
            result = await client.call_tool("state_owner_probe", {}, raise_on_error=False)

        assert not result.is_error, result.data
        assert result.data == {"api": True, "doh": True, "downloads": True}

    async def test_a_standalone_server_still_resolves_all_three_owners(self, mock_api_key, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

        async with Client(self._server_with_probe()) as client:
            await client.list_tools()
            result = await client.call_tool("state_owner_probe", {}, raise_on_error=False)

        assert not result.is_error, result.data
        assert result.data == {"api": True, "doh": True, "downloads": True}


class TestNoProcessGlobalFallback:
    """Every accessor fails loudly when no owner is active."""

    def test_api_client_accessor(self):
        with pytest.raises(StateOwnershipError):
            client_module.get_api_client()

    def test_api_client_module_attribute(self):
        with pytest.raises(StateOwnershipError):
            _ = client_module.api_client

    def test_mcp_server_accessor(self):
        token = server_module.mcp_server_owner.set(None)
        try:
            with pytest.raises(StateOwnershipError):
                server_module.get_mcp_server()
        finally:
            server_module.mcp_server_owner.reset(token)

    def test_doh_client_accessor(self):
        token = doh_module.doh_client_owner.set(None)
        try:
            with pytest.raises(StateOwnershipError):
                doh_module.current_doh_client_owner()
        finally:
            doh_module.doh_client_owner.reset(token)

    def test_download_registry_accessor(self):
        token = logs_module._download_registry.set(None)
        try:
            with pytest.raises(logs_module.MissingDownloadRegistryError):
                logs_module.current_download_registry()
        finally:
            logs_module._download_registry.reset(token)

    def test_redirect_client_accessor(self):
        token = logs_module._redirect_client_owner.set(None)
        try:
            with pytest.raises(StateOwnershipError):
                logs_module.current_redirect_client_owner()
        finally:
            logs_module._redirect_client_owner.reset(token)


class TestDownloadRegistryOwnership:
    """Two registries in one process keep their own state."""

    def test_cleanup_reaches_only_the_owners_own_files(self, tmp_path):
        registry_a = logs_module.DownloadRegistry()
        registry_b = logs_module.DownloadRegistry()
        csv_a = str(tmp_path / "a.csv")
        csv_b = str(tmp_path / "b.csv")
        for path in (csv_a, csv_b):
            (tmp_path / os.path.basename(path)).write_text("csv")

        registry_a.note(csv_a, downloaded=True)
        registry_b.note(csv_b, downloaded=True)

        registry_a.cleanup()

        assert csv_a not in registry_a
        assert csv_b in registry_b, "one registry's cleanup claimed another registry's file"

    def test_note_skips_failures_and_bounds_retention(self, tmp_path, monkeypatch):
        registry = logs_module.DownloadRegistry()
        registry.note(str(tmp_path / "failed.csv"), downloaded=False)
        assert len(registry) == 0

        kept: list[str] = []
        for index in range(logs_module.MAX_RETAINED_DOWNLOADS + 2):
            path = str(tmp_path / f"{index}.csv")
            (tmp_path / f"{index}.csv").write_text("csv")
            registry.note(path, downloaded=True)
            kept.append(path)

        assert len(registry) == logs_module.MAX_RETAINED_DOWNLOADS
        assert kept[0] not in registry, "the oldest download should have been evicted"

    def test_cleanup_is_idempotent(self, tmp_path):
        registry = logs_module.DownloadRegistry()
        csv = str(tmp_path / "a.csv")
        (tmp_path / "a.csv").write_text("csv")
        registry.note(csv, downloaded=True)

        registry.cleanup()
        registry.cleanup()
        assert len(registry) == 0


class TestRedirectClientOwnership:
    """Two redirect owners in one process keep their own clients (issue #774)."""

    def test_a_published_owner_serves_the_current_context(self):
        owner = logs_module.RedirectClientOwner()
        with logs_module.redirect_client_owned(owner):
            assert logs_module.current_redirect_client_owner() is owner
        assert logs_module.current_redirect_client_owner() is not owner

    async def test_one_owners_shutdown_leaves_the_others_client_open(self):
        owners = [logs_module.RedirectClientOwner(), logs_module.RedirectClientOwner()]
        first, second = (owner.get() for owner in owners)
        assert first is not second

        await owners[0].aclose()

        assert first.is_closed
        assert not second.is_closed, "one owner's shutdown closed another owner's client"
        await owners[1].aclose()


class TestDohClientOwnership:
    """Two DoH owners in one process keep their own clients."""

    async def test_one_owners_shutdown_leaves_the_others_client_open(self, monkeypatch):
        owners = [doh_module.DohClientOwner(), doh_module.DohClientOwner()]
        first, second = (owner.get() for owner in owners)
        assert first is not second

        await owners[0].aclose()

        assert first.is_closed
        assert not second.is_closed, "one owner's shutdown closed another owner's client"
        await owners[1].aclose()


class TestServerResources:
    """The lifespan's resources owner closes only what it owns."""

    async def test_aclose_releases_every_owned_state(self, mock_api_key, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        resources = ServerResources()

        with (
            client_module.api_client_owned(resources.api_client),
            doh_module.doh_client_owned(resources.doh_client),
        ):
            api_client = resources.api_client.get()
            doh_client = resources.doh_client.get()
            assert not api_client.is_closed
            assert not doh_client.is_closed

        await resources.aclose()

        assert api_client.is_closed
        assert doh_client.is_closed

    async def test_two_lifespans_use_independent_state(self, mock_api_key, monkeypatch):
        """Two servers in one process: one's shutdown cannot touch the other's state."""
        monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
        resources_a = ServerResources()
        resources_b = ServerResources()

        with (
            client_module.api_client_owned(resources_a.api_client),
            doh_module.doh_client_owned(resources_a.doh_client),
        ):
            a_api_client = resources_a.api_client.get()
            # Server B starts and finishes WHILE server A is live:
            with (
                client_module.api_client_owned(resources_b.api_client),
                doh_module.doh_client_owned(resources_b.doh_client),
            ):
                b_api_client = resources_b.api_client.get()
                assert a_api_client is not b_api_client
            await resources_b.aclose()
            assert b_api_client.is_closed
            assert not a_api_client.is_closed, "a sibling server's shutdown closed this server's client"

        await resources_a.aclose()
        assert a_api_client.is_closed
