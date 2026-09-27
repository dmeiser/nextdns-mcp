"""Regression tests for the client-layer destination allow-list (issue #262).

``AccessControlledClient`` attaches ``X-Api-Key`` as a client-level default
header, so it follows every request to whatever host the request URL names.
These tests pin the contract that a request only ever leaves the process for an
approved NextDNS host (the REST API base, or the DoH host ``dohLookup`` uses),
on both the normal request path and the streaming path, and that a refusal
reaches the transport neither at all nor with the key attached.
"""

from collections.abc import Callable
from typing import ClassVar

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp.client import AccessControlledClient, AccessDeniedError

API_KEY = "test-key-12345"
API_BASE = "https://api.nextdns.io"
DOH_HOST = "https://dns.nextdns.io"


@pytest.fixture(autouse=True)
def acl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny-all ACL environment, so a denial can only come from the checks under test."""
    for var in ("NEXTDNS_READ_ONLY", "NEXTDNS_READABLE_PROFILES", "NEXTDNS_WRITABLE_PROFILES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


@pytest.fixture
def recorder() -> Callable[..., list[httpx.Request]]:
    """Return a factory building a client whose transport records every request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": "success"})

    def build(base_url: str = API_BASE) -> tuple[AccessControlledClient, list[httpx.Request]]:
        client = AccessControlledClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
            headers={"X-Api-Key": API_KEY},
            follow_redirects=False,
        )
        return client, seen

    return build


class TestApprovedDestinationsProceed:
    """A request to an approved host is sent, key attached, exactly as before."""

    async def test_relative_request_to_api_host_proceeds(self, recorder: Callable[..., object]) -> None:
        client, seen = recorder()
        async with client:
            response = await client.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert len(seen) == 1
        assert seen[0].url.host == "api.nextdns.io"
        assert seen[0].headers["X-Api-Key"] == API_KEY

    @pytest.mark.parametrize(
        "url",
        [
            f"{DOH_HOST}/abc123/dns-query",
            "//dns.nextdns.io/abc123/dns-query",
            f"{API_BASE}/profiles/abc123/settings",
            "https://API.NextDNS.IO./profiles/abc123",
        ],
    )
    async def test_caller_supplied_host_is_never_contacted(self, url: str, recorder: Callable[..., object]) -> None:
        """A caller-supplied host is refused even when it names an approved NextDNS host.

        The key follows the request off the client's own base URL, so the only
        destination reachable through this client is the base it was built with;
        every authority-bearing URL is refused before the transport is reached.
        """
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                await client.request("GET", url)

        assert exc_info.value.code == "access_denied"
        assert seen == []

    async def test_stream_to_api_host_proceeds(self, recorder: Callable[..., object]) -> None:
        client, seen = recorder()
        async with client, client.stream("GET", "/profiles/abc123/logs/download") as response:
            await response.aread()

        assert response.status_code == 200
        assert len(seen) == 1
        assert seen[0].url.host == "api.nextdns.io"

    @pytest.mark.parametrize(
        "base_url",
        [
            DOH_HOST,
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://[::1]:8080",
        ],
    )
    async def test_approved_base_url_proceeds(self, base_url: str, recorder: Callable[..., object]) -> None:
        """Every approved host serves a request, including loopback test doubles.

        Loopback is allowed so local test servers keep working; no shipped tool
        ever targets it.
        """
        client, seen = recorder(base_url=base_url)
        async with client:
            response = await client.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert len(seen) == 1

    async def test_configured_base_url_host_is_approved(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Callable[..., object]
    ) -> None:
        """NEXTDNS_BASE_URL stays consistent: pointing it at a proxy widens the allow-list.

        This is the existing base-URL knob, so a self-hosted/proxy deployment
        needs no second configuration mechanism; no new escape hatch is added.
        """
        proxy_base = "https://nextdns.internal.example.com"
        monkeypatch.setattr(client_module, "NEXTDNS_BASE_URL", proxy_base)
        client, seen = recorder(base_url=proxy_base)
        async with client:
            response = await client.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert seen[0].url.host == "nextdns.internal.example.com"


class TestNonApprovedDestinationsRefused:
    """A request to any other host is refused before it reaches the transport."""

    OFFSITE_URLS: ClassVar[list[str]] = [
        "https://evil.example/profiles/abc123/settings",
        "https://api.nextdns.io@evil.example/profiles/abc123/settings",
        "//evil.example/profiles/abc123/settings",
        "http://169.254.169.254/latest/meta-data/",
    ]

    @pytest.mark.parametrize("url", OFFSITE_URLS)
    async def test_request_refused_without_network_call(self, url: str, recorder: Callable[..., object]) -> None:
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                await client.request("GET", url)

        assert exc_info.value.code == "access_denied"
        assert seen == []

    @pytest.mark.parametrize("url", OFFSITE_URLS)
    async def test_stream_refused_without_network_call(self, url: str, recorder: Callable[..., object]) -> None:
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                async with client.stream("GET", url):
                    pass

        assert exc_info.value.code == "access_denied"
        assert seen == []

    async def test_relative_request_refused_when_client_base_url_is_off_site(
        self, recorder: Callable[..., object]
    ) -> None:
        """A client aimed off-site must not carry the key anywhere, not even relatively."""
        client, seen = recorder(base_url="https://evil.example")
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                await client.request("GET", "/profiles/abc123/settings")

        assert exc_info.value.code == "access_denied"
        assert "evil.example" in str(exc_info.value)
        assert seen == []

    async def test_error_text_never_contains_the_key_or_query(self, recorder: Callable[..., object]) -> None:
        """The refusal names the host only: no key, no path, no query string."""
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                await client.request("GET", "https://evil.example/profiles/abc123/settings?token=secret-cursor")

        error = exc_info.value
        assert API_KEY not in str(error)
        assert "secret-cursor" not in str(error)
        assert "/profiles/" not in str(error)
        assert "evil.example" in str(error)
        assert seen == []


class TestStreamFailsClosed:
    """stream() applies the same URL classification as request() (issue #262).

    The four URLs below are exactly the classes request() denies: an
    authority-bearing URL, a scheme-relative URL, a traversal payload, and a
    /profiles path with no safe profile id. stream() used to send all four.
    """

    UNCLASSIFIABLE_URLS: ClassVar[list[str]] = [
        "https://evil.example/profiles/abc123/settings",
        "//evil.example/profiles/abc123/settings",
        "/profiles/abc123/../../profiles/abc123/settings",
        "/profiles/abc.def/settings",
    ]

    @pytest.mark.parametrize("url", UNCLASSIFIABLE_URLS)
    async def test_stream_refuses_unclassifiable_urls(self, url: str, recorder: Callable[..., object]) -> None:
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                async with client.stream("GET", url):
                    pass

        assert exc_info.value.code == "access_denied"
        assert seen == []

    async def test_stream_still_denies_unreadable_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        seen: list[httpx.Request] = []
        client = AccessControlledClient(
            base_url=API_BASE,
            transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),  # type: ignore[func-returns-value]
            headers={"X-Api-Key": API_KEY},
        )
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                async with client.stream("GET", "/profiles/xyz999/logs/download"):
                    pass

        assert exc_info.value.code == "read_access_denied"
        assert seen == []
