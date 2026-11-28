"""The log-download path keeps the API key in and the network out of the wrong places.

``downloadLogs`` is the one tool the spec cannot express as a plain call: the endpoint
answers with a redirect to a signed URL, so the server has to decide what to do with a
``Location`` header it does not control. This part pins the request the server makes
next: the API key never crosses to the redirect target (issue #266), and a hop is
refused unless it is https on a globally routable host. The streaming half lives in
``test_log_download_guard_streaming.py`` and the tool lifecycle in
``test_log_download_guard_download.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import ipaddress
import os
import socket
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp.tools import logs as logs_module
from nextdns_mcp.tools.logs import guard, redirects

# example.com: an address Python's ipaddress module reports as globally routable.
PUBLIC_IP = "93.184.216.34"
PRIVATE_IPS = ("127.0.0.1", "10.0.0.5", "169.254.169.254", "172.16.0.1", "fc00::1", "::1")


def _is_ip_literal(host: str) -> bool:
    """Whether a URL's host is an address rather than a name to resolve."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class _FakeResponse:
    """A streaming response shaped the way ``_write_stream_to_tempfile`` reads one."""

    def __init__(
        self,
        url: str,
        *,
        chunks: list[str] | None = None,
        location: str | None = None,
        status_code: int = 200,
        content_type: str = "text/csv",
        set_cookie: str | None = None,
    ) -> None:
        self.request = httpx.Request("GET", url)
        self.status_code = status_code
        headers = {"location": location} if location else {} | ({"content-type": content_type} if content_type else {})
        if set_cookie:
            # A target's ``Set-Cookie`` is what the shared cookie jar has to
            # survive: see the isolation test on the persistent redirect client.
            headers["set-cookie"] = set_cookie
        self.headers = httpx.Headers(headers)
        self._chunks = chunks or []
        # Tracks the fake's own consumption so is_stream_consumed matches a
        # real httpx stream: unconsumed until every chunk has been iterated.
        self._consumed = False

    @property
    def has_redirect_location(self) -> bool:
        return "location" in self.headers

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def is_stream_consumed(self) -> bool:
        return self._consumed

    def raise_for_status(self) -> None:
        if self.is_error:
            # Faithful to streaming httpx: the error carries the *streaming*
            # response (body still unread), which is the shape that used to
            # crash the payload builder with ResponseNotRead.
            raise httpx.HTTPStatusError(f"upstream said {self.status_code}", request=self.request, response=self)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        self._consumed = True

    async def aiter_text(self) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk
        self._consumed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeClient:
    """Records the requests made through it, so the absence of a key is checkable."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": str(url), "kwargs": kwargs})
        if not self.responses:
            raise AssertionError(f"no scripted response left for {method} {url}")
        return self.responses.pop(0)


class _FakeUnauthenticatedClient:
    """Stands in for the client that follows the redirect chain."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        # The shared client's cookie jar is cleared around every chain; a plain
        # dict answers .clear() the way the real jar does.
        self.cookies: dict[str, str] = {}
        # What the jar held at each request, so a leak between downloads is visible.
        self.jars: list[dict[str, str]] = []
        # The timeout each request was sent with, so a config change mid-process
        # either reaches the next download or visibly does not.
        self.timeouts: list[Any] = []
        self.constructions = 0

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.jars.append(dict(self.cookies))
        self.timeouts.append(kwargs.get("timeout"))
        self.calls.append((method, str(url)))
        if not self.responses:
            raise AssertionError(f"no scripted redirect response left for {url}")
        response = self.responses.pop(0)
        # httpx merges a response's Set-Cookie into the client's own jar, so the
        # fake has to, or a test cannot see whether the chain's clear runs.
        for item in response.headers.get_list("set-cookie"):
            name, _, value = item.partition("=")
            self.cookies[name.strip()] = value.strip()
        return response

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def unauthenticated(monkeypatch):
    """Route the redirect-following client through a fake, and hand it the responses."""

    def _install(responses: list[_FakeResponse]) -> _FakeUnauthenticatedClient:
        client = _FakeUnauthenticatedClient(responses)

        def factory(*args: Any, **kwargs: Any) -> _FakeUnauthenticatedClient:
            # The follow-up fetch must be built with redirect handling off: httpx's
            # own follow would skip the per-hop vetting entirely. And it must be
            # built with no headers, because the API key belongs to the first
            # request only. The pool must stay bounded so the persistent client
            # cannot open unbounded connections (issue #656).
            assert kwargs.get("follow_redirects") is False
            assert not kwargs.get("headers")
            assert isinstance(kwargs.get("limits"), httpx.Limits)
            client.constructions += 1
            return client

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return client

    return _install


