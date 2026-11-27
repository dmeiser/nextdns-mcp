"""Unit tests for the /health readiness endpoint.

``GET /health`` is a real readiness check: it probes the NextDNS API with the
configured API key and returns ``200 OK`` with ``{"status": "ok"}`` only when
that probe succeeds. Failures return ``503`` with the minimal body
``{"status": "error"}`` — never the failure class or the reason, since the
HTTP transport is unauthenticated by design (issue #603). The class and reason
move to the server log instead. The result is cached so health polling cannot
hammer the API; the cache and its single-flight lock are owned by one
``ReadinessProber`` per server (issue #642), and the probe sends through the
raw probe transport owner the server instance owns.
"""

import logging

import httpx

from nextdns_mcp import client as client_module
from nextdns_mcp import health
from nextdns_mcp.config import ConfigurationError
from nextdns_mcp.health import (
    HEALTH_CACHE_TTL,
    HEALTH_PROBE_PATH,
    HEALTH_PROBE_TIMEOUT,
    ReadinessProber,
)
from nextdns_mcp.openapi import create_mcp_server


class FakeProbeTransport:
    """Stand-in for the raw probe transport that records probe calls.

    ``outcomes`` are consumed in order; each entry is either an
    ``httpx.Response`` to return or an exception instance to raise.
    """

    def __init__(self, *outcomes: httpx.Response | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        outcome = self._outcomes.pop(0) if self._outcomes else self._outcomes
        if isinstance(outcome, Exception):
            raise outcome
        assert outcome is not None
        return outcome


def _response(status_code: int, body: str = '{"error":"Forbidden"}') -> httpx.Response:
    return httpx.Response(
        status_code,
        text=body,
        request=httpx.Request("GET", f"https://api.nextdns.io{HEALTH_PROBE_PATH}"),
    )


def _install_client(monkeypatch, *outcomes: httpx.Response | Exception) -> FakeProbeTransport:
    """Point the health probe at a fake transport by patching the lazy build.

    The prober resolves its transport through the server-owned
    ``ProbeClientOwner``, whose lazy build calls ``create_raw_probe_client``
    at call time - patching the factory routes every owner (the standalone
    probers here and the one a created server registers) at the fake.
    """
    client = FakeProbeTransport(*outcomes)
    monkeypatch.setattr(client_module, "create_raw_probe_client", lambda: client)
    return client


def _prober_with(monkeypatch, *outcomes: httpx.Response | Exception) -> tuple[ReadinessProber, FakeProbeTransport]:
    """A prober whose probe sends through a fake transport."""
    return ReadinessProber(), _install_client(monkeypatch, *outcomes)


async def _call_health(app) -> tuple[int, dict]:
    """GET /health on the given server app and return (status code, parsed body)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")
    return response.status_code, response.json()


async def test_health_returns_200_when_the_credential_probe_succeeds(monkeypatch, mock_api_key):
    """A successful NextDNS probe yields 200 with {"status": "ok"}."""
    client = _install_client(monkeypatch, _response(200, "[]"))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 200
    assert body == {"status": "ok"}
    assert client.calls == [("GET", HEALTH_PROBE_PATH, {"timeout": HEALTH_PROBE_TIMEOUT})]


async def test_unauthenticated_health_does_not_disclose_auth_class(monkeypatch, mock_api_key):
    """An upstream 401 answers 503 with no class and no reason to read (issue #603).

    Before the fix this body was ``{"status": "error", "class": "auth",
    "reason": "... (HTTP 401)"}``, which told any host that could reach the
    port that the configured key was rejected.
    """
    _install_client(monkeypatch, _response(401, '{"error":"API key not found","token":"super-secret"}'))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert "class" not in body
    assert "reason" not in body


async def test_unauthenticated_health_does_not_disclose_unreachable_class(monkeypatch, mock_api_key):
    """An upstream ConnectError answers 503 with no class and no reason to read."""
    _install_client(monkeypatch, httpx.ConnectError("no route to host"))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert "class" not in body
    assert "reason" not in body


async def test_health_503_body_is_exactly_the_minimal_error_shape(monkeypatch, mock_api_key):
    """The failure body carries the status field and nothing else."""
    _install_client(monkeypatch, _response(401))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}


async def test_health_failure_detail_reaches_the_server_log(monkeypatch, mock_api_key, caplog):
    """Closing the body moves the diagnostic, it does not drop it.

    An operator still has to tell "NextDNS rejected the key" from "NextDNS is
    down", and the log is the only place left that can say so.
    """
    _install_client(monkeypatch, _response(401))

    with caplog.at_level(logging.WARNING, logger="nextdns_mcp.health"):
        status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}
    assert "class=auth" in caplog.text
    assert "HTTP 401" in caplog.text


async def test_health_never_leaks_key_material_or_the_probe_body(monkeypatch, mock_api_key):
    """Failure payloads carry nothing but the status field, never secrets or the raw body."""
    secret = mock_api_key
    upstream_body = f'{{"error":"invalid key","api_key":"{secret}"}}'
    _install_client(monkeypatch, _response(401, upstream_body))

    _, body = await _call_health(create_mcp_server().http_app())

    serialized = str(body)
    assert secret not in serialized
    assert upstream_body not in serialized
    assert body == {"status": "error"}


async def test_health_returns_503_when_no_api_key_is_usable():
    """A missing or empty API key is reported as 503, not a 500 — and says no more."""
    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}


async def test_an_invalid_http_timeout_is_reported_as_unready_without_naming_it(monkeypatch, mock_api_key):
    """An invalid NEXTDNS_HTTP_TIMEOUT still yields 503, without leaking the cause."""
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "not-a-number")

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}
    assert "NEXTDNS_HTTP_TIMEOUT" not in str(body)


async def test_an_invalid_api_base_is_logged_as_the_api_base(monkeypatch, mock_api_key, caplog):
    """An invalid NEXTDNS_API_BASE names that setting in the warning, not NEXTDNS_HTTP_TIMEOUT."""
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    monkeypatch.setenv("NEXTDNS_API_BASE", "http://api.nextdns.io")

    with caplog.at_level(logging.WARNING, logger="nextdns_mcp.health"):
        status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}
    assert "NEXTDNS_API_BASE" in caplog.text
    assert "NEXTDNS_HTTP_TIMEOUT" not in caplog.text


async def test_a_configuration_error_naming_no_setting_is_reported_generically(monkeypatch, mock_api_key, caplog):
    """A configuration error that names no setting stays generic, and quotes nothing from it."""
    _install_client(monkeypatch, ConfigurationError("client construction failed: value not accepted"))

    with caplog.at_level(logging.WARNING, logger="nextdns_mcp.health"):
        status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}
    assert "NextDNS client configuration is invalid" in caplog.text
    assert "NEXTDNS_" not in caplog.text


async def test_configuration_failure_is_cached_like_any_other_probe_result(monkeypatch):
    """Repeated polling inside the TTL reuses the configuration-failure result."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)
    prober = ReadinessProber()

    assert (await prober.check()) is not None
    first_result = prober._cache

    assert (await prober.check()) is not None
    assert prober._cache is first_result


async def test_health_returns_503_on_timeout(monkeypatch, mock_api_key):
    """A probe that times out is a 503 with the minimal body."""
    _install_client(monkeypatch, httpx.ReadTimeout("timed out"))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}


async def test_health_returns_503_when_nextdns_is_down(monkeypatch, mock_api_key):
    """A connection error is a 503 with the minimal body."""
    _install_client(monkeypatch, httpx.ConnectError("no route to host"))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}


