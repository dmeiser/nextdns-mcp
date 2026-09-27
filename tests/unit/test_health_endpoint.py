"""Unit tests for the /health readiness endpoint.

``GET /health`` is a real readiness check: it probes the NextDNS API with the
configured API key and returns ``200 OK`` with ``{"status": "ok"}`` only when
that probe succeeds. Failures return ``503`` with the failure class and a short
reason, and the result is cached so health polling cannot hammer the API.
"""

from collections.abc import Iterator

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import openapi
from nextdns_mcp.openapi import (
    HEALTH_CACHE_TTL,
    HEALTH_PROBE_PATH,
    HEALTH_PROBE_TIMEOUT,
    create_mcp_server,
)
from nextdns_mcp.server import get_mcp_server


class FakeApiClient:
    """Stand-in for the shared API client that records raw probe calls.

    ``outcomes`` are consumed in order; each entry is either an
    ``httpx.Response`` to return or an exception instance to raise.
    """

    def __init__(self, *outcomes: httpx.Response | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, str, dict]] = []

    async def raw_request(self, method: str, url: str, **kwargs) -> httpx.Response:
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


def _install_client(monkeypatch, *outcomes: httpx.Response | Exception) -> FakeApiClient:
    """Point the health probe at a fake client and reset the result cache."""
    client = FakeApiClient(*outcomes)
    monkeypatch.setattr(openapi, "get_api_client", lambda: client)
    openapi._health_probe_cache = None
    return client


async def _call_health() -> tuple[int, dict]:
    """GET /health over the real HTTP app and return (status code, parsed body)."""
    app = create_mcp_server().http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")
    return response.status_code, response.json()


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> Iterator[None]:
    """Keep the module-level readiness cache from leaking between tests."""
    openapi._health_probe_cache = None
    yield
    openapi._health_probe_cache = None


async def test_health_returns_200_when_the_credential_probe_succeeds(monkeypatch, mock_api_key):
    """A successful NextDNS probe yields 200 with {"status": "ok"}."""
    client = _install_client(monkeypatch, _response(200, "[]"))

    status_code, body = await _call_health()

    assert status_code == 200
    assert body == {"status": "ok"}
    assert client.calls == [("GET", HEALTH_PROBE_PATH, {"timeout": HEALTH_PROBE_TIMEOUT})]


async def test_health_returns_503_auth_class_when_nextdns_rejects_the_key(monkeypatch, mock_api_key):
    """An upstream 401 is reported as 503 with the auth failure class."""
    _install_client(monkeypatch, _response(401, '{"error":"API key not found","token":"super-secret"}'))

    status_code, body = await _call_health()

    assert status_code == 503
    assert body["class"] == "auth"
    assert body["status"] == "error"
    assert "401" in body["reason"]


async def test_health_never_leaks_key_material_or_the_probe_body(monkeypatch, mock_api_key):
    """Failure payloads carry the class and a short reason, never secrets or the raw body."""
    secret = mock_api_key
    upstream_body = f'{{"error":"invalid key","api_key":"{secret}"}}'
    _install_client(monkeypatch, _response(401, upstream_body))

    _, body = await _call_health()

    serialized = str(body)
    assert secret not in serialized
    assert upstream_body not in serialized
    assert body["class"] == "auth"
    assert body["reason"] == "NextDNS API rejected the configured credentials (HTTP 401)"


