"""The unauthenticated redirect-chain fetch for log downloads.

The download endpoint answers with a redirect to a signed URL, and the
follow-up fetch must not carry the ``X-Api-Key`` header to a host the API
does not control: the chain is fetched with one persistent, bounded,
unauthenticated client instead of a fresh client per download, so repeated
downloads can reuse eligible keep-alive connections (issue #656). Every hop
is vetted by :mod:`.guard` before any request is made to it, and the whole
chain runs under ``guard._PIN_FETCH_LOCK`` so vetting and fetching stay one
indivisible section process-wide (see the guard module's note on the pinned
resolver).

The persistent client carries no credentials and no default headers, so the
API key still never crosses to a redirect target. Its shared cookie jar is
cleared on entry to and exit from every chain, under the lock, which is what
keeps cookies from leaking between downloads the way a fresh client used to
guarantee; the per-request timeout comes from the serving server's creation-time
configuration on every chain.

The client is owned per server, not per process: a ``RedirectClientOwner``
lives on the server's ``ServerResources`` (see ``nextdns_mcp.lifespan``), so
one embedded server's shutdown closes only its own client and cannot pull
keep-alive connections out from under a live sibling's downloads. The
owner's close runs under ``guard._PIN_FETCH_LOCK`` - the same lock every
chain holds for its whole vet-and-fetch section - so a shutdown close waits
for an active fetch instead of closing the client out from under it
(issue #774).

SPDX-License-Identifier: MIT
"""

import asyncio
import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from ... import client
from ...config import ServerConfig
from ...errors import StateOwnershipError
from ...state import current_server_config, request_resources
from . import guard, stream

# Maximum number of redirects followed when downloading logs.
_MAX_DOWNLOAD_REDIRECTS = 5


def _redirect_timeout(config: ServerConfig | None) -> float:
    """Use the server snapshot timeout when one exists, otherwise the process environment."""
    return config.http_timeout if config is not None else client.get_http_timeout()


def _current_redirect_timeout() -> float:
    """The timeout for this call: the serving server's snapshot if one exists."""
    config = current_server_config()
    return client.get_http_timeout() if config is None else config.http_timeout


class RedirectClientOwner:
    """Owns one persistent redirect-chain client for one explicit lifetime.

    Explicit owner for the client state that used to live in the module-global
    ``_redirect_client`` singleton (mirrors ``client.ApiClientOwner`` and
    ``tools.doh.DohClientOwner``): a server lifespan owns one owner per server
    on its ``ServerResources``, and standalone callers build their own. The
    client is created lazily on first use and reused until :meth:`aclose`, so
    repeated downloads keep their keep-alive connection reuse per owner - and
    two owners in one process never share a client, so one owner's shutdown
    cannot close another owner's connections.

    The client carries no credentials and no default headers, so the API key
    can never cross to a redirect target. Its timeout is taken from the
    serving server's creation-time config snapshot when one exists, falling
    back to the process environment for standalone owners. The cookie jar IS
    shared, so ``_follow_redirects_to_tempfile`` clears it on entry to and
    exit from every chain - under ``guard._PIN_FETCH_LOCK``, so the clears are
    indivisible from the chain between them and cookies still cannot leak
    from one download into the next.
    """

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        """Return this owner's redirect-chain client, creating it on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=_redirect_timeout(self.config),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        return self._client

    async def aclose(self) -> None:
        """Close this owner's client, waiting for any active fetch to finish.

        The close runs under ``guard._PIN_FETCH_LOCK``: every chain holds that
        lock for its whole vet-and-fetch section, so a shutdown close can
        never land mid-chain - it waits for the active fetch to release the
        lock, and a chain that starts after the close builds a fresh client
        instead of reusing a closed one. The next :meth:`get` constructs a
        fresh client, picking up any updated configuration (such as
        ``NEXTDNS_HTTP_TIMEOUT``).
        """
        async with guard._PIN_FETCH_LOCK:
            if self._client is not None:
                owned = self._client
                self._client = None
                await owned.aclose()


# The owner whose redirect client the current context uses; published by a
# server lifespan or by standalone callers for same-task work, and resolved
# from the FastMCP request's lifespan context for served requests (see
# nextdns_mcp.state). No module-level default: an absent owner fails loudly
# instead of silently sharing one process-global client between independent
# servers.
_redirect_client_owner: contextvars.ContextVar[RedirectClientOwner | None] = contextvars.ContextVar(
    "nextdns_redirect_client_owner", default=None
)


def current_redirect_client_owner() -> RedirectClientOwner:
    """Return the owner of the redirect-chain client state for the current context.

    Resolution order: an owner published with :func:`redirect_client_owned`,
    then the resources of the FastMCP server serving this request.

    Raises:
        StateOwnershipError: When no owner is active. Code reached outside a
            server lifespan must publish one with :func:`redirect_client_owned`.
    """
    owner = _redirect_client_owner.get()
    if owner is None:
        resources = request_resources()
        if resources is not None:
            owner = resources.redirect_client
    if owner is None:
        raise StateOwnershipError(
            "No redirect client owner is active in this context. Server code runs inside a server "
            "lifespan, which publishes one; standalone callers must wrap the work in "
            "`with redirects.redirect_client_owned(redirects.RedirectClientOwner()):`."
        )
    return owner


@contextmanager
def redirect_client_owned(owner: RedirectClientOwner) -> Iterator[RedirectClientOwner]:
    """Publish ``owner`` as the current context's redirect client owner for the block."""
    token = _redirect_client_owner.set(owner)
    try:
        yield owner
    finally:
        _redirect_client_owner.reset(token)


