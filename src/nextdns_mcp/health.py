"""``/health`` readiness endpoint and its NextDNS API probe.

``GET /health`` is a real readiness check, not a liveness constant: it probes
the NextDNS API with the configured API key and returns ``200 OK`` with
``{"status": "ok"}`` only when that probe succeeds. A failing probe returns
``503`` with the minimal body ``{"status": "error"}`` — no failure class and
no reason. The HTTP transport is unauthenticated by design (``docs/safety.md``:
an authenticating reverse proxy is the caller's job), so anything this body
says is readable by every host that can reach the port. A class of ``auth``
plus a reason naming the upstream status is an oracle for "is this API key
accepted", which is not something a route that cannot identify its caller
should answer (issue #603). The class and reason stay on the operator's side
of the boundary, in the process log (``_log_readiness_failure``). Results are
cached for ``HEALTH_CACHE_TTL`` seconds so a polling load balancer cannot
hammer the API. The route is registered with FastMCP's ``custom_route`` so it
is served alongside the ``/mcp`` streamable-HTTP endpoint; in stdio mode there
is no HTTP surface, so the route is simply not reachable.

The probe sends through the shared raw probe client (issue #603's ACL-free
transport, owned upstream of this module's scope).

SPDX-License-Identifier: MIT
"""

import asyncio
import logging
import re
import time
from typing import NamedTuple

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .client import get_raw_probe_client
from .config import ConfigurationError

logger = logging.getLogger(__name__)

HEALTH_PROBE_PATH = "/profiles"
"""Cheapest authenticated read on the NextDNS API.

``GET /profiles`` returns the caller's profile list, so a 2xx proves the API
key is accepted without touching (or mutating) any specific profile."""

HEALTH_PROBE_TIMEOUT = 5.0
"""Seconds allowed for the readiness probe before NextDNS is called unreachable."""

HEALTH_CACHE_TTL = 30.0
"""Seconds a probe result is reused, so health polling cannot hammer the API."""

HEALTH_FAILURE_AUTH = "auth"
"""Failure class: NextDNS rejected the configured credentials, or no usable API key is
configured locally (missing or empty NEXTDNS_API_KEY, unreadable NEXTDNS_API_KEY_FILE)."""

HEALTH_FAILURE_UNREACHABLE = "unreachable"
"""Failure class: NextDNS could not be probed, or answered unusably."""


class _HealthFailure(NamedTuple):
    """A failed readiness probe, kept on the operator's side of the boundary.

    Both fields are reported to the server log (`_log_readiness_failure`) and
    never over ``/health``: the HTTP transport is unauthenticated by design, so
    the class and reason would tell any host that can reach the port whether the
    configured API key is accepted (issue #603).

    Attributes:
        failure_class: ``"auth"`` or ``"unreachable"``.
        reason: Short operator-facing explanation. Built only from the HTTP
            status code, the exception type name, and configuration state, so it
            can never carry API key material, auth headers, or the upstream
            response body.
    """

    failure_class: str
    reason: str


class ReadinessProber:
    """Owns the ``/health`` readiness cache and single-flight lock for one server.

    Explicit owner for the probe state that used to live in the module-global
    ``_health_probe_cache`` singleton and its module-global lock. Each
    ``FastMCP`` server gets its own prober when its ``/health`` route is
    registered, so two servers embedded in one process keep independent
    readiness state and never serialize each other's probes.
    """

    def __init__(self) -> None:
        self._cache: tuple[float, _HealthFailure | None] | None = None
        # Guards probe *construction*, not cache reads: concurrent ``/health``
        # pollers that all miss an empty (or just-expired) cache used to each
        # fire their own upstream request - a thundering herd against the
        # cheapest authenticated endpoint, which is exactly what the TTL cache
        # exists to prevent (the cache bounds steady-state polling, not the
        # simultaneous first miss). The lock makes the probe single-flight:
        # one request goes upstream, everyone else waits on it and reads the
        # filled cache.
        self._lock = asyncio.Lock()

    async def check(self) -> _HealthFailure | None:
        """Return the readiness result, probing NextDNS at most once per cache window.

        Single-flight: concurrent pollers that all miss an empty or expired
        cache wait on the one in-flight probe instead of each issuing their own
        (see ``self._lock``). A poller that arrives while a probe is running
        returns that probe's result, not a stale or duplicate one.

        The probe transport is the shared raw probe client (issue #603's
        ACL-free transport, owned upstream of this card's scope).

        Returns:
            None when the configured credentials work against NextDNS,
            otherwise a _HealthFailure describing why the service is not ready.
        """
        fresh, failure = self._cached()
        if fresh:
            logger.debug("Reusing cached /health readiness result")
            return failure

        async with self._lock:
            fresh, failure = self._cached()
            if fresh:
                logger.debug("Reusing /health readiness result filled by a concurrent probe")
                return failure
            failure = await self._probe()
            self._cache = (time.monotonic(), failure)
            if failure is not None:
                # One record per real probe, not per poll: the detail lives here
                # because it cannot live in the /health body (see #603).
                _log_readiness_failure(failure)
            return failure

    def _cached(self) -> tuple[bool, _HealthFailure | None]:
        """Whether the cached probe result is still valid, and what it said.

        ``(fresh, failure)``; ``failure`` is the cached result (``None``
        meaning *proven healthy*) only when ``fresh`` is True.
        """
        cached = self._cache
        if cached is None or time.monotonic() - cached[0] >= HEALTH_CACHE_TTL:
            return False, None
        return True, cached[1]

    async def _probe(self) -> _HealthFailure | None:
        """Probe the NextDNS API once with the configured credentials.

        The request goes through the raw probe transport
        (``client.get_raw_probe_client()``), which bypasses profile access
        control: a local ACL denial must not mask the true upstream
        authentication state. Only the status code and the exception type
        name are reported, never the API key, the auth headers, or the response
        body.

        Returns:
            None when NextDNS accepted the credentials, otherwise a
            _HealthFailure.
        """
        try:
            probe_client = get_raw_probe_client()
            response = await probe_client.request("GET", HEALTH_PROBE_PATH, timeout=HEALTH_PROBE_TIMEOUT)
        except ConfigurationError as exc:
            return _configuration_failure(exc)
        except httpx.TimeoutException:
            return _HealthFailure(
                HEALTH_FAILURE_UNREACHABLE,
                f"NextDNS API did not respond within {HEALTH_PROBE_TIMEOUT:g}s",
            )
        except httpx.HTTPError as exc:
            return _HealthFailure(
                HEALTH_FAILURE_UNREACHABLE,
                f"Could not reach the NextDNS API ({type(exc).__name__})",
            )

        if response.is_success:
            return None
        return _probe_status_failure(response)