async def test_health_returns_503_auth_class_when_no_api_key_is_usable(monkeypatch):
    """A missing or empty API key is reported as 503 with the auth class, not a 500."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)
    monkeypatch.setattr(client_module, "_client", None)
    openapi._health_probe_cache = None

    status_code, body = await _call_health()

    assert status_code == 503
    assert body["status"] == "error"
    assert body["class"] == "auth"
    assert "API key" in body["reason"]


async def test_health_reports_an_invalid_http_timeout_as_unreachable(monkeypatch, mock_api_key):
    """An invalid NEXTDNS_HTTP_TIMEOUT is never mislabeled as an auth failure."""
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "not-a-number")
    monkeypatch.setattr(client_module, "_client", None)
    openapi._health_probe_cache = None

    status_code, body = await _call_health()

    assert status_code == 503
    assert body["class"] == "unreachable"
    assert "NEXTDNS_HTTP_TIMEOUT" in body["reason"]


async def test_configuration_failure_is_cached_like_any_other_probe_result(monkeypatch):
    """Repeated polling inside the TTL reuses the configuration-failure result."""
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE", raising=False)
    monkeypatch.setattr(client_module, "_client", None)
    openapi._health_probe_cache = None

    assert (await _call_health())[0] == 503
    first_result = openapi._health_probe_cache

    assert (await _call_health())[0] == 503
    assert openapi._health_probe_cache is first_result


async def test_health_returns_503_unreachable_class_on_timeout(monkeypatch, mock_api_key):
    """A probe that times out is reported as 503 with the unreachable class."""
    _install_client(monkeypatch, httpx.ReadTimeout("timed out"))

    status_code, body = await _call_health()

    assert status_code == 503
    assert body["class"] == "unreachable"
    assert "5s" in body["reason"]


async def test_health_returns_503_unreachable_class_when_nextdns_is_down(monkeypatch, mock_api_key):
    """A connection error is reported as 503 with the unreachable class."""
    _install_client(monkeypatch, httpx.ConnectError("no route to host"))

    status_code, body = await _call_health()

    assert status_code == 503
    assert body["class"] == "unreachable"
    assert "ConnectError" in body["reason"]


async def test_health_returns_503_unreachable_class_on_upstream_server_error(monkeypatch, mock_api_key):
    """A non-auth error status from NextDNS is reported as unreachable."""
    _install_client(monkeypatch, _response(503, "upstream boom"))

    status_code, body = await _call_health()

    assert status_code == 503
    assert body["class"] == "unreachable"
    assert "503" in body["reason"]


async def test_second_health_call_within_the_window_does_not_re_probe(monkeypatch, mock_api_key):
    """Health polling inside the cache window reuses the first probe result."""
    client = _install_client(monkeypatch, _response(200, "[]"))

    assert (await _call_health())[0] == 200
    assert (await _call_health())[0] == 200
    assert (await _call_health())[0] == 200

    assert len(client.calls) == 1


async def test_cached_failure_is_reused_and_expires_after_the_ttl(monkeypatch, mock_api_key):
    """A cached failure is reused, and a re-probe happens once the TTL elapses."""
    client = _install_client(monkeypatch, _response(401), _response(200, "[]"))

    assert (await _call_health())[0] == 503
    assert (await _call_health())[0] == 503
    assert len(client.calls) == 1

    # Age the cached entry past the TTL by backdating its timestamp.
    cached_at, failure = openapi._health_probe_cache
    openapi._health_probe_cache = (cached_at - (HEALTH_CACHE_TTL + 1), failure)
    assert failure is not None

    assert (await _call_health())[0] == 200
    assert len(client.calls) == 2


async def test_probe_issues_a_raw_request_with_the_short_timeout(monkeypatch, mock_api_key):
    """The probe calls the client's raw transport with the 5 second timeout."""
    client = _install_client(monkeypatch, _response(200, "[]"))

    assert (await _call_health())[0] == 200

    method, url, kwargs = client.calls[0]
    assert (method, url) == ("GET", "/profiles")
    assert kwargs["timeout"] == 5.0


async def test_health_served_by_http_app(monkeypatch, mock_api_key):
    """GET /health on the streamable-HTTP app reports 200 once the probe succeeds."""
    _install_client(monkeypatch, _response(200, "[]"))
    server = get_mcp_server()
    app = server.http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


async def test_health_served_by_http_app_reports_failures(monkeypatch, mock_api_key):
    """A failing probe surfaces as a 503 JSON body over the real HTTP app."""
    _install_client(monkeypatch, _response(401))
    server = get_mcp_server()
    app = server.http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["class"] == "auth"