async def test_health_returns_503_on_upstream_server_error(monkeypatch, mock_api_key):
    """A non-auth error status from NextDNS is a 503 with the minimal body."""
    _install_client(monkeypatch, _response(503, "upstream boom"))

    status_code, body = await _call_health(create_mcp_server().http_app())

    assert status_code == 503
    assert body == {"status": "error"}
    assert "503" not in str(body)


async def test_second_health_call_within_the_window_does_not_re_probe(monkeypatch, mock_api_key):
    """Health polling inside the cache window reuses the first probe result."""
    client = _install_client(monkeypatch, _response(200, "[]"))
    app = create_mcp_server().http_app()

    assert (await _call_health(app))[0] == 200
    assert (await _call_health(app))[0] == 200
    assert (await _call_health(app))[0] == 200

    assert len(client.calls) == 1


async def test_cached_failure_is_reused_and_expires_after_the_ttl(monkeypatch, mock_api_key):
    """A cached failure is reused, and a re-probe happens once the TTL elapses."""
    prober, client = _prober_with(monkeypatch, _response(401), _response(200, "[]"))

    assert (await prober.check()) is not None
    assert (await prober.check()) is not None
    assert len(client.calls) == 1

    # Age the cached entry past the TTL by backdating its timestamp.
    cached_at, failure = prober._cache
    prober._cache = (cached_at - (HEALTH_CACHE_TTL + 1), failure)
    assert failure is not None

    assert (await prober.check()) is None
    assert len(client.calls) == 2


