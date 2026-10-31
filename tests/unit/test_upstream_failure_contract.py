"""Upstream-failure contract at the product surface (issue #181).

These tests drive a real local HTTP server that plays the NextDNS API, a real
``AccessControlledClient`` pointed at it, and the real tool call path. Nothing
in ``nextdns_mcp`` is mocked, so what is asserted is what an MCP caller
observes: upstream ``429``/``5xx``/``401`` become typed ``NextDNSError``
subclasses carrying the status and body (so a caller can retry on 429 and
circuit-break on 5xx), and every registered tool still reports a standardized
error payload rather than success.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest
from fastmcp import Client

from nextdns_mcp import client as client_module
from nextdns_mcp import utils
from nextdns_mcp.errors import NextDNSAuthError, NextDNSError, NextDNSRateLimitError, NextDNSServerError
from nextdns_mcp.server import build_mcp_server

# Path served by the stub -> (status, body). The plot series path carries a
# query string; the stub matches on path only.
_UPSTREAM: dict[str, tuple[int, str]] = {}


class _StubUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self) -> None:
        path = urlsplit(self.path).path
        # Drain the request body before answering. The stub speaks HTTP/1.1 with
        # keep-alive, so an unread body stays in the socket buffer and is parsed as
        # the next request line: a POST's JSON would come back as the following
        # call's method name and the stub would answer 501 instead of the status
        # under test.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        status, body = _UPSTREAM[path]
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

    def log_message(self, *args: object) -> None:  # keep test output clean
        pass


@pytest.fixture
async def stub_nextdns_api(monkeypatch):
    """Serve canned upstream statuses over real HTTP through the real ACL client."""
    _UPSTREAM.update(
        {
            "/profiles": (200, json.dumps({"data": [{"id": "abc123", "name": "Home Network"}]})),
            "/profiles/abc123/settings": (200, json.dumps({"data": {"name": "Home"}})),
            "/profiles/abc123/denylist": (200, json.dumps({"data": []})),
            "/profiles/abc123/privacy": (200, json.dumps({"data": []})),
            "/profiles/abc123/rewrites": (200, json.dumps({"data": []})),
            "/profiles/abc123/logs": (200, json.dumps({"data": []})),
            "/profiles/abc123/analytics/status;series": (200, json.dumps({"data": []})),
            "/upstream/rate-limited": (429, '{"error":"rate limit exceeded","retryAfter":30}'),
            "/upstream/unavailable": (503, '{"error":"upstream unavailable"}'),
            "/upstream/unauthorized": (401, '{"error":"invalid api key"}'),
        }
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    client = client_module.AccessControlledClient(
        base_url=base_url,
        headers={"X-Api-Key": "test-key", "Accept": "application/json", "Content-Type": "application/json"},
        timeout=5.0,
        follow_redirects=False,
    )
    # Every production read of the client goes through ``client.api_client``,
    # which is served by the module's lazy ``__getattr__`` and therefore resolves
    # to the ``_client`` singleton -- unless a real entry is sitting in the
    # module dict, because a test that injected ``api_client`` directly leaves
    # one behind on teardown (its monkeypatch probe is answered by that same
    # ``__getattr__``). Such a leftover would shadow the stub and send these
    # requests to the real api.nextdns.io on a dead event loop, so drop it here
    # and again on the way out instead of depending on which tests ran before.
    client_module.__dict__.pop("api_client", None)
    monkeypatch.setattr(client_module, "_client", client)
    try:
        yield base_url
    finally:
        client_module.__dict__.pop("api_client", None)
        await client.aclose()
        httpd.shutdown()
        httpd.server_close()
        _UPSTREAM.clear()


def _serve(status: int, body: str) -> None:
    """Point every stubbed upstream path at the same canned failure."""
    for path in list(_UPSTREAM):
        _UPSTREAM[path] = (status, body)


@pytest.mark.parametrize(
    ("path", "expected_type", "expected_status"),
    [
        ("/upstream/rate-limited", NextDNSRateLimitError, 429),
        ("/upstream/unavailable", NextDNSServerError, 503),
        ("/upstream/unauthorized", NextDNSAuthError, 401),
    ],
)
@pytest.mark.asyncio
async def test_upstream_status_raises_typed_exception_with_status_and_body(
    stub_nextdns_api, path, expected_type, expected_status
):
    """The caller-facing surface a retry/circuit-breaker needs: type, status, body."""
    with pytest.raises(expected_type) as excinfo:
        await utils._api_request("GET", path)

    assert isinstance(excinfo.value, NextDNSError)
    assert excinfo.value.status_code == expected_status
    assert excinfo.value.response_body == _UPSTREAM[path][1]
    # The original transport error stays reachable for logging/retries.
    assert excinfo.value.__cause__ is not None


@pytest.mark.asyncio
async def test_successful_upstream_response_is_unchanged(stub_nextdns_api):
    payload = await utils._api_request_payload("GET", "/profiles")
    assert payload == {"data": [{"id": "abc123", "name": "Home Network"}]}


@pytest.mark.asyncio
async def test_non_http_failure_raises_base_exception_without_status(stub_nextdns_api, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(client_module.get_api_client(), "request", boom)

    with pytest.raises(NextDNSError) as excinfo:
        await utils._api_request("GET", "/profiles")
    assert type(excinfo.value) is NextDNSError
    assert excinfo.value.status_code is None
    assert excinfo.value.response_body is None


class TestToolsReportUpstreamFailuresAsPayloads:
    """No tool may report success, or an untyped crash, when upstream fails.

    The set below is representative rather than exhaustive: one call per HTTP
    method the surface uses (GET, POST, PUT, PATCH, DELETE), a collection
    endpoint beside the profile-scoped ones, and the hand-written plot tool,
    which reaches the API through its own fetch helper instead of a generated
    body. Those are the paths where a failure can be swallowed differently.
    """

    _CALLS: ClassVar[tuple[tuple[str, dict[str, Any]], ...]] = (
        ("listProfiles", {}),
        ("getSettings", {"profile_id": "abc123"}),
        ("getDenylist", {"profile_id": "abc123"}),
        ("addToDenylist", {"profile_id": "abc123", "id": "example.com"}),
        ("replaceDenylist", {"profile_id": "abc123", "body": [{"id": "example.com"}]}),
        ("updatePrivacySettings", {"profile_id": "abc123", "disguisedTrackers": True}),
        ("listRewrites", {"profile_id": "abc123"}),
        ("getLogs", {"profile_id": "abc123"}),
        ("clearLogs", {"profile_id": "abc123"}),
        ("getAnalyticsStatusSeries", {"profile_id": "abc123"}),
        ("plotAnalytics", {"metric": "status", "profile_id": "abc123"}),
    )

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "test-key")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 429, 500, 503])
    async def test_every_tool_returns_typed_error_payload(self, stub_nextdns_api, status):
        _serve(status, '{"error":"upstream failure"}')

        async with Client(build_mcp_server()) as client:
            for tool, args in self._CALLS:
                result = await client.call_tool(tool, args)
                payload = result.structured_content
                assert payload.get("code") == "http_error", f"{tool} did not report http_error: {payload}"
                assert payload.get("status_code") == status, f"{tool} lost the upstream status: {payload}"
                assert "success" not in payload

    @pytest.mark.asyncio
    async def test_tools_still_succeed_when_upstream_is_healthy(self, stub_nextdns_api):
        """The failure handling above must not swallow a good response."""
        async with Client(build_mcp_server()) as client:
            profiles = await client.call_tool("listProfiles", {})
            denylist = await client.call_tool("getDenylist", {"profile_id": "abc123"})
        assert profiles.structured_content == {"data": [{"id": "abc123", "name": "Home Network"}]}
        assert denylist.structured_content == {"data": []}
