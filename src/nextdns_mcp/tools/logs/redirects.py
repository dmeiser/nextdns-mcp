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

SPDX-License-Identifier: MIT
"""

import asyncio
from typing import Any

import httpx

from ... import client
from ...state import current_server_config
from . import guard, stream

# Maximum number of redirects followed when downloading logs.
_MAX_DOWNLOAD_REDIRECTS = 5

# The persistent unauthenticated client, module-level for keep-alive
# connection reuse across downloads (mirrors tools.doh._doh_client; issue
# #656): constructing and closing a fresh client per download churned
# connections on a user-initiated but potentially repeated action. The
# explicit pool bounds the client, and it carries no credentials and no
# default headers, so the API key can never cross to a redirect target. The
# timeout baked at construction is only the fallback: each request passes the
# serving server's snapshot timeout. The cookie jar IS shared, so
# _follow_redirects_to_tempfile clears it on entry to and exit from every
# chain - under guard._PIN_FETCH_LOCK, so the clears are indivisible from
# the chain between them and cookies still cannot leak from one download
# into the next.
_redirect_client: httpx.AsyncClient | None = None


def _current_redirect_timeout() -> float:
    """The timeout for this call: the serving server's snapshot if one exists."""
    config = current_server_config()
    return client.get_http_timeout() if config is None else config.http_timeout


def _get_redirect_client(timeout: float | None = None) -> httpx.AsyncClient:
    """Return the persistent redirect-chain client, creating it on first use.

    ``timeout`` is the creating server's snapshot value when the chain is
    served by a server; a standalone caller leaves it unset and the process
    environment supplies it as before.
    """
    global _redirect_client
    if _redirect_client is None:
        _redirect_client = httpx.AsyncClient(
            timeout=client.get_http_timeout() if timeout is None else timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _redirect_client


async def close_redirect_client() -> None:
    """Close the persistent redirect-chain client if one exists and reset the singleton.

    Called by the server lifespan at shutdown. The next download after a close
    constructs a fresh client from the serving server's snapshot (or from the
    process environment for a standalone caller).
    """
    global _redirect_client
    if _redirect_client is not None:
        owned = _redirect_client
        _redirect_client = None
        await owned.aclose()


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
            unauthenticated = _get_redirect_client(timeout)
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
    async with client.get_api_client().stream(
        "GET", f"/profiles/{profile_id}/logs/download", headers={"Accept-Encoding": "identity"}
    ) as initial:
        # A non-redirect response is written straight to disk;
        # _write_stream_to_tempfile surfaces any HTTP error via
        # raise_for_status.
        if not initial.has_redirect_location or initial.is_error:
            return await stream._write_stream_to_tempfile(initial, path)
        redirect_url = initial.request.url.join(initial.headers["location"])
    return await _follow_redirects_to_tempfile(redirect_url, path)