def _get_redirect_client() -> httpx.AsyncClient:
    """Return the current owner's persistent redirect-chain client, creating it on first use."""
    return current_redirect_client_owner().get()



async def close_redirect_client() -> None:
    """Close the current owner's redirect-chain client if one exists and reset it.

    Called by the server lifespan at shutdown, under the fetch lock (see
    :meth:`RedirectClientOwner.aclose`). The next download after a close
    constructs a fresh client from the serving server's snapshot (or from the
    process environment for a standalone caller).
    """
    await current_redirect_client_owner().aclose()


async def _follow_redirects_to_tempfile(next_url: httpx.URL, path: str) -> dict[str, Any]:
    """Follow a redirect chain to the final CSV, streaming it to ``path``.

    The ``Location`` chain is fetched with the persistent unauthenticated
    client so the ``X-Api-Key`` header never crosses to a redirect target.
    Relative ``Location`` headers are resolved against the origin of the
    current URL. Redirects are bounded so a loop cannot hang the call, and
    every hop (the first included) is checked to be https on a globally
    routable host, so a redirect cannot aim the fetch at an internal or
    metadata service.

    The whole chain runs under ``guard._PIN_FETCH_LOCK``: vetting and
    fetching must be one indivisible section process-wide, or a concurrent
    download's resolver swap interleaves with this one (see the guard module
    note). The lock also serialises chains, which is what makes the shared
    client's cookie jar safe to reuse: it is cleared on entry and on exit, so
    each chain starts from an empty jar and leaves one, exactly as a fresh
    per-download client used to guarantee. The clears bracket the chain, not
    its hops: a cookie one hop sets still reaches the next hop of the same
    download. The client is fetched inside the lock for the same reason - a
    chain that queued behind another never holds a client a concurrent
    shutdown already closed.
    """
    redirects = 0
    timeout = _current_redirect_timeout()
    async with asyncio.timeout(timeout * (_MAX_DOWNLOAD_REDIRECTS + 1)):
        async with guard._PIN_FETCH_LOCK:
            unauthenticated = _get_redirect_client()
            # A fresh client per download used to give every chain its own
            # empty cookie jar; the shared jar is cleared on entry and on exit
            # so a cookie a redirect target set cannot reach the next download
            # or linger in process memory between downloads.
            unauthenticated.cookies.clear()
            try:
                while True:
                    # Checked before every fetch, so the first ``Location`` is covered
                    # by the same rule as the rest of the chain; the fetch then dials
                    # the vetted addresses rather than re-resolving the name.
                    infos = await guard._check_download_redirect_target(next_url)
                    with guard._pinned_dns(next_url.host, infos):
                        async with unauthenticated.stream(
                            "GET",
                            next_url,
                            headers={"Accept-Encoding": "identity"},
                            # The serving server's creation-time timeout: the
                            # shared client's constructor value is only a fallback,
                            # so this is the value each request actually uses.
                            timeout=timeout,
                        ) as response:
                            if response.has_redirect_location:
                                redirects += 1
                                if redirects > _MAX_DOWNLOAD_REDIRECTS:
                                    raise httpx.TooManyRedirects(
                                        f"Exceeded {_MAX_DOWNLOAD_REDIRECTS} redirects", request=response.request
                                    )
                                next_url = response.request.url.join(response.headers["location"])
                                continue
                            return await stream._write_stream_to_tempfile(response, path)
            finally:
                unauthenticated.cookies.clear()


async def _stream_download_to_path(profile_id: str, path: str) -> dict[str, Any]:
    """Stream the log download for ``profile_id`` into ``path`` and return the payload."""
    async with client.api_client.stream(
        "GET", f"/profiles/{profile_id}/logs/download", headers={"Accept-Encoding": "identity"}
    ) as initial:
        # A non-redirect response is written straight to disk;
        # _write_stream_to_tempfile surfaces any HTTP error via
        # raise_for_status.
        if not initial.has_redirect_location or initial.is_error:
            return await stream._write_stream_to_tempfile(initial, path)
        redirect_url = initial.request.url.join(initial.headers["location"])
    return await _follow_redirects_to_tempfile(redirect_url, path)
