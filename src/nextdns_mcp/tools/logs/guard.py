"""SSRF vetting of the log-download redirect chain and the pinned resolver (issue #266).

The follow-up fetch is unauthenticated, so a ``Location`` from the API
response is checked before any request is made to it: a hop is refused when
its scheme is not https, or when its host resolves to a non-globally-routable
address (loopback, private, link-local and unique-local ranges, which is what
covers 127.0.0.1, 10/8-style ranges and the 169.254.169.254 metadata
endpoint). Any other public https destination is followed, including a host
other than the API origin. The scheme check applies to pinned hosts too, so a
pin can never allow a plaintext fetch.

Known properties of this reachability check:
- The vetted address IS the dialled address (issue #266 review): while the
  hop is fetched, ``socket.getaddrinfo`` is pinned to the answer that was
  vetted (``_pinned_dns``), so httpcore connects to the vetted IP instead of
  resolving the name again — the DNS-rebinding / TOCTOU window between
  "vetted" and "dialled" is closed rather than accepted. The request URL,
  TLS SNI, certificate verification and the Host header all stay hostname-
  based; only the dialled address is pinned. The patch is process-global for
  the duration of one hop's fetch (``socket`` is the only resolver httpcore
  reaches through); other names resolve normally during the window, and the
  pinned name resolves to addresses already vetted as public.
- A resolution failure REFUSES the hop (fail closed, see
  ``_resolve_public_addresses``): the guard does not degrade to
  allow when DNS misbehaves.

Stdlib plus ``httpx.URL``; nothing here fetches, streams, or touches disk.

SPDX-License-Identifier: MIT
"""

import asyncio
import contextlib
import ipaddress
import socket
import threading
from collections.abc import Iterator
from typing import Any, Self

import httpx

# Verified public hosts allowed to serve log downloads even though they do not
# resolve to a globally routable address. Intentionally empty: the real NextDNS
# download host is not yet verified, so nothing is pinned here. To pin one, add
# its hostname to this set with a comment saying why it is trusted.
ALLOWED_DOWNLOAD_SCHEMES = {"https"}

ALLOWED_DOWNLOAD_HOSTS: frozenset[str] = frozenset()


class DownloadRedirectRefusedError(Exception):
    """Raised when a log-download ``Location`` is not a public https destination.

    SSRF guard for the redirect chain: the unauthenticated follow-up fetch is
    never aimed at a non-https or non-globally-routable destination, so a
    compromised or malicious API response cannot use it to reach an internal
    service such as a cloud metadata endpoint (issue #266). Public destinations
    on any host remain reachable.
    """


def _is_public_address(address: str | int) -> bool:
    """Return whether a resolved address is a globally routable unicast address."""
    parsed = ipaddress.ip_address(address)
    return parsed.is_global and not parsed.is_multicast and not parsed.is_reserved


async def _resolve_public_addresses(host: str) -> list[Any] | None:
    """The vetted addrinfo entries for ``host``, or None when the hop must be refused.

    Resolution runs off the event loop because it can block on DNS.

    Fails closed: a name that does not resolve - resolver down, NXDOMAIN,
    empty answer - is refused, not waved through. The old fail-open reading
    argued an unresolvable host cannot be connected to anyway; that argument
    holds only while this guard's resolver and httpx's answer the same
    question with the same result, and a guard that disables itself whenever
    DNS misbehaves is not a guard. The redirect can wait for a working
    resolver; the metadata endpoint cannot wait for anything.

    Returning the vetted entries (not just a bool) is what lets the fetch
    connect to *these* addresses via ``_pinned_dns`` instead of resolving the
    name a second time and hoping the answer held still.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos or not all(_is_public_address(info[4][0]) for info in infos):
        return None
    return list(infos)


# The pinned-resolver swap is process-global (httpcore reaches the resolver
# only through the ``socket`` module attribute), so two downloads running
# their check/fetch windows at the same moment could restore each other's
# wrapper out of order and leave a stale pin installed, or continue one hop
# unpinned. ``_PIN_FETCH_LOCK`` serializes the complete vet-and-fetch
# critical section across all concurrent downloadLogs calls: for one
# download at a time, every hop's resolution, connect, header read and body
# stream run with its own pin installed and nothing else's. A
# per-connection resolver would avoid the serialization, but httpx/httpcore
# expose no hook for one short of replacing the transport stack wholesale;
# serializing is the honest trade, and downloadLogs is a rare call (issue
# #266 review). httpx ties body streaming to the open stream context, so the
# body is inside the section rather than re-resolving outside it.
#
# The lock is process-global because the pin it guards is, so it must be
# usable from every event loop that reaches it: an ``asyncio.Lock`` created
# at import time binds to the first loop that contends it and raises
# ``RuntimeError: ... is bound to a different event loop`` when a second
# loop (another embedded server, another thread) contends it later.
class _Handoff:
    """Who releases a lock whose acquire was abandoned mid-flight.

    A cancelled waiter's worker thread can still finish the acquire after the
    waiter is gone - including when the whole event loop is torn down and
    cancels the shielded task itself, which the shield cannot protect against.
    The worker and the abandoning waiter decide under one mutex, one at a
    time: whichever decision comes second releases, so the process-global lock
    can neither leak nor have a hold released that this waiter never took.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._abandoned = False
        self._held = False

    def worker_took_the_lock(self) -> bool:
        """Record the worker's acquire; report whether it must give it back."""
        with self._mutex:
            self._held = True
            return self._abandoned

    def abandon(self) -> bool:
        """Record the waiter's abandonment; report whether it must release."""
        with self._mutex:
            self._abandoned = True
            return self._held


