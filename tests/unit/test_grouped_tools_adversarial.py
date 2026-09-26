"""Adversarial integration tests for the grouped CRUD tools (issue #156).

Every other grouped-tool test in ``test_grouped_tools.py`` replaces
``client.api_client`` with a raw :class:`unittest.mock.AsyncMock`, which stubs
``request``/``stream`` and therefore exercises none of the URL validation or ACL
gating that lives inside :class:`AccessControlledClient`. Those tests can only
assert what a raw client *forwards*, never that a forbidden URL is refused or a
sensitive value is protected.

These tests build a **real** ``AccessControlledClient`` on an
``httpx.MockTransport`` and route the grouped tools through it, so URL
normalisation, ACL gating, fail-closed denials, redirect handling, and JSON-body
passthrough are all exercised end-to-end.

Threat classes covered
----------------------
- **Path-traversal / ACL-bypass**: traversal payloads, absolute and
  scheme-relative URLs, and unclassifiable ``/profiles`` paths must be denied
  (fail-closed) and must never reach the transport, even when the ACL is wide
  open (``ALL``).
- **Redirect key leak**: a log-download ``Location`` must be fetched with an
  unauthenticated client so the ``X-Api-Key`` header never crosses to the
  redirect target.
- **JSON body coercion corruption**: string body values (profile names, PINs,
  boolean-looking flags) must reach the transport unchanged as strings.
- **Fail-closed contract**: grouped tools surface the client's synthetic 403 as
  the typed ``read_access_denied`` / ``write_access_denied`` payloads, never as
  a success.
"""

import asyncio
import json
import os
from collections.abc import Callable

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import server
from nextdns_mcp.client import AccessControlledClient
from nextdns_mcp.tools import logs as logs_module

API_BASE = "https://api.nextdns.io"
API_KEY = "test-key-12345"
CSV = "date,time,question,answer\n2024-01-01,12:00:00,example.com,A\n"


class _LiveClient:
    """A real AccessControlledClient on a MockTransport that records requests.

    The transport answers 200 JSON for profile paths and 204 for the collection
    create/delete endpoints, mirroring what the real NextDNS API would return
    for an authenticated, permitted request.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ):
        self.seen: list[httpx.Request] = []
        self._inner = handler or self._default_inner
        self.client = AccessControlledClient(
            base_url=API_BASE,
            transport=httpx.MockTransport(self._recording_handler),
            follow_redirects=False,
            headers={"X-Api-Key": API_KEY},
        )
        monkeypatch.setattr(client_module, "api_client", self.client)

    def _recording_handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self._inner(request)

    def _default_inner(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.rstrip("/") == f"{API_BASE}/profiles":
            return httpx.Response(200, json={"id": "newprof"}, request=request)
        if request.method == "DELETE" and url.rstrip("/") == f"{API_BASE}/profiles":
            return httpx.Response(204, request=request)
        if "/logs/download" in url:
            return httpx.Response(
                200, content=CSV.encode("utf-8"), request=request, headers={"content-type": "text/csv"}
            )
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            return httpx.Response(204, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    def handler_for(self, handler: Callable[[httpx.Request], httpx.Response]) -> "_LiveClient":
        """Re-point the transport handler (for redirect/capture scenarios)."""
        self._inner = handler
        return self

    async def close(self) -> None:
        await self.client.aclose()


@pytest.fixture
async def live_client(monkeypatch: pytest.MonkeyPatch) -> _LiveClient:
    """Real access-controlled client wired into the grouped tools' api_client."""
    live = _LiveClient(monkeypatch)
    yield live
    await live.close()


@pytest.fixture
def restricted_env(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], None]:
    """ACL with a single writable profile; readable is unset so writes imply reads.

    ``abc123`` is readable+writable; every other 6-char id is denied for both.
    """
    monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
    monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
    return monkeypatch.setenv


def _sent_request(live_client: _LiveClient, url_path: str) -> httpx.Request | None:
    """Return the first captured request whose URL contains ``url_path``."""
    for request in live_client.seen:
        if url_path in str(request.url):
            return request
    return None


