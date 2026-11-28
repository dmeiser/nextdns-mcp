"""Regression tests for the client-layer destination allow-list (issue #262).

``AccessControlledClient`` attaches ``X-Api-Key`` as a client-level default
header, so it follows every request to whatever host the request URL names.
These tests pin the contract that a request only ever leaves the process for a
host on the client's allow-list (the REST API base, the NextDNS DoH host, the
host of the API base the client was created on, and loopback test doubles), on
both the normal request path and the streaming path, and that a refusal reaches
the transport neither at all nor with the key attached.
"""

from collections.abc import Callable
from typing import ClassVar

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp.client import AccessControlledClient, AccessDeniedError, RawProbeTransport

API_KEY = "test-key-12345"
API_BASE = "https://api.nextdns.io"
DOH_HOST = "https://dns.nextdns.io"

Recorder = Callable[..., tuple[AccessControlledClient, list[httpx.Request]]]


@pytest.fixture(autouse=True)
def acl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fully-permissive ACL environment, so a denial can only come from the checks under test."""
    for var in ("NEXTDNS_READ_ONLY", "NEXTDNS_READABLE_PROFILES", "NEXTDNS_WRITABLE_PROFILES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


@pytest.fixture
def recorder() -> Recorder:
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

    async def test_relative_request_to_api_host_proceeds(self, recorder: Recorder) -> None:
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
    async def test_caller_supplied_host_is_never_contacted(self, url: str, recorder: Recorder) -> None:
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

    async def test_stream_to_api_host_proceeds(self, recorder: Recorder) -> None:
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
    async def test_approved_base_url_proceeds(self, base_url: str, recorder: Recorder) -> None:
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
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """The configured API base stays consistent: pointing it at a proxy widens the allow-list.

        NEXTDNS_API_BASE is the existing base-URL knob, so a self-hosted/proxy
        deployment needs no second configuration mechanism; no new escape
        hatch is added.
        """
        proxy_base = "https://nextdns.internal.example.com"
        monkeypatch.setenv("NEXTDNS_API_BASE", proxy_base)
        client, seen = recorder(base_url=proxy_base)
        async with client:
            response = await client.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert seen[0].url.host == "nextdns.internal.example.com"

    async def test_retained_client_keeps_its_own_base_host_approved(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """A client kept alive across a NEXTDNS_API_BASE change still reaches its own base host.

        The environment is resolved once per client, at creation, and the
        destination allowance is that same base. Re-reading it per request
        would move the allowance to the new host while the retained client was
        still sending relative requests to the old one, and those requests
        would fail closed in the window before the client is reset.
        """
        proxy_a = "https://proxy-a.internal.example.com"
        monkeypatch.setenv("NEXTDNS_API_BASE", proxy_a)
        client, seen = recorder(base_url=proxy_a)
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://proxy-b.internal.example.com")
        async with client:
            response = await client.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert seen[0].url.host == "proxy-a.internal.example.com"

    async def test_new_host_approved_only_after_a_client_is_built_on_it(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """The mirror of the previous test: the new host is approved for a client created after the change.

        Together the pair pins that base and allowance move one client at a
        time, so the guard never permits a host no live client is aimed at.
        """
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://proxy-a.internal.example.com")
        stale, seen = recorder(base_url="https://proxy-a.internal.example.com")
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://proxy-b.internal.example.com")

        async with stale:
            with pytest.raises(AccessDeniedError) as exc_info:
                await stale.request("GET", "https://proxy-b.internal.example.com/profiles/abc123/settings")

        assert str(exc_info.value) == "Blocked request to non-NextDNS host: proxy-b.internal.example.com"
        assert seen == []

        fresh, fresh_seen = recorder(base_url="https://proxy-b.internal.example.com")
        async with fresh:
            response = await fresh.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert fresh_seen[0].url.host == "proxy-b.internal.example.com"


class TestAllowListFollowsTheCreationTimeBase:
    """The configured entry is the base captured when the client was created, never a later env read."""

    def test_configured_base_is_the_env_value_as_of_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://proxy-a.internal.example.com/")
        assert client_module._configured_destination_base() == "https://proxy-a.internal.example.com"

    def test_configured_base_of_a_rejected_value_adds_no_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A value the getter refuses must not widen the allow-list (fail closed)."""
        monkeypatch.setenv("NEXTDNS_API_BASE", "http://plain.internal.example.com")
        configured = client_module._configured_destination_base()

        assert configured == ""
        assert client_module._allowed_destination_hosts(configured) == (
            client_module.ALLOWED_DESTINATION_HOSTS | client_module.LOOPBACK_HOSTS
        )

    def test_allow_list_ignores_an_env_change_after_the_base_was_taken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://proxy-b.internal.example.com")
        hosts = client_module._allowed_destination_hosts("https://proxy-a.internal.example.com")

        assert hosts == client_module.ALLOWED_DESTINATION_HOSTS | client_module.LOOPBACK_HOSTS | {
            "proxy-a.internal.example.com"
        }

    def test_allow_list_without_a_configured_base_holds_only_the_built_ins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No configured base means no extra entry, and no empty-string entry."""
        monkeypatch.setenv("NEXTDNS_API_BASE", "https://proxy-b.internal.example.com")
        hosts = client_module._allowed_destination_hosts()

        assert hosts == client_module.ALLOWED_DESTINATION_HOSTS | client_module.LOOPBACK_HOSTS


class TestFactoryResolvesOneBasePerClient:
    """One configuration read per client: the aim and the allowance cannot be two different bases."""

    @staticmethod
    def _flaky_getter() -> str:
        """A getter that moves between two hosts, standing in for a config reload."""
        TestFactoryResolvesOneBasePerClient.reads += 1
        return (
            "https://proxy-a.internal.example.com"
            if TestFactoryResolvesOneBasePerClient.reads == 1
            else "https://proxy-b.internal.example.com"
        )

    reads: ClassVar[int] = 0

    def test_factory_reads_the_base_once_and_passes_it_to_both_uses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        type(self).reads = 0
        monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)
        monkeypatch.setattr(client_module, "get_api_base", self._flaky_getter)

        client = client_module.create_nextdns_client()

        assert str(client.base_url) == "https://proxy-a.internal.example.com"
        assert client._configured_base == "https://proxy-a.internal.example.com"
        assert type(self).reads == 1

    async def test_relative_request_through_that_client_passes_its_own_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA's probe: a base that moves mid-creation must not leave the client's own host refused.

        The factory's kwargs are handed to the constructor exactly as the
        factory builds them, and the request goes through a recording transport,
        so this fails (AccessDeniedError, zero transport calls) if the allowance
        ever resolves to a second, different read of the setting.
        """
        type(self).reads = 0
        monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)
        monkeypatch.setattr(client_module, "get_api_base", self._flaky_getter)

        seen: list[httpx.Request] = []
        client = AccessControlledClient(
            transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),  # type: ignore[func-returns-value]
            **client_module._authenticated_client_kwargs(),
        )
        async with client:
            response = await client.request("GET", "/profiles/abc123/settings")

        assert response.status_code == 200
        assert [r.url.host for r in seen] == ["proxy-a.internal.example.com"]

    async def test_probe_client_shares_the_same_single_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        type(self).reads = 0
        monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)
        monkeypatch.setattr(client_module, "get_api_base", self._flaky_getter)

        probe = client_module.create_raw_probe_client()

        assert str(probe.base_url) == "https://proxy-a.internal.example.com"
        assert probe._configured_base == "https://proxy-a.internal.example.com"
        await probe.aclose()


