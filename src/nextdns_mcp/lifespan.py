"""Server lifespan and the per-server state it owns.

``create_mcp_server`` (``nextdns_mcp.openapi``) attaches one
``ServerResources`` — the API client owner, DoH client owner, and download
registry — to each ``FastMCP`` instance and hands ``server_lifespan`` to
FastMCP as the server's lifespan. The lifespan publishes the resources two
ways, both explicit: into each module's owner context var for same-task
callers, and into the lifespan context FastMCP hands every request this
server serves (issue #642). Shutdown releases everything the server owns.

This module has no dependency on the optional ``generative`` module: the
lazily-started Pyodide/Deno sandbox subprocess (issue #408) is stopped by
``PrefabGenerativeUI.lifespan()``, which FastMCP unwinds before this
lifespan, and one server's shutdown no longer reaches into its siblings'
sandboxes (issue #542).

SPDX-License-Identifier: MIT
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .client import ApiClientOwner, api_client_owner, close_raw_probe_client
from .config import ServerConfig
from .state import RESOURCES_LIFESPAN_KEY, server_config
from .tools.doh import DohClientOwner, doh_client_owner
from .tools.logs import DownloadRegistry, close_redirect_client, download_registry


class ServerResources:
    """One server instance's explicitly owned state.

    Owns the immutable configuration snapshot, API client owner, DoH client
    owner, and download registry a server serves: the state that used to live
    in process-global module singletons any caller could reach. Every client
    and download consumer reads the snapshot this server was created with rather
    than the process environment. ``create_mcp_server()`` builds one per
    ``FastMCP`` instance and attaches it to the instance, so two embedded
    servers never read or reset each other's state. :meth:`aclose` closes the
    clients and reclaims the downloads - one server's shutdown cannot reach
    into a sibling server's connections or tracked CSVs.
    """

    def __init__(self, config: ServerConfig | None = None) -> None:
        snapshot = config if config is not None else ServerConfig.from_env()
        self.config = snapshot
        self.api_client = ApiClientOwner(config=snapshot)
        self.doh_client = DohClientOwner(config=snapshot)
        self.downloads = DownloadRegistry()

    async def aclose(self) -> None:
        """Release everything this server owns: clients first, then the downloads.

        Matches the shutdown order the lifespan has always used: close the
        API client even when the DoH close or the download cleanup fails,
        and never leave this server's tracked CSVs behind.
        """
        try:
            await self.api_client.aclose()
        finally:
            await self.doh_client.aclose()
            self.downloads.cleanup()


def server_resources(mcp: FastMCP) -> ServerResources:
    """The resources attached to this FastMCP instance at creation.

    Raises:
        RuntimeError: When the instance carries no resources - every server
            this package creates attaches them in ``create_mcp_server``.
    """
    resources = getattr(mcp, "_nextdns_resources", None)
    if resources is None:
        raise RuntimeError("FastMCP instance carries no nextdns_mcp ServerResources")
    return resources


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage server lifespan: own the server's state explicitly, release it on shutdown.

    Own the state THIS server hands out: the API client, DoH client, and
    download registry attached to the instance by ``create_mcp_server``. The
    lifespan publishes them two ways, both explicit: into each module's owner
    context var for same-task callers, and into the lifespan context FastMCP
    hands every request this server serves (issue #642: the lifespan task
    does not propagate context variables into request tasks, so the lifespan
    context is the only channel served traffic can resolve owners through).
    A second embedded server owns its own resources, so one server's shutdown
    closes only its own clients and reclaims only its own downloads
    (issues #277, #642). The state used to be process-global singletons that
    every embedded server's lifespan emptied or closed, killing a live
    sibling's connections and deleting its tracked CSVs mid-read.
    """
    resources = server_resources(server)
    api_token = api_client_owner.set(resources.api_client)
    doh_token = doh_client_owner.set(resources.doh_client)
    registry_token = download_registry.set(resources.downloads)
    config_token = server_config.set(resources.config)
    try:
        yield {RESOURCES_LIFESPAN_KEY: resources}
    finally:
        try:
            await resources.api_client.aclose()
        finally:
            await close_raw_probe_client()
            await resources.doh_client.aclose()
            resources.downloads.cleanup()
            # The downloadLogs redirect chain reuses one persistent unauthenticated
            # client for keep-alive reuse (issue #656); like the transport above
            # it is closed at shutdown.
            await close_redirect_client()
            # The lazily-started Pyodide/Deno sandbox subprocess (issue #408) is
            # stopped by PrefabGenerativeUI.lifespan(), which FastMCP unwinds
            # before this lifespan: this module has no dependency on the optional
            # generative module, and one server's shutdown no longer reaches into
            # its siblings' sandboxes (issue #542).
            download_registry.reset(registry_token)
            doh_client_owner.reset(doh_token)
            api_client_owner.reset(api_token)
            server_config.reset(config_token)