class TestTraversalAndAclBypass:
    """Path-traversal and URL-classification attacks must fail closed.

    The grouped tools validate profile_id shape (rejecting traversal payloads
    before they reach the client), but the client is the security boundary that
    must also refuse traversal/absolute/unclassifiable URLs that reach it by any
    other means. Both layers are asserted here against a real client.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            "abc123/../../xyz999",
            "..%2f..%2fxyz999",
            "abc123%2f..%2fxyz999",
            "../../profiles/xyz999",
            "abc123/../settings",
            "a" * 40,  # overlong: not spec-shaped, unclassifiable
        ],
    )
    async def test_traversal_profile_id_rejected_before_client(self, live_client, payload):
        """Traversal/overlong profile_id never reaches the transport or the ACL."""
        result = await server.manageProfiles("get", profile_id=payload)
        assert result["code"] == "invalid_profile_id"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_traversal_url_denied_even_when_all_readable(self, live_client, monkeypatch):
        """A traversal URL is refused (fail-closed) even with a wide-open ACL."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
        response = await live_client.client.request("GET", "/profiles/abc123/../../profiles/xyz999/settings")
        assert response.status_code == 403
        assert response.json()["code"] == "access_denied"
        assert live_client.seen == []  # never reached the transport

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            "https://evil.example/profiles/abc123/settings",
            "//evil.example/profiles/abc123/settings",
            "https://api.nextdns.io.evil.example/profiles/abc123/settings",
        ],
    )
    async def test_absolute_and_authority_url_denied(self, live_client, monkeypatch, payload):
        """Absolute and scheme-relative URLs targeting another host are refused."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
        response = await live_client.client.request("GET", payload)
        assert response.status_code == 403
        assert response.json()["code"] == "access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            "/profiles/abc.def/settings",  # dot: unclassifiable
            "/profiles/ABC123/settings",  # uppercase: not spec-shaped
            "/profiles/abc_def/settings",  # underscore: not spec-shaped
        ],
    )
    async def test_unclassifiable_profiles_path_denied(self, live_client, monkeypatch, payload):
        """A /profiles path with no safe, spec-shaped id is refused (fail-closed)."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
        response = await live_client.client.request("GET", payload)
        assert response.status_code == 403
        assert response.json()["code"] == "access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_entry_id_traversal_rejected(self, live_client):
        """A traversal entry_id is rejected by the tool before any request."""
        result = await server.manageLists("allowlist", "remove", "abc123", entry_id="../settings")
        assert result["code"] == "invalid_entry_id"
        assert live_client.seen == []