class TestNonApprovedDestinationsRefused:
    """A request to any other host is refused before it reaches the transport."""

    OFFSITE_URLS: ClassVar[list[str]] = [
        "https://evil.example/profiles/abc123/settings",
        "https://api.nextdns.io@evil.example/profiles/abc123/settings",
        "//evil.example/profiles/abc123/settings",
        "http://169.254.169.254/latest/meta-data/",
    ]

    @pytest.mark.parametrize("url", OFFSITE_URLS)
    async def test_request_refused_without_network_call(self, url: str, recorder: Recorder) -> None:
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                await client.request("GET", url)

        assert exc_info.value.code == "access_denied"
        assert seen == []

    @pytest.mark.parametrize("url", OFFSITE_URLS)
    async def test_stream_refused_without_network_call(self, url: str, recorder: Recorder) -> None:
        client, seen = recorder()
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                async with client.stream("GET", url):
                    pass

        assert exc_info.value.code == "access_denied"
        assert seen == []

    async def test_relative_request_refused_when_client_base_url_is_off_site(self, recorder: Recorder) -> None:
        """A client aimed off-site must not carry the key anywhere, not even relatively."""
        client, seen = recorder(base_url="https://evil.example")
        async with client:
            with pytest.raises(AccessDeniedError) as exc_info:
                await client.request("GET", "/profiles/abc123/settings")

        assert exc_info.value.code == "access_denied"
        assert "evil.example" in str(exc_info.value)
        assert seen == []

    async def test_error_text_never_contains_the_key_or_query(self, recorder: Recorder) -> None:
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


class TestRawPathKeepsTheDestinationAllowList:
    """The probe transport skips the profile ACL, never the destination allow-list.

    RawProbeTransport is the ACL-free path used by the /health readiness
    probe, but it carries the same X-Api-Key client header as every other
    path, so a caller-supplied off-site URL must still be refused there.
    """

    async def test_raw_request_to_off_site_host_is_refused(self) -> None:
        seen: list[httpx.Request] = []
        raw = RawProbeTransport(
            base_url=API_BASE,
            transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),  # type: ignore[func-returns-value]
            headers={"X-Api-Key": API_KEY},
        )
        async with raw:
            with pytest.raises(AccessDeniedError) as exc_info:
                await raw.request("GET", "https://evil.example/profiles/abc123/settings")

        assert exc_info.value.code == "access_denied"
        assert seen == []

    async def test_raw_request_to_approved_host_still_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe path is unaffected: a deny-all ACL still lets it reach the base host."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        seen: list[httpx.Request] = []
        raw = RawProbeTransport(
            base_url=API_BASE,
            transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),  # type: ignore[func-returns-value]
            headers={"X-Api-Key": API_KEY},
        )
        async with raw:
            response = await raw.request("GET", "/health")

        assert response.status_code == 200
        assert seen[0].url.host == "api.nextdns.io"


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
    async def test_stream_refuses_unclassifiable_urls(self, url: str, recorder: Recorder) -> None:
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