async def test_probe_issues_a_raw_request_with_the_short_timeout(monkeypatch, mock_api_key):
    """The probe hits the raw probe transport with the 5 second timeout."""
    client = _install_client(monkeypatch, _response(200, "[]"))

    assert (await _call_health(create_mcp_server().http_app()))[0] == 200

    method, url, kwargs = client.calls[0]
    assert (method, url) == ("GET", "/profiles")
    assert kwargs["timeout"] == 5.0


async def test_health_served_by_http_app(monkeypatch, mock_api_key):
    """GET /health on the streamable-HTTP app reports 200 once the probe succeeds."""
    _install_client(monkeypatch, _response(200, "[]"))
    from nextdns_mcp.server import build_mcp_server

    app = build_mcp_server().http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


async def test_health_served_by_http_app_reports_failures(monkeypatch, mock_api_key):
    """A failing probe surfaces as a 503 minimal JSON body over the real HTTP app."""
    _install_client(monkeypatch, _response(401))
    from nextdns_mcp.server import build_mcp_server

    app = build_mcp_server().http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error"}


async def test_concurrent_pollers_share_one_probe(monkeypatch, mock_api_key):
    """The cache bounds steady polling; the lock bounds the first miss.

    Concurrent pollers that all miss an empty cache used to each fire their
    own request - a thundering herd against the cheapest authenticated
    endpoint, which is what the cache exists to prevent. Single-flight makes
    one request go upstream; everyone else waits on it and reads the result
    that probe filled, rather than a duplicate or a stale one.
    """
    import asyncio

    calls: list[int] = []

    async def slow_probe() -> None:
        calls.append(1)
        await asyncio.sleep(0.05)  # let the other poller arrive and wait on the lock

    prober = ReadinessProber()
    monkeypatch.setattr(prober, "_probe", slow_probe)

    first, second = await asyncio.gather(prober.check(), prober.check())
    assert len(calls) == 1, "the simultaneous first miss must issue exactly one probe"
    assert first is None and second is None


async def test_two_probers_do_not_share_their_cache(monkeypatch, mock_api_key):
    """Readiness state is per server: a second server's prober probes on its own."""
    prober_a, client = _prober_with(monkeypatch, _response(200, "[]"), _response(200, "[]"))
    prober_b = ReadinessProber()

    await prober_a.check()
    await prober_b.check()

    assert len(client.calls) == 2, "each server owns its own readiness cache"


async def test_v1_models_endpoint_answers_empty_list():
    """GET /v1/models answers OpenWebUI's model discovery probe with an empty list."""
    from nextdns_mcp.server import build_mcp_server

    app = build_mcp_server().http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}