def register_health_route(mcp: FastMCP) -> None:
    """Register the ``/health`` readiness endpoint on the FastMCP HTTP app.

    See the module docstring for the probe contract this route serves. The
    prober — the route's TTL cache and single-flight lock — is owned here, per
    server, so two embedded servers never share readiness state.

    Args:
        mcp: The FastMCP server to attach the route to.
    """
    prober = ReadinessProber()

    async def health_check(_request: Request) -> JSONResponse:
        """Report NextDNS API readiness as measured by a live credential probe."""
        failure = await prober.check()
        if failure is None:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "error"}, status_code=503)

    mcp.custom_route("/health", methods=["GET"])(health_check)


def _log_readiness_failure(failure: _HealthFailure) -> None:
    """Report a failed probe to the operator, where the response body cannot.

    The class and reason identify why the server is not ready — an auth failure
    and an unreachable API need different fixes — so closing the body has to
    move that detail, not drop it. Both fields are built from a status code, an
    exception type name, and configuration state (`_HealthFailure`), never from a
    request URL or an exception message, so this WARNING needs no redaction
    pass (`_redacted()` in `client.py` guards those shapes).
    """
    logger.warning("NextDNS readiness probe failed: class=%s reason=%s", failure.failure_class, failure.reason)


_CONFIG_ENV_VAR = re.compile(r"NEXTDNS_[A-Z0-9_]+")
"""A NEXTDNS_* environment variable name inside a ConfigurationError message.

Every ConfigurationError the client constructors raise names the setting it
blames, so the classification reads the variable out of the caught exception
instead of re-reading os.environ and guessing (issue #785)."""

_API_KEY_ENV_VARS = frozenset({"NEXTDNS_API_KEY", "NEXTDNS_API_KEY_FILE"})
"""Variables whose failure is an authentication problem, not an unreachable API."""


def _configuration_failure(exc: ConfigurationError) -> _HealthFailure:
    """Classify a ConfigurationError from building the client by the setting it names.

    Only the variable name is reported - never the exception message, which can
    quote the invalid value - so the warning stays sanitized while the operator
    learns which setting to fix.
    """
    match = _CONFIG_ENV_VAR.search(str(exc))
    if match is not None and match.group(0) in _API_KEY_ENV_VARS:
        return _HealthFailure(HEALTH_FAILURE_AUTH, "No usable NextDNS API key is configured")
    if match is not None:
        return _HealthFailure(
            HEALTH_FAILURE_UNREACHABLE,
            f"NextDNS client configuration is invalid (check {match.group(0)})",
        )
    return _HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        "NextDNS client configuration is invalid",
    )


def _probe_status_failure(response: httpx.Response) -> _HealthFailure:
    """Classify a non-success probe answer: 401/403 is auth, anything else unreachable."""
    if response.status_code in (401, 403):
        return _HealthFailure(
            HEALTH_FAILURE_AUTH,
            f"NextDNS API rejected the configured credentials (HTTP {response.status_code})",
        )
    return _HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        f"NextDNS API answered with an unusable status (HTTP {response.status_code})",
    )