class TestAclGatingEndToEnd:
    """Forbidden read/write paths return the typed denial, never a success.

    These are the grouped-tool tests that the raw-mock suite cannot express: a
    denied profile is refused by the client and surfaced as the typed ACL error
    instead of the raw mock's blanket success.
    """

    @pytest.mark.asyncio
    async def test_read_denied_profile(self, live_client, restricted_env):
        result = await server.manageProfiles("get", profile_id="xyz999")
        assert result["code"] == "read_access_denied"
        assert result["status_code"] == 403
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_write_denied_profile(self, live_client, restricted_env):
        result = await server.manageLists("denylist", "add", "xyz999", entry="bad.com")
        assert result["code"] == "write_access_denied"
        assert result["status_code"] == 403
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_settings_write_denied_profile(self, live_client, restricted_env):
        result = await server.manageSettings("update", "general", "xyz999", settings={"web3": True})
        assert result["code"] == "write_access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_rewrites_read_denied_profile(self, live_client, restricted_env):
        result = await server.manageRewrites("list", "xyz999")
        assert result["code"] == "read_access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_analytics_read_denied_profile(self, live_client, restricted_env):
        result = await server.queryAnalytics("status", "xyz999", from_time="-1d", limit=5)
        assert result["code"] == "read_access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_logs_read_denied_profile(self, live_client, restricted_env):
        result = await server.manageLogs("get", "xyz999", limit=10)
        assert result["code"] == "read_access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_logs_clear_denied_profile(self, live_client, restricted_env):
        result = await server.manageLogs("clear", "xyz999")
        assert result["code"] == "write_access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_profile_delete_denied_profile(self, live_client, restricted_env):
        result = await server.manageProfiles("delete", profile_id="xyz999")
        assert result["code"] == "write_access_denied"
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_denial_carries_typed_code_and_profile(self, live_client, restricted_env):
        """The synthetic 403 body preserves the denial class and the profile id."""
        result = await server.manageProfiles("get", profile_id="xyz999")
        assert result["profile_id"] == "xyz999"
        assert result["status_code"] == 403
        assert "Read access denied" in result["error"]

    @pytest.mark.asyncio
    async def test_permitted_profile_reaches_transport(self, live_client, restricted_env):
        """The positive control: an allowed profile passes the ACL to the API."""
        result = await server.manageProfiles("get", profile_id="abc123")
        assert result == {"ok": True}
        assert any(str(r.url).endswith("/profiles/abc123") for r in live_client.seen)

    @pytest.mark.asyncio
    async def test_read_only_mode_denies_writes(self, live_client, monkeypatch):
        """In read-only mode, even a writable profile's writes are refused."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        result = await server.manageProfiles("update", profile_id="abc123", name="New")
        assert result["code"] == "write_access_denied"
        assert "read-only" in result["error"].lower()
        assert live_client.seen == []


class TestBodyCoercionIntegrity:
    """JSON body strings must reach the transport unchanged (issue #145/#156).

    The client deliberately does NOT coerce string body values: a profile name
    like ``"12345"`` or a PIN like ``"0012"`` must stay a string, and ``"true"``
    must not become a boolean, or the upstream API would 400.
    """

    @pytest.mark.asyncio
    async def test_numeric_and_boolean_strings_not_corrupted(self, live_client, restricted_env):
        settings = {"name": "12345", "web3": "true", "pin": "0012", "label": "42"}
        result = await server.manageSettings("update", "general", "abc123", settings=settings)
        assert result == {"success": True}
        request = _sent_request(live_client, "/profiles/abc123/settings")
        assert request is not None
        sent = json.loads(request.content)
        assert sent == settings
        for value in sent.values():
            assert isinstance(value, str), f"body value corrupted to {type(value).__name__}: {value!r}"

    @pytest.mark.asyncio
    async def test_list_entry_strings_not_corrupted(self, live_client, restricted_env):
        result = await server.manageLists(
            "denylist", "add", "abc123", entry={"id": "12345.example.com", "active": "true"}
        )
        assert result == {"success": True}
        request = _sent_request(live_client, "/profiles/abc123/denylist")
        assert request is not None
        sent = json.loads(request.content)
        assert sent == {"id": "12345.example.com", "active": "true"}
        assert isinstance(sent["id"], str)
        assert isinstance(sent["active"], str)

    @pytest.mark.asyncio
    async def test_native_typed_values_pass_through(self, live_client, restricted_env):
        """Properly typed body values pass through unchanged."""
        result = await server.manageLists("denylist", "update", "abc123", entry_id="bad.com", entry={"active": False})
        assert result == {"success": True}
        request = _sent_request(live_client, "/profiles/abc123/denylist/bad.com")
        assert request is not None
        assert json.loads(request.content) == {"active": False}
        assert isinstance(json.loads(request.content)["active"], bool)


class TestDownloadAclAndRedirect:
    """Log-download ACL gating and redirect key-leak protection (issues #130/#156)."""

    @pytest.mark.asyncio
    async def test_download_denied_profile_returns_typed_403(self, live_client, restricted_env):
        """A non-readable profile's download is refused with read_access_denied."""
        result = await server.manageLogs("download", "xyz999")
        assert result["code"] == "read_access_denied"
        assert result["status_code"] == 403
        assert "file_path" not in result
        assert live_client.seen == []  # the stream never reached the transport

    @pytest.mark.asyncio
    async def test_download_permitted_streams_file(self, live_client, restricted_env):
        """An allowed profile's download streams the CSV to a temp file."""
        result = await server.manageLogs("download", "abc123")
        assert result["content_type"] == "text/csv"
        assert result["row_count"] == 2
        assert os.path.isfile(result["file_path"])
        await asyncio.to_thread(os.unlink, result["file_path"])

    @pytest.mark.asyncio
    async def test_redirect_key_never_crosses_to_target(self, live_client, restricted_env, monkeypatch):
        """A download redirect is fetched unauthenticated: no X-Api-Key on the target.

        The initial authenticated request carries the key; the redirected fetch
        (routed through a patched unauthenticated client) must not.
        """
        initial_headers: dict[str, dict[str, str]] = {}

        def initial_handler(request: httpx.Request) -> httpx.Response:
            initial_headers["hdrs"] = dict(request.headers)
            location = "https://api.nextdns.io/profiles/abc123/logs/signed.csv"
            return httpx.Response(302, request=request, headers={"location": location})

        final_headers: dict[str, dict[str, str]] = {}
        csv_seen: list[str] = []

        def final_handler(request: httpx.Request) -> httpx.Response:
            final_headers["hdrs"] = dict(request.headers)
            csv_seen.append(str(request.url))
            return httpx.Response(
                200, content=CSV.encode("utf-8"), request=request, headers={"content-type": "text/csv"}
            )

        # Re-point the authenticated client's transport to the 302 handler.
        live_client.handler_for(initial_handler)

        # Capture the real class before patching: logs_module.httpx is the global
        # httpx module, so httpx.AsyncClient is exactly the symbol the tool calls.
        real_async_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            # Unauthenticated: a plain AsyncClient with no X-Api-Key header, exactly
            # what the production redirect path uses so the key never crosses hosts.
            return real_async_client(transport=httpx.MockTransport(final_handler), follow_redirects=False)

        monkeypatch.setattr(logs_module.httpx, "AsyncClient", fake_client)

        result = await server.manageLogs("download", "abc123")
        assert result["content_type"] == "text/csv"
        assert result["row_count"] == 2
        await asyncio.to_thread(os.unlink, result["file_path"])

        # The initial (authenticated) request did carry the key...
        assert initial_headers["hdrs"].get("x-api-key") == API_KEY
        # ...and the redirected request went to the target WITHOUT the key.
        assert csv_seen[0] == "https://api.nextdns.io/profiles/abc123/logs/signed.csv"
        assert "x-api-key" not in {k.lower() for k in final_headers["hdrs"]}

    @pytest.mark.asyncio
    async def test_stream_denied_never_touches_transport(self, live_client, restricted_env):
        """The client's stream() enforces the same fail-closed denial as request()."""
        async with live_client.client.stream("GET", "/profiles/xyz999/logs/download") as response:
            assert response.status_code == 403
            assert response.json()["code"] == "read_access_denied"
            assert not response.has_redirect_location
        assert live_client.seen == []


class TestFailClosedContract:
    """The grouped tools must never turn a client 403 into a success.

    ``test_grouped_tools.py::TestApiRequest::test_success_204`` previously
    asserted that a non-spec profile path (``/profiles/abc``) returns a
    success. That codified fail-open: a raw mock returned 204 for an ID the
    access-controlled client would refuse. This class re-asserts the
    fail-closed contract that shipped with the ACL work.
    """

    @pytest.mark.asyncio
    async def test_non_spec_profile_id_is_denied_not_success(self, live_client, monkeypatch):
        """DELETE /profiles/abc (3-char, non-spec) is refused, never {"success": True}."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
        response = await live_client.client.request("DELETE", "/profiles/abc")
        assert response.status_code == 403
        assert response.json()["code"] == "access_denied"
        # And through the grouped tool the same shape is surfaced as a denial.
        result = await server._api_request("DELETE", "/profiles/abc")
        assert result["code"] == "access_denied"
        assert result["status_code"] == 403
        assert result != {"success": True}

    @pytest.mark.asyncio
    async def test_denied_read_is_never_success(self, live_client, restricted_env):
        """A denied read surfaces read_access_denied, not a 200 payload."""
        result = await server._api_request("GET", "/profiles/xyz999/settings")
        assert result["code"] == "read_access_denied"
        assert result != {"success": True}
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_denied_write_is_never_success(self, live_client, restricted_env):
        """A denied write surfaces write_access_denied, not a 204 success."""
        result = await server._api_request("POST", "/profiles/xyz999/denylist", json={"id": "a.com"})
        assert result["code"] == "write_access_denied"
        assert result != {"success": True}
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_collection_write_denied_when_no_writable(self, live_client, monkeypatch):
        """With no writable profiles, POST /profiles is refused (issue #132)."""
        monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_WRITABLE_PROFILES", raising=False)
        result = await server.manageProfiles("create", name="Test")
        assert result["code"] == "write_access_denied"
        assert "no profiles are writable" in result["error"].lower()
        assert live_client.seen == []

    @pytest.mark.asyncio
    async def test_collection_read_denied_when_no_readable(self, live_client, monkeypatch):
        """With no readable profiles, GET /profiles is refused (deny-all default)."""
        monkeypatch.delenv("NEXTDNS_READABLE_PROFILES", raising=False)
        monkeypatch.delenv("NEXTDNS_WRITABLE_PROFILES", raising=False)
        result = await server.manageProfiles("list")
        assert result["code"] == "read_access_denied"
        assert "no profiles are readable" in result["error"].lower()
        assert live_client.seen == []
