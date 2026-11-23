"""The unauthenticated redirect-chain fetch for log downloads.

The download endpoint answers with a redirect to a signed URL, and the
follow-up fetch must not carry the ``X-Api-Key`` header to a host the API
does not control: the chain is fetched with a separate unauthenticated
client. Every hop is vetted by :mod:`.guard` before any request is made to
it, and the whole chain runs under ``guard._PIN_FETCH_LOCK`` so vetting and
fetching stay one indivisible section process-wide (see the guard module's
note on the pinned resolver).

SPDX-License-Identifier: MIT
"""

import asyncio
from typing import Any

import httpx

from ... import client
from . import guard, stream

# Maximum number of redirects followed when downloading logs.
_MAX_DOWNLOAD_REDIRECTS = 5


async def _follow_redirects_to_tempfile(next_url: httpx.URL, path: str) -> dict[str, Any]:
    """Follow a redirect chain to the final CSV, streaming it to ``path``.

    The ``Location`` chain is fetched with an unauthenticated client so the
    ``X-Api-Key`` header never crosses to a redirect target. Relative
    ``Location`` headers are resolved against the origin of the current URL.
    Redirects are bounded so a loop cannot hang the call, and every hop (the
    first included) is checked to be https on a globally routable host, so a
    redirect cannot aim the fetch at an internal or metadata service.

    The whole chain runs under ``_PIN_FETCH_LOCK``: vetting and fetching must
    be one indivisible section process-wide, or a concurrent download's
    resolver swap interleaves with this one (see the module note).
    """
    redirects = 0
    unauthenticated = httpx.AsyncClient(timeout=client.get_http_timeout(), follow_redirects=False)
    try:
        async with asyncio.timeout(client.get_http_timeout() * (_MAX_DOWNLOAD_REDIRECTS + 1)):
            async with guard._PIN_FETCH_LOCK:
                while True:
                    # Checked before every fetch, so the first ``Location`` is covered
                    # by the same rule as the rest of the chain; the fetch then dials
                    # the vetted addresses rather than re-resolving the name.
                    infos = await guard._check_download_redirect_target(next_url)
                    with guard._pinned_dns(next_url.host, infos):
                        async with unauthenticated.stream(
                            "GET", next_url, headers={"Accept-Encoding": "identity"}
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
        await unauthenticated.aclose()


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