class _CrossLoopLock:
    """An async lock with no event-loop affinity, safe from any loop or thread.

    The critical section it guards (the pinned resolver) is process-global,
    so the lock is built on ``threading.Lock`` - the one lock primitive whose
    scope matches. Acquiring blocks, so the wait is offloaded to a worker
    thread: the event loop stays responsive while another loop's fetch holds
    the section, and two tasks on one loop cannot deadlock each other the
    way a bare ``threading.Lock.acquire()`` on the loop would.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _acquire_and_hand_off(self, handoff: _Handoff) -> None:
        self._lock.acquire()
        if handoff.worker_took_the_lock():
            # The waiter was abandoned - cancelled by its caller, or by the
            # event loop tearing down around it - before this acquire
            # completed, so nobody is left to use the section: give it back.
            self._lock.release()

    async def __aenter__(self) -> Self:
        handoff = _Handoff()
        acquiring = asyncio.ensure_future(asyncio.to_thread(self._acquire_and_hand_off, handoff))
        try:
            await asyncio.shield(acquiring)
        except BaseException:
            # Cancelled (by the caller's timeout or by loop teardown) or the
            # acquire failed before its worker started. Pair the abandonment
            # with the worker under the handoff's mutex: if the worker already
            # took the lock it never saw the abandonment, so the waiter
            # releases it here; otherwise the worker self-releases when its
            # acquire completes. A waiter whose acquire never ran holds
            # nothing and releases nothing - including another server's hold.
            if handoff.abandon():
                self._lock.release()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._lock.release()


_PIN_FETCH_LOCK = _CrossLoopLock()


@contextlib.contextmanager
def _pinned_dns(host: str | None, infos: list[Any] | None) -> Iterator[None]:
    """Make ``socket.getaddrinfo`` return the vetted ``infos`` for ``host``.

    Installed for the duration of one hop's fetch, so the connection httpcore
    opens dials the address that was vetted, closing the rebinding window
    (module note above). httpcore resolves through the event loop, which
    reaches ``socket.getaddrinfo`` by module attribute at call time - the only
    hook available without replacing the transport stack. The swap is
    process-global while active, so the caller must hold
    :data:`_PIN_FETCH_LOCK` across check-and-fetch: with the lock, no other
    download's swap can interleave, restore out of order, or leave this
    window unpinned. Other name lookups (any host but ``host``) pass through
    to the real resolver while the pin is installed. A no-op for pinned
    allow hosts (``infos is None``), which skip resolution entirely.
    """
    if host is None or infos is None:
        yield
        return
    real = socket.getaddrinfo

    def pinned(name: Any, *args: Any, **kwargs: Any) -> Any:
        resolved_name = name.decode() if isinstance(name, (bytes, bytearray)) else str(name)
        if resolved_name == host:
            return infos
        return real(name, *args, **kwargs)

    socket.getaddrinfo = pinned  # type: ignore[assignment]  # typeshed overloads are narrower than the swap
    try:
        yield
    finally:
        socket.getaddrinfo = real


async def _check_download_redirect_target(next_url: httpx.URL) -> list[Any] | None:
    """Vet one redirect hop; return its vetted addresses, or None for a pinned host.

    Raises:
        DownloadRedirectRefusedError: If the scheme is not in
            :data:`ALLOWED_DOWNLOAD_SCHEMES`, or the host is not pinned in
            :data:`ALLOWED_DOWNLOAD_HOSTS` and does not resolve to only globally
            routable addresses.
    """
    if next_url.scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise DownloadRedirectRefusedError(
            f"Refusing log download redirect to {next_url.scheme}://{next_url.host}: scheme not allowed"
        )
    if next_url.host in ALLOWED_DOWNLOAD_HOSTS:
        return None
    infos = await _resolve_public_addresses(next_url.host)
    if infos is None:
        raise DownloadRedirectRefusedError(
            f"Refusing log download redirect to {next_url.scheme}://{next_url.host}: host is not publicly routable"
        )
    return infos
