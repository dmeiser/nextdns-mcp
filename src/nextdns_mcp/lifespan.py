"""Server lifespan and the per-server state it owns.

``create_mcp_server`` (``nextdns_mcp.openapi``) attaches one
``ServerResources`` — the API client owner, probe transport owner, DoH
client owner, download registry, and redirect client owner — to each
``FastMCP`` instance and hands ``server_lifespan`` to FastMCP as the
server's lifespan. The lifespan publishes the resources two ways, both
explicit: into each module's owner context var for same-task callers, and
into the lifespan context FastMCP hands every request this server serves
(issue #642). Shutdown releases everything the server owns through
``ServerResources.aclose()``, which attempts every close even when one
fails (issue #774).

This module has no dependency on the optional ``generative`` module: the
lazily-started Pyodide/Deno sandbox subprocess (issue #408) is stopped by
``PrefabGenerativeUI.lifespan()``, which FastMCP unwinds before this
lifespan, and one server's shutdown no longer reaches into its siblings'
sandboxes (issue #542).

SPDX-License-Identifier: MIT
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .client import ApiClientOwner, ProbeClientOwner, api_client_owner
from .config import ServerConfig
from .state import RESOURCES_LIFESPAN_KEY, server_config
from .tools.doh import DohClientOwner, doh_client_owner
from .tools.logs import DownloadRegistry, RedirectClientOwner, _redirect_client_owner, download_registry


class ServerResources:
    """One server instance's explicitly owned state.

    Owns the immutable configuration snapshot, API client owner, probe
    transport owner, DoH client owner, download registry, and redirect client
    owner a server serves: the state that used to live in process-global
    module singletons any caller could reach. Every client and download
    consumer reads the snapshot this server was created with rather than the
    process environment. ``create_mcp_server()`` builds one per ``FastMCP``
    instance and attaches it to the instance, so two embedded servers never
    read or reset each other's state. :meth:`aclose` closes the clients and
    reclaims the downloads - one server's shutdown cannot reach into a
    sibling server's connections or tracked CSVs.
    """

    def __init__(self, config: ServerConfig | None = None) -> None:
        snapshot = config if config is not None else ServerConfig.from_env()
        self.config = snapshot
        self.api_client = ApiClientOwner(config=snapshot)
        self.probe_client = ProbeClientOwner()
        self.doh_client = DohClientOwner(config=snapshot)
        self.downloads = DownloadRegistry()
        self.redirect_client = RedirectClientOwner(config=snapshot)

    async def aclose(self) -> None:
        """Release everything this server owns, attempting every step.

        Matches the shutdown order the lifespan has always used - API client,
        probe transport, DoH client, downloads, redirect client - but every
        close is pushed onto an ``AsyncExitStack``, so a close that raises
        cannot skip the ones after it: the stack runs every callback and
        re-raises the failure only after the last one has run. Never leave
        this server's tracked CSVs behind because an unrelated client close
        failed (issue #774).
        """
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self.redirect_client.aclose)
            # Reclaiming tracked CSVs unlinks files that can be hundreds of
            # megabytes; shutdown pushes that work to a worker thread so the
            # event loop is not blocked by the final filesystem pass.
            stack.push_async_callback(asyncio.to_thread, self.downloads.cleanup)
            stack.push_async_callback(self.doh_client.aclose)
            stack.push_async_callback(self.probe_client.aclose)
            stack.push_async_callback(self.api_client.aclose)


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

    Own the state THIS server hands out: the API client, probe transport, DoH
    client, download registry, and redirect client attached to the instance
    by ``create_mcp_server``. The
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
    redirect_token = _redirect_client_owner.set(resources.redirect_client)
    config_token = server_config.set(resources.config)
    try:
        yield {RESOURCES_LIFESPAN_KEY: resources}
    finally:
        try:
            await resources.aclose()
        finally:
            _redirect_client_owner.reset(redirect_token)
            download_registry.reset(registry_token)
            doh_client_owner.reset(doh_token)
            api_client_owner.reset(api_token)
            server_config.reset(config_token)