@pytest.fixture
def owned_api_client(monkeypatch):
    """Build a context manager publishing an API-client owner around a fake.

    The download path resolves the client through the current owner (issue
    #642), so tests install the fake as the owner's lazy build instead of
    patching the old module-level ``api_client`` attribute. Enter the context
    around the download call: the owner is scoped to the calling context.
    """

    def _owner_publishing(fake):
        monkeypatch.setattr(client_module, "create_nextdns_client", lambda: fake)
        return client_module.api_client_owned(client_module.ApiClientOwner())

    return _owner_publishing


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Resolve every name to a public address unless a test says otherwise.

    A literal IP resolves to itself: the vetting has to see the address the URL
    actually names, or a test about a metadata address would pass by accident of
    the fake resolver rather than of the guard.
    """

    def fake_getaddrinfo(host: str, port: Any, **kwargs: Any) -> list[Any]:
        address = host if _is_ip_literal(host) else PUBLIC_IP
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class TestAddressVetting:
    """Only a globally routable unicast address is a permitted download target."""

    @pytest.mark.parametrize("address", PRIVATE_IPS)
    def test_a_non_public_address_is_not_public(self, address):
        assert logs_module._is_public_address(address) is False

    @pytest.mark.parametrize("address", [PUBLIC_IP, "8.8.4.4", "2606:4700:4700::1111"])
    def test_a_globally_routable_address_is_public(self, address):
        assert logs_module._is_public_address(address) is True

    async def test_a_host_with_one_private_address_is_refused(self, monkeypatch):
        """A name that resolves to both a public and an internal address is refused.

        Checking every answer, not just the first, is what keeps a DNS record that
        mixes the two from selecting the internal one at connect time.
        """

        def fake_getaddrinfo(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        assert await guard._resolve_public_addresses("mixed.example") is None

    async def test_an_unresolvable_host_is_refused(self, monkeypatch):
        """Name-resolution failure fails the vetting closed (issue #266 review).

        The old fail-open reading argued an unresolvable host cannot be dialed
        anyway - but that holds only while the guard's resolver and httpx's
        agree, so the guard must not switch itself off when DNS misbehaves. A
        host whose addresses cannot be enumerated is not proven public, and an
        unproven hop is refused.
        """

        def fail(host: str, port: Any, **kwargs: Any) -> list[Any]:
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "getaddrinfo", fail)
        assert await guard._resolve_public_addresses("gone.example") is None

    async def test_an_empty_resolution_is_refused(self, monkeypatch):
        """An empty address list proves nothing either - same closed default."""

        def none(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return []

        monkeypatch.setattr(socket, "getaddrinfo", none)
        assert await guard._resolve_public_addresses("ghost.example") is None


class TestRedirectRefusal:
    """The vetting runs before the request, on every hop including the first."""

    async def test_a_plaintext_target_is_refused(self):
        with pytest.raises(logs_module.DownloadRedirectRefusedError, match="scheme not allowed"):
            await logs_module._check_download_redirect_target(httpx.URL("http://downloads.example/file.csv"))

    async def test_a_non_http_target_is_refused(self):
        with pytest.raises(logs_module.DownloadRedirectRefusedError):
            await logs_module._check_download_redirect_target(httpx.URL("file:///etc/passwd"))

    async def test_a_metadata_address_is_refused(self):
        with pytest.raises(logs_module.DownloadRedirectRefusedError, match="not publicly routable"):
            await logs_module._check_download_redirect_target(httpx.URL("https://169.254.169.254/latest/meta-data"))

    async def test_the_refusal_names_no_signed_url(self, monkeypatch):
        """The signed part of a redirect URL is a secret, and the message is logged."""

        def fake_getaddrinfo(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(logs_module.DownloadRedirectRefusedError) as raised:
            await logs_module._check_download_redirect_target(
                httpx.URL("https://internal.example/file.csv?X-Amz-Signature=deadbeef")
            )
        assert "deadbeef" not in str(raised.value)
        assert "X-Amz-Signature" not in str(raised.value)

    async def test_a_pinned_host_skips_resolution(self, monkeypatch):
        """A host the operator pins is trusted without a DNS round trip.

        The set ships empty — nothing is trusted by name — so the branch is
        exercised with a pin installed, which is how an operator would use it.
        """

        def fail(host: str, port: Any, **kwargs: Any) -> list[Any]:
            raise AssertionError("a pinned host must not be resolved")

        monkeypatch.setattr(socket, "getaddrinfo", fail)
        monkeypatch.setattr(guard, "ALLOWED_DOWNLOAD_HOSTS", frozenset({"cdn.nextdns.io"}))
        await logs_module._check_download_redirect_target(httpx.URL("https://cdn.nextdns.io/file.csv"))

    async def test_a_public_https_target_is_accepted(self):
        await logs_module._check_download_redirect_target(httpx.URL("https://storage.example/file.csv"))


class TestUnauthenticatedFollowUp:
    """The API key stays on the NextDNS request."""

    async def test_the_redirect_chain_is_fetched_without_the_key(self, tmp_path, unauthenticated, monkeypatch):
        """The signed hop is fetched by a second client that has no credentials.

        The authenticated client makes exactly one request, to the NextDNS API;
        every hop after that belongs to the unauthenticated client, which is
        constructed with no headers at all (asserted in the ``unauthenticated``
        fixture). So a redirect to another host cannot carry the API key.
        """
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        signed = "https://storage.example/file.csv?X-Amz-Signature=deadbeef"
        api = _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        follower = unauthenticated(
            [
                _FakeResponse(signed, location="https://storage.example/second.csv"),
                _FakeResponse("https://storage.example/second.csv", chunks=["host,reason\na.com,blocked\n"]),
            ]
        )

        payload = await logs_module._follow_redirects_to_tempfile(httpx.URL(signed), str(tmp_path / "download.csv"))

        assert payload["row_count"] == 2
        assert [url for _, url in follower.calls] == [signed, "https://storage.example/second.csv"]
        assert api.calls == []

    async def test_a_refused_hop_is_never_requested(self, tmp_path, unauthenticated, monkeypatch):
        """The check runs before the fetch, so nothing is ever sent to the target."""

        def fake_getaddrinfo(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        client = unauthenticated([_FakeResponse("https://internal.example/file.csv", chunks=["pwned"])])
        with pytest.raises(logs_module.DownloadRedirectRefusedError):
            await logs_module._follow_redirects_to_tempfile(
                httpx.URL("https://internal.example/file.csv"), str(tmp_path / "d.csv")
            )
        assert client.calls == []

    async def test_a_redirect_loop_stops(self, tmp_path, unauthenticated):
        """A cycle cannot hang the call: the hop count is bounded."""
        hops = [
            _FakeResponse(f"https://storage.example/{n}", location=f"https://storage.example/{n + 1}")
            for n in range(logs_module._MAX_DOWNLOAD_REDIRECTS + 3)
        ]
        client = unauthenticated(hops)
        with pytest.raises(httpx.TooManyRedirects):
            await logs_module._follow_redirects_to_tempfile(
                httpx.URL("https://storage.example/0"), str(tmp_path / "d.csv")
            )
        assert len(client.calls) == logs_module._MAX_DOWNLOAD_REDIRECTS + 1

    async def test_the_redirect_chain_reuses_one_client_closed_at_shutdown(self, tmp_path, unauthenticated):
        """Two downloads share the persistent client instead of constructing and
        closing a fresh one per chain (issue #656); the lifespan closes it at shutdown."""
        client = unauthenticated(
            [
                _FakeResponse("https://storage.example/one.csv", chunks=["a\n"]),
                _FakeResponse("https://storage.example/two.csv", chunks=["b\n"]),
            ]
        )
        for name in ("one.csv", "two.csv"):
            await logs_module._follow_redirects_to_tempfile(
                httpx.URL(f"https://storage.example/{name}"), str(tmp_path / name)
            )
        assert len(client.calls) == 2
        assert client.constructions == 1, "each download constructed its own client"
        assert client.closed is False, "a completed download must not close the shared client"
        await logs_module.close_redirect_client()
        assert client.closed is True
        owner = redirects.current_redirect_client_owner()
        assert owner._client is None, "closing must reset the owner's client"

    async def test_close_before_any_download_is_a_noop(self):
        """The shutdown close is safe when no download ever created the client."""
        await logs_module.close_redirect_client()
        assert redirects.current_redirect_client_owner()._client is None

    async def test_close_waits_for_an_active_fetch(self):
        """The shutdown close runs under the fetch lock (issue #774): it must wait
        for a chain holding the lock rather than close the client mid-fetch."""
        async with logs_module._PIN_FETCH_LOCK:  # stands in for an in-flight chain
            close_task = asyncio.create_task(logs_module.close_redirect_client())
            await asyncio.sleep(0.05)
            assert not close_task.done(), "close completed while a fetch held the lock"
        await asyncio.wait_for(close_task, timeout=5)

    async def test_two_owners_never_share_a_redirect_client(self):
        """Per-server ownership: one server's redirect client is not its sibling's."""
        first = redirects.RedirectClientOwner()
        second = redirects.RedirectClientOwner()
        try:
            assert first.get() is not second.get()
        finally:
            await first.aclose()
            await second.aclose()

    def test_the_fetch_lock_contends_in_two_event_loops(self):
        """The pinned-resolver lock is process-global, so it must contend cleanly
        from more than one event loop (issue #774): an import-time asyncio.Lock
        binds to the first loop and raises ``bound to a different event loop``
        the moment a second loop contends it."""

        async def contend() -> None:
            async def hold() -> None:
                async with logs_module._PIN_FETCH_LOCK:
                    await asyncio.sleep(0.02)

            await asyncio.gather(hold(), hold())

        asyncio.run(contend())
        asyncio.run(contend())  # a loop-bound lock raises RuntimeError here

    async def test_a_cancelled_waiter_never_leaks_the_lock(self):
        """A waiter cancelled while queued (the download's overall timeout can
        fire while a chain waits behind another) must not leave the
        process-global lock held forever: the orphaned acquire's release is
        tied to its completion."""
        async with logs_module._PIN_FETCH_LOCK:
            waiter = asyncio.create_task(logs_module._PIN_FETCH_LOCK.__aenter__())
            await asyncio.sleep(0.05)  # the waiter is blocked in its worker thread
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        # The orphaned acquire completes once the holder releases, and the
        # handoff gives the lock straight back: a fresh acquire must not hang.
        async with asyncio.timeout(5):
            async with logs_module._PIN_FETCH_LOCK:
                pass

    def test_a_teardown_cancelled_waiter_never_leaks_the_lock(self):
        """Loop teardown cancels the shielded task itself - which the shield
        cannot protect against - while its worker is blocked in ``acquire``.
        The orphaned acquire still completes, and must hand the process-global
        lock back instead of stranding every later fetch and close."""
        lock = guard._CrossLoopLock()

        def hold_section() -> None:
            lock._lock.acquire()  # another server's download chain
            time.sleep(0.5)
            lock._lock.release()

        holder = threading.Thread(target=hold_section, daemon=True)
        holder.start()
        time.sleep(0.05)

        async def torn_down_loop() -> None:
            asyncio.ensure_future(lock.__aenter__())
            await asyncio.sleep(0.15)  # the waiter is queued in its worker thread
            # returning makes asyncio.run cancel every pending task, the
            # shielded acquire included, and then join the worker

        asyncio.run(torn_down_loop())
        holder.join()
        time.sleep(0.1)  # let the orphaned worker finish its acquire

        assert not lock._lock.locked()
        assert lock._lock.acquire(blocking=False) is True
        lock._lock.release()

    async def test_an_abandoned_waiter_releases_what_the_worker_took(self):
        """Race: the worker finishes the acquire just before the waiter is
        cancelled, so it never sees the abandonment. The waiter must release
        what the worker took - once, and not again when the task completes."""
        lock = guard._CrossLoopLock()
        release_holder = threading.Event()

        def hold_section() -> None:
            lock._lock.acquire()
            release_holder.wait(5)
            lock._lock.release()

        holder = threading.Thread(target=hold_section, daemon=True)
        holder.start()
        time.sleep(0.05)  # noqa: ASYNC251 - a thread holds the section, not this loop

        waiter = asyncio.ensure_future(lock.__aenter__())
        await asyncio.sleep(0.1)  # the waiter's worker is blocked in acquire
        release_holder.set()
        # Hold the loop: the worker takes the freed section and its result
        # queues up undelivered, so the waiter is cancelled with the lock
        # already in the worker's hands and the handoff still unrecorded here.
        time.sleep(0.2)  # noqa: ASYNC251 - blocking the loop IS the setup
        holder.join()
        assert lock._lock.locked(), "the worker never took the freed section: nothing to hand back"
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0)  # reap the completed acquire task
        assert not lock._lock.locked()

    async def test_an_acquire_that_failed_releases_nothing(self, monkeypatch):
        """An executor refusal during teardown ends the acquire with an
        exception rather than cancellation: nothing was acquired, so another
        server's hold on the section must survive untouched."""
        lock = guard._CrossLoopLock()

        def refuse(*args: Any, **kwargs: Any) -> Any:
            async def fail() -> None:
                raise RuntimeError("cannot schedule new futures after shutdown")

            return fail()

        with monkeypatch.context() as patcher:
            patcher.setattr(asyncio, "to_thread", refuse)
            assert lock._lock.acquire(blocking=False)  # a sibling's in-flight chain
            try:
                with pytest.raises(RuntimeError, match="cannot schedule new futures"):
                    await lock.__aenter__()
                assert lock._lock.locked()
            finally:
                lock._lock.release()
        async with asyncio.timeout(5):
            async with lock:
                pass

    def test_a_teardown_cancelled_close_does_not_strand_a_sibling(self):
        """The same leak through the public path: one server's shutdown close
        queued behind another's in-flight chain, cancelled by loop teardown,
        must not block the live sibling's own shutdown close."""
        live = redirects.RedirectClientOwner()
        doomed = redirects.RedirectClientOwner()
        live_client = live.get()
        doomed.get()
        release_chain = threading.Event()

        def hold_like_a_chain() -> None:
            guard._PIN_FETCH_LOCK._lock.acquire()
            release_chain.wait(5)
            guard._PIN_FETCH_LOCK._lock.release()

        holder = threading.Thread(target=hold_like_a_chain, daemon=True)
        holder.start()
        time.sleep(0.1)

        async def torn_down_shutdown() -> None:
            asyncio.ensure_future(doomed.aclose())
            await asyncio.sleep(0.15)  # the close is queued behind the chain
            # returning cancels the close task, as loop teardown does

        asyncio.run(torn_down_shutdown())
        release_chain.set()
        holder.join()

        outcomes: list[str] = []

        def run_live_shutdown() -> None:
            async def close() -> None:
                async with asyncio.timeout(5):
                    await live.aclose()

            try:
                asyncio.run(close())
                outcomes.append("closed")
            except RuntimeError as exc:
                outcomes.append(str(exc))

        # A leaked lock blocks inside a worker thread that asyncio.run joins,
        # so observe the close off-loop rather than hanging the whole suite.
        shutdown = threading.Thread(target=run_live_shutdown, daemon=True)
        shutdown.start()
        shutdown.join(6)
        assert not shutdown.is_alive(), "the live sibling's close is stuck on a leaked lock"
        assert outcomes == ["closed"]
        assert live_client.is_closed

    def test_the_fetch_lock_excludes_a_second_loop(self):
        """Serialization is process-wide, not per loop: two loops in two threads
        must never hold the pinned-resolver section at the same time."""
        events: list[str] = []

        def run_loop(tag: str) -> None:
            async def work() -> None:
                async with logs_module._PIN_FETCH_LOCK:
                    events.append(f"{tag}-in")
                    await asyncio.sleep(0.05)
                    events.append(f"{tag}-out")

            asyncio.run(work())

        threads = [threading.Thread(target=run_loop, args=(tag,)) for tag in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(events) == 4
        assert events[0][:-3] == events[1][:-4], f"the two loops interleaved inside the section: {events}"

    async def test_a_cookie_from_one_download_does_not_reach_the_next(self, tmp_path, unauthenticated):
        """The persistent client shares one cookie jar, so each chain must start from
        an empty one and leave one: a fresh client per download used to guarantee that.

        Removing both clears lets the target's cookie ride on the next download's
        request; each clear is pinned on its own by the two tests that follow.
        """
        client = unauthenticated(
            [
                _FakeResponse("https://storage.example/one.csv", chunks=["a\n"], set_cookie="session=s1"),
                _FakeResponse("https://storage.example/two.csv", chunks=["b\n"]),
            ]
        )
        for name in ("one.csv", "two.csv"):
            await logs_module._follow_redirects_to_tempfile(
                httpx.URL(f"https://storage.example/{name}"), str(tmp_path / name)
            )
        assert client.jars == [{}, {}], f"a download requested with the previous download's cookies: {client.jars}"
        assert client.cookies == {}, "a target's cookie lingered in the shared jar after its download"

    async def test_a_dirty_jar_is_cleared_before_the_next_chain_starts(self, tmp_path, unauthenticated):
        """The entry clear is what a jar dirtied outside any chain cannot get past.

        Whatever left ``session`` in the jar - an interrupted chain, a future call
        site that reuses the client - the next download must still request with an
        empty one. Removing the entry clear fails this; the exit clear cannot cover
        it, because the write happened after the previous chain ended.
        """
        client = unauthenticated([_FakeResponse("https://storage.example/one.csv", chunks=["a\n"])])
        client.cookies["session"] = "stale"
        await logs_module._follow_redirects_to_tempfile(
            httpx.URL("https://storage.example/one.csv"), str(tmp_path / "one.csv")
        )
        assert client.jars == [{}], f"the chain started with a cookie already in the jar: {client.jars}"

    async def test_a_cookie_set_mid_chain_still_reaches_the_next_hop(self, tmp_path, unauthenticated):
        """The clears bracket the chain, not its hops: inside one download a target's
        cookie is still sent to the next hop, exactly as a fresh client sent it."""
        client = unauthenticated(
            [
                _FakeResponse(
                    "https://storage.example/first.csv",
                    location="https://storage.example/final.csv",
                    set_cookie="session=s1",
                ),
                _FakeResponse("https://storage.example/final.csv", chunks=["host,reason\na.com,blocked\n"]),
            ]
        )
        await logs_module._follow_redirects_to_tempfile(
            httpx.URL("https://storage.example/first.csv"), str(tmp_path / "d.csv")
        )
        assert client.jars == [{}, {"session": "s1"}], "the chain must not clear its own hops' cookies"
        assert client.cookies == {}, "the chain must leave an empty jar behind"

    async def test_the_request_timeout_is_re_read_per_download(self, tmp_path, unauthenticated, monkeypatch):
        """A fresh client per download re-read ``NEXTDNS_HTTP_TIMEOUT`` at construction;
        the shared client must re-read it per request, or a mid-process change to the
        setting silently stops applying to every later download (PM scope finding).

        Removing the per-request ``timeout=`` leaves the first construction's
        value baked in and fails the second assertion below.
        """
        client = unauthenticated(
            [
                _FakeResponse("https://storage.example/one.csv", chunks=["a\n"]),
                _FakeResponse("https://storage.example/two.csv", chunks=["b\n"]),
            ]
        )
        monkeypatch.setattr(redirects.client, "get_http_timeout", lambda: 5.0)
        await logs_module._follow_redirects_to_tempfile(
            httpx.URL("https://storage.example/one.csv"), str(tmp_path / "one.csv")
        )
        monkeypatch.setattr(redirects.client, "get_http_timeout", lambda: 9.0)
        await logs_module._follow_redirects_to_tempfile(
            httpx.URL("https://storage.example/two.csv"), str(tmp_path / "two.csv")
        )
        assert client.timeouts == [5.0, 9.0], f"the request timeout froze at first construction: {client.timeouts}"


class TestTempFileCleanup:
    """A call that does not end in a download leaves neither a file nor a directory."""

    def test_a_file_that_was_never_written_is_not_an_error(self, tmp_path, caplog):
        """The ACL can refuse before a byte is written, so there is nothing to unlink."""
        missing = str(tmp_path / "download.csv")
        logs_module._remove_temp_file(missing)
        assert "Failed to remove" not in caplog.text

    def test_a_file_that_cannot_be_removed_is_reported(self, tmp_path, monkeypatch, caplog):
        """A failed unlink is logged, not raised: it must not mask the download's error."""
        path = tmp_path / "download.csv"
        path.write_text("partial")

        def refuse(target: str, *args: Any) -> None:
            raise PermissionError("device busy")

        monkeypatch.setattr(os, "unlink", refuse)
        with caplog.at_level("WARNING"):
            logs_module._remove_temp_file(str(path))
        assert "Failed to remove temp log file" in caplog.text

    def test_a_parent_directory_that_cannot_be_removed_is_not_fatal(self, tmp_path, monkeypatch):
        """Another download may be writing in the same parent; the call still returns.

        The file is gone, which is the part that matters. Failing to remove the
        directory is not worth replacing the download's own error with an
        unrelated one about a directory.
        """
        path = tmp_path / "download.csv"
        path.write_text("partial")

        def refuse(target: str, *args: Any) -> None:
            raise OSError("not empty")

        monkeypatch.setattr(os, "rmdir", refuse)
        logs_module._remove_temp_file(str(path))
        assert not path.exists()
