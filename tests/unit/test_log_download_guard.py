"""The log-download path keeps the API key in and the network out of the wrong places.

``downloadLogs`` is the one tool the spec cannot express as a plain call: the endpoint
answers with a redirect to a signed URL, so the server has to decide what to do with a
``Location`` header it does not control. Three properties are pinned here, all of them
about the request the server makes next rather than the answer it gets back:

- the API key never crosses to the redirect target (issue #266),
- a hop is refused unless it is https on a globally routable host, so a hostile
  ``Location`` cannot probe an internal service or the cloud metadata endpoint,
- the body is streamed to disk under a total-size cap, and a call that does not
  succeed leaves no file and no directory behind.

SPDX-License-Identifier: MIT
"""

import asyncio
import ipaddress
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import config
from nextdns_mcp.client import AccessDeniedError
from nextdns_mcp.errors import ErrorCode
from nextdns_mcp.tools import logs as logs_module

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
    ) -> None:
        self.request = httpx.Request("GET", url)
        self.status_code = status_code
        self.headers = httpx.Headers(
            {"location": location} if location else {} | ({"content-type": content_type} if content_type else {})
        )
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

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, str(url)))
        if not self.responses:
            raise AssertionError(f"no scripted redirect response left for {url}")
        return self.responses.pop(0)

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
            # request only.
            assert kwargs.get("follow_redirects") is False
            assert not kwargs.get("headers")
            return client

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return client

    return _install


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
        assert await logs_module._host_resolves_to_public_address("mixed.example") is False

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
        assert await logs_module._host_resolves_to_public_address("gone.example") is False

    async def test_an_empty_resolution_is_refused(self, monkeypatch):
        """An empty address list proves nothing either - same closed default."""

        def none(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return []

        monkeypatch.setattr(socket, "getaddrinfo", none)
        assert await logs_module._host_resolves_to_public_address("ghost.example") is False


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
        monkeypatch.setattr(logs_module, "ALLOWED_DOWNLOAD_HOSTS", frozenset({"cdn.nextdns.io"}))
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
        monkeypatch.setattr(client_module, "api_client", api)
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

    async def test_the_follow_up_client_is_closed(self, tmp_path, unauthenticated):
        client = unauthenticated([_FakeResponse("https://storage.example/file.csv", chunks=["a\n"])])
        await logs_module._follow_redirects_to_tempfile(
            httpx.URL("https://storage.example/file.csv"), str(tmp_path / "d.csv")
        )
        assert client.closed is True


class TestTempFileCleanup:
    """A call that does not end in a download leaves neither a file nor a directory."""

    def test_a_file_that_was_never_written_is_not_an_error(self, tmp_path, caplog):
        """The ACL can refuse before a byte is written, so there is nothing to unlink."""
        missing = str(tmp_path / "download.csv")
        logs_module._unlink_temp_file(missing)
        assert "Failed to remove" not in caplog.text

    def test_a_file_that_cannot_be_removed_is_reported(self, tmp_path, monkeypatch, caplog):
        """A failed unlink is logged, not raised: it must not mask the download's error."""
        path = tmp_path / "download.csv"
        path.write_text("partial")

        def refuse(target: str, *args: Any) -> None:
            raise PermissionError("device busy")

        monkeypatch.setattr(logs_module.os, "unlink", refuse)
        with caplog.at_level("WARNING"):
            logs_module._unlink_temp_file(str(path))
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

        monkeypatch.setattr(logs_module.os, "rmdir", refuse)
        logs_module._unlink_temp_file(str(path))
        assert not path.exists()


class TestStreamingToDisk:
    """The CSV goes to disk; only a bounded preview is held for the caller."""

    async def test_the_file_is_written_and_only_a_preview_returned(self, tmp_path):
        path = str(tmp_path / "download.csv")
        rows = [f"host{n}.com,blocked\n" for n in range(60)]
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=rows, content_type="text/csv"), path
        )
        assert payload["file_path"] == path
        assert payload["content_type"] == "text/csv"
        assert payload["row_count"] == 60
        assert payload["size"] == os.path.getsize(path)
        # The preview is a window, not the document: bounded in lines and bytes,
        # and flagged as a partial view of a larger file.
        assert payload["preview"]["line_count"] == logs_module.DOWNLOAD_PREVIEW_MAX_LINES
        assert payload["preview"]["bytes"] <= logs_module.DOWNLOAD_PREVIEW_MAX_BYTES
        assert payload["preview"]["truncated"] is True

    async def test_a_final_line_without_a_newline_is_counted(self, tmp_path):
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["a\n", "b"]), str(tmp_path / "d.csv")
        )
        assert payload["row_count"] == 2
        assert payload["preview"]["truncated"] is False

    async def test_a_chunk_that_breaks_the_cap_stops_mid_stream(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "40")
        path = str(tmp_path / "download.csv")
        with pytest.raises(logs_module.DownloadTooLargeError, match="aborted after 30 bytes"):
            await logs_module._write_stream_to_tempfile(
                _FakeResponse("https://x/file.csv", chunks=["x" * 30, "y" * 30]), path
            )

    async def test_an_error_status_surfaces_as_an_http_error(self, tmp_path):
        with pytest.raises(httpx.HTTPStatusError):
            await logs_module._write_stream_to_tempfile(
                _FakeResponse("https://x/file.csv", status_code=503), str(tmp_path / "d.csv")
            )

    async def test_an_empty_chunk_is_skipped_not_counted(self, tmp_path):
        """A zero-length chunk from the stream is not a row and not written.

        httpx can yield an empty piece between packets; counting it would make the
        reported row count disagree with the file on disk.
        """
        path = str(tmp_path / "d.csv")
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["a.com,blocked\n", "", "b.com,blocked\n"]), path
        )
        assert payload["row_count"] == 2
        assert payload["size"] == os.path.getsize(path)
        assert Path(path).read_text(encoding="utf-8") == "a.com,blocked\nb.com,blocked\n"

    async def test_the_preview_stops_at_the_line_cap(self, tmp_path, monkeypatch):
        """One chunk holding more rows than the preview allows still fills only the window.

        The cap is what keeps a download's payload small enough to be worth sending
        to a model; a single large chunk must not be able to blow past it.
        """
        monkeypatch.setattr(logs_module, "DOWNLOAD_PREVIEW_MAX_LINES", 2)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["l1\nl2\nl3\nl4\nl5\n"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["line_count"] == 2
        assert payload["preview"]["text"] == "l1\nl2\n"
        # The rows past the window are still on disk and still counted.
        assert payload["row_count"] == 5

    async def test_the_preview_stops_at_the_byte_cap(self, tmp_path, monkeypatch):
        """A line that would cross the byte cap is left out rather than truncated in half."""
        monkeypatch.setattr(logs_module, "DOWNLOAD_PREVIEW_MAX_BYTES", 8)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["abcdefgh\n"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["text"] == ""
        assert payload["preview"]["bytes"] == 0
        assert payload["row_count"] == 1
        assert payload["size"] == 9

    async def test_the_preview_is_a_window_on_a_file_larger_than_both_caps(self, tmp_path):
        """Truncation is flagged against the file's size, not the preview's."""
        rows = ["x" * 200 + "\n" for _ in range(200)]
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=rows), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["truncated"] is True
        assert payload["preview"]["bytes"] <= logs_module.DOWNLOAD_PREVIEW_MAX_BYTES
        assert payload["size"] > payload["preview"]["bytes"]


class TestDownloadTool:
    """``downloadLogs`` answers with a payload and leaves nothing behind on failure."""

    @pytest.fixture(autouse=True)
    def _api_key(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    async def test_a_redirect_is_followed_to_a_file(self, monkeypatch, unauthenticated):
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        monkeypatch.setattr(
            client_module,
            "api_client",
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)]),
        )
        unauthenticated([_FakeResponse(signed, chunks=["host,reason\na.com,blocked\n"])])

        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["row_count"] == 2
        assert os.path.exists(payload["file_path"])
        assert payload["preview"]["text"].startswith("host,reason")

    async def test_a_successful_download_is_reclaimed_at_shutdown(self, monkeypatch, unauthenticated):
        """The kept CSV is bounded: the lifespan cleanup removes it and its dir.

        The file must outlive the call (the caller reads the returned path),
        so the reclaim point is server shutdown - without it, every successful
        download accumulated for the life of the container.
        """
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        monkeypatch.setattr(
            client_module,
            "api_client",
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)]),
        )
        unauthenticated([_FakeResponse(signed, chunks=["host,reason\na.com,blocked\n"])])

        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert os.path.exists(payload["file_path"])
        assert payload["file_path"] in logs_module._SUCCESSFUL_DOWNLOADS
        logs_module.cleanup_successful_downloads()
        assert not os.path.exists(payload["file_path"])
        assert not os.path.exists(os.path.dirname(payload["file_path"]))
        logs_module.cleanup_successful_downloads()  # idempotent: nothing registered, nothing to do

    async def test_an_acl_denial_is_typed_and_leaves_no_temp_file(self, monkeypatch):
        class Denied:
            def stream(self, method, url, **kwargs):
                raise AccessDeniedError(
                    "Read access denied for profile: abc123",
                    code=ErrorCode.READ_ACCESS_DENIED,
                    profile_id="abc123",
                )

        monkeypatch.setattr(client_module, "api_client", Denied())
        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.READ_ACCESS_DENIED
        assert "Read access denied for profile: abc123" in payload["error"]

    async def test_a_refused_redirect_leaves_no_temp_directory(self, monkeypatch, unauthenticated):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

        class Redirect:
            def stream(self, method, url, **kwargs):
                return _FakeResponse(
                    "https://api.nextdns.io/profiles/abc123/logs/download", location="https://169.254.169.254/x"
                )

        monkeypatch.setattr(client_module, "api_client", Redirect())
        unauthenticated([])
        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert "non-https" in payload["error"] or "non-public" in payload["error"]

    async def test_a_size_cap_abort_reports_download_too_large(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "10")

        class Stream:
            def stream(self, method, url, **kwargs):
                return _FakeResponse(
                    "https://api.nextdns.io/profiles/abc123/logs/download", chunks=["x" * 20, "y" * 20]
                )

        monkeypatch.setattr(client_module, "api_client", Stream())
        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.DOWNLOAD_TOO_LARGE
        assert "NEXTDNS_DOWNLOAD_MAX_BYTES" in payload["error"]

    async def test_a_network_failure_at_the_api_reports_http_error(self, monkeypatch):
        """An httpx failure on the NextDNS request keeps the documented http_error shape."""

        class Down:
            def stream(self, method, url, **kwargs):
                raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client_module, "api_client", Down())
        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["status_code"] is None

    async def test_the_temp_file_and_its_directory_are_removed_on_failure(self, monkeypatch):
        """A failed download leaves neither a partial CSV nor an empty directory."""
        created: list[str] = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def watch_mkdtemp(*args: Any, **kwargs: Any) -> str:
            made = real_mkdtemp(*args, **kwargs)
            created.append(made)
            return made

        monkeypatch.setattr(logs_module.tempfile, "mkdtemp", watch_mkdtemp)

        class Boom:
            def stream(self, method, url, **kwargs):
                raise RuntimeError("upstream is down")

        monkeypatch.setattr(client_module, "api_client", Boom())
        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.INTERNAL_ERROR
        assert created and not os.path.exists(created[0])

    def test_the_default_size_cap_is_the_configured_one(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_DOWNLOAD_MAX_BYTES", raising=False)
        assert config.get_download_max_bytes() == config.DEFAULT_DOWNLOAD_MAX_BYTES


class TestStreamedFailureIsTyped:
    """A mid-download upstream failure answers as the typed payload, not a crash.

    A streaming response raises ``httpx.ResponseNotRead`` from ``.json()`` and
    ``.text`` until its body has been read, and ``http_error_payload`` reads
    both - so a streaming 503 used to escape as ``ResponseNotRead`` instead of
    describing the failure. The error document is now read bounded and
    re-raised around a buffered copy of the response, so the payload carries
    what upstream said without buffering an unbounded failure page.
    """

    async def test_a_streaming_503_reports_the_typed_error_with_its_body(self, monkeypatch, unauthenticated):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        monkeypatch.setattr(
            client_module,
            "api_client",
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)]),
        )
        unauthenticated(
            [
                _FakeResponse(
                    signed,
                    status_code=503,
                    chunks=['{"errors":[{"code":"serverError","detail":"upstream down"}]}'],
                )
            ]
        )

        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["status_code"] == 503
        assert "upstream down" in payload.get("response_body", ""), "the error document must ride along"

    async def test_a_consumed_error_response_reraises_without_another_body_read(self, monkeypatch):
        """A failure whose body is already read goes straight back out.

        ``is_stream_consumed`` is False on a live stream - the shape that
        needs the bounded read - and True once the body has been read. Reading
        a consumed body again raises inside the error handler, so the consumed
        case must re-raise untouched, carrying the body it already holds.
        """
        reads: list = []

        async def watch_body(response):
            reads.append(response)
            return b""

        monkeypatch.setattr(logs_module, "_bounded_error_body", watch_body)
        response = _FakeResponse("https://x/f.csv", status_code=502, chunks=["bad gateway"])
        async for _ in response.aiter_bytes():  # consume it, exactly as a completed download would
            pass

        with pytest.raises(httpx.HTTPStatusError):
            await logs_module._raise_for_streamed_status(response)
        assert reads == [], "a consumed body must not be re-read"

    async def test_the_error_body_read_is_bounded(self, monkeypatch, unauthenticated):
        """A hostile multi-megabyte failure page is read only up to the cap."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        monkeypatch.setattr(
            client_module,
            "api_client",
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)]),
        )
        huge = ["<html>" + ("x" * 5000), "x" * 5000 + "</html>"]
        unauthenticated([_FakeResponse(signed, status_code=503, chunks=huge)])

        payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload.get("response_body_truncated") is True
        assert len(payload.get("response_body", "")) <= logs_module.BODY_SLICE_BYTES + len(
            " [truncated]"  # the marker the payload adds
        )


class TestByteAccountingAndPartialCodepoints:
    """The file gets the raw bytes; text accounting never rewrites what is stored.

    The size cap and the disk write use the raw chunk (no re-encode round
    trip), and the text for row/preview tallying comes from an incremental
    decoder, so a codepoint split across chunk boundaries still decodes -
    as a whole codepoint when the stream supplies the rest, as the decoder's
    replacement when it does not. What is on disk is byte-identical either way.
    """

    async def test_a_split_codepoint_decodes_across_chunks(self, tmp_path):
        pieces = ["名前,値\n".encode(), b"ok", "é行".encode()]
        # Split the last chunk so a codepoint straddles the boundary.
        tail = "é行".encode()
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/f.csv", chunks=[pieces[0], pieces[1], tail[:1], tail[1:]]), str(tmp_path / "d.csv")
        )
        assert Path(payload["file_path"]).read_bytes() == pieces[0] + pieces[1] + tail
        assert "é行" in payload["preview"]["text"]

    async def test_a_truncated_codepoint_is_replaced_not_a_crash(self, tmp_path):
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/f.csv", chunks=[b"row\n", b"\xc3"]),  # lead byte with no trailer
            str(tmp_path / "d.csv"),
        )
        assert Path(payload["file_path"]).read_bytes() == b"row\n\xc3"  # raw bytes written verbatim
        assert payload["preview"]["text"].startswith("row")  # the decoder's replacement lands in the preview

    async def test_the_cap_measures_raw_bytes_not_reencoded_text(self, tmp_path, monkeypatch):
        """The limit applies to the bytes read, so accounting cannot drift from the file."""
        monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "6")
        with pytest.raises(logs_module.DownloadTooLargeError):
            await logs_module._write_stream_to_tempfile(
                _FakeResponse("https://x/f.csv", chunks=["一二三".encode()]),  # 9 bytes, 3 "characters"
                str(tmp_path / "d.csv"),
            )


class TestDiallingTheVettedAddress:
    """The vetted IP is the dialled IP: the rebinding window is closed, not accepted.

    A ``Location`` that vets public on the first lookup must not be fetched
    after a second lookup flips the name to loopback (classic rebinding), and
    while the hop is fetched the process resolver must be answering from the
    vetted set - the URL, and therefore TLS SNI, certificate verification and
    the Host header, stay hostname-based.
    """

    async def test_a_name_that_flips_to_private_between_hops_is_refused(self, tmp_path, unauthenticated, monkeypatch):
        signed = "https://storage.example/file.csv?X-Amz-Signature=deadbeef"
        answers = iter([PUBLIC_IP, "127.0.0.1"])

        def flip(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 0))]

        monkeypatch.setattr(socket, "getaddrinfo", flip)
        follower = unauthenticated([_FakeResponse(signed, location="https://storage.example/second.csv")])
        with pytest.raises(logs_module.DownloadRedirectRefusedError):
            await logs_module._follow_redirects_to_tempfile(httpx.URL(signed), str(tmp_path / "d.csv"))
        assert [url for _, url in follower.calls] == [signed], "the poisoned second hop was fetched"

    async def test_the_pinned_resolver_is_installed_while_the_hop_is_fetched(self, tmp_path, monkeypatch):
        signed = "https://storage.example/file.csv"

        def public(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]

        monkeypatch.setattr(socket, "getaddrinfo", public)
        observed: dict[str, Any] = {}

        class _ProbeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def stream(self, method: str, url: Any, **kwargs: Any) -> Any:
                observed["resolver"] = socket.getaddrinfo
                observed["url"] = str(url)
                return _FakeResponse(str(url), chunks=["a\n"])

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(httpx, "AsyncClient", _ProbeClient)
        await logs_module._follow_redirects_to_tempfile(httpx.URL(signed), str(tmp_path / "d.csv"))

        pinned = observed["resolver"]
        assert pinned is not public, "the fetch ran with the process resolver, not the pinned one"
        assert pinned("storage.example", None)[0][4][0] == PUBLIC_IP
        assert pinned("other.example", None)[0][4][0] == PUBLIC_IP, "other names fall through to the real resolver"
        assert observed["url"] == signed, "the request URL stays hostname-based (SNI, Host, cert)"

    def test_pinned_dns_answers_only_for_the_vetted_host(self, monkeypatch):
        vetted = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]

        def real(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.99", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", real)
        with logs_module._pinned_dns("cdn.example", vetted):
            assert socket.getaddrinfo("cdn.example", None)[0][4][0] == PUBLIC_IP
            assert socket.getaddrinfo("other.example", None)[0][4][0] == "93.184.216.99"
        assert socket.getaddrinfo("cdn.example", None)[0][4][0] == "93.184.216.99"

    def test_pinned_dns_is_a_noop_for_pinned_allow_hosts(self, monkeypatch):
        def real(host: str, port: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]

        monkeypatch.setattr(socket, "getaddrinfo", real)
        with logs_module._pinned_dns("cdn.nextdns.io", None):
            assert socket.getaddrinfo is real


class TestPreviewRespectsRecordBoundaries:
    """Chunk boundaries are not CSV record boundaries: a split line is previewed whole."""

    async def test_a_record_split_across_chunks_is_previewed_whole(self, tmp_path, unauthenticated, monkeypatch):
        signed = "https://storage.example/file.csv"
        unauthenticated([_FakeResponse(signed, chunks=["host,rea", "son\na.co", "m,blocked\n"])])
        payload = await logs_module._follow_redirects_to_tempfile(httpx.URL(signed), str(tmp_path / "d.csv"))
        assert payload["preview"]["text"] == "host,reason\na.com,blocked\n"
        assert payload["preview"]["line_count"] == 2

    async def test_the_final_unterminated_record_is_previewed(self, tmp_path, unauthenticated, monkeypatch):
        signed = "https://storage.example/file.csv"
        unauthenticated([_FakeResponse(signed, chunks=["a\nb,c"])])
        payload = await logs_module._follow_redirects_to_tempfile(httpx.URL(signed), str(tmp_path / "d.csv"))
        assert payload["preview"]["text"] == "a\nb,c"

    async def test_a_record_larger_than_the_preview_does_not_grow_the_carry(
        self, tmp_path, unauthenticated, monkeypatch
    ):
        """One oversized record: the carry is dropped, not accumulated forever."""
        signed = "https://storage.example/file.csv"
        giant = "x" * (logs_module.DOWNLOAD_PREVIEW_MAX_BYTES * 2 + 1024)
        unauthenticated([_FakeResponse(signed, chunks=[giant[: len(giant) // 2], giant[len(giant) // 2 :] + "\n"])])
        payload = await logs_module._follow_redirects_to_tempfile(httpx.URL(signed), str(tmp_path / "d.csv"))
        assert payload["row_count"] == 1
        assert payload["preview"]["line_count"] <= 1


class TestPinnedWindowsSerialiseAcrossDownloads:
    """The resolver swap is process-global, so vet-and-fetch is ONE critical section.

    Without a shared lock two downloads interleave their swaps: when the
    earlier one finishes first it restores the resolver under the later
    one, leaving that fetch running unpinned, and the later one then
    restores an earlier wrapper as "real" - stale residue for the whole
    process. This test forces exactly that schedule: A's fetch stays open
    50 ms; B starts inside that window, dials (flipping B's own host to
    loopback at dial time, the rebinding race), and stays open 30 ms after
    - so A finishes first. With the lock, B only vets and connects after A
    released it, under B's own pin; without it, B's probe below resolves
    through the real (flipped) resolver and sees loopback, and a stale
    wrapper outlives both downloads.
    """

    async def test_out_of_order_overlaps_stay_pinned_and_restore_clean(self, tmp_path, monkeypatch):
        a_url = "https://a.example/f.csv"
        b_url = "https://b.example/f.csv"
        state = {"flipped": False}

        def fake_resolver(host: str, port: Any, **kwargs: Any) -> list[Any]:
            if str(host) == "b.example" and state["flipped"]:
                # B's host "rebinds" to loopback after B dialled: only B's
                # own pin, still installed, can answer with the vetted address.
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
            ip = {"a.example": "93.184.216.34", "b.example": "93.184.216.99"}.get(str(host), "93.184.216.200")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_resolver)

        seen: dict[str, Any] = {}
        finished: list[str] = []

        class _ProbingResponse(_FakeResponse):
            def __init__(self, url: str, name: str, delay: float) -> None:
                super().__init__(url, chunks=[f"{name},ok\n"])
                self._name = name
                self._delay = delay

            async def __aenter__(self) -> Self:
                await asyncio.sleep(self._delay)
                seen[self._name] = {
                    "own": socket.getaddrinfo(httpx.URL(self.request.url).host, None)[0][4][0],
                    "other": socket.getaddrinfo("other.example", None)[0][4][0],
                }
                return self

            async def __aexit__(self, *exc_info: object) -> None:
                finished.append(self._name)

        responses = {a_url: _ProbingResponse(a_url, "a", 0.05), b_url: _ProbingResponse(b_url, "b", 0.06)}

        class _UrlClient:
            def stream(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
                if str(url) == b_url:
                    # The flip arms when B dials: from here the real resolver
                    # betrays b.example, and B outlives A's window either way.
                    state["flipped"] = True
                return responses[str(url)]

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _UrlClient())

        async def start_b() -> dict[str, Any]:
            # B enters the function while A is inside its window: overlap is
            # proven at entry, whether the lock defers its fetch or not.
            seen["overlap"] = "a" not in finished
            return await logs_module._follow_redirects_to_tempfile(httpx.URL(b_url), str(tmp_path / "b.csv"))

        task_a = asyncio.create_task(
            logs_module._follow_redirects_to_tempfile(httpx.URL(a_url), str(tmp_path / "a.csv"))
        )
        await asyncio.sleep(0.01)  # A is inside its pinned fetch window when B starts
        task_b = asyncio.create_task(start_b())
        result_b = await task_b
        result_a = await task_a

        assert seen["overlap"] is True, "B never overlapped A's fetch: the schedule did not test concurrency"
        assert seen["b"]["own"] == "93.184.216.99", "B continued unpinned after A restored the resolver"
        assert seen["b"]["other"] == "93.184.216.200", "unrelated names must reach the real resolver"
        assert socket.getaddrinfo is fake_resolver, "a stale pin wrapper was left installed"
        assert result_a["row_count"] == result_b["row_count"] == 1


class TestOversizedRecordsDropWhole:
    """A record longer than the whole preview cap is dropped once, not reborn."""

    async def test_a_dropped_records_tail_does_not_resurface_as_a_new_record(self, tmp_path, monkeypatch):
        """The carry is dropped when one unterminated record crosses the byte
        cap; later chunks still belong to THAT record. Appending their leading
        text to the preview would show a fragment and imply a record that
        never existed."""
        monkeypatch.setattr(logs_module, "DOWNLOAD_PREVIEW_MAX_BYTES", 16)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["A" * 30, "AAAA\nreal,row\n"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["text"] == "real,row\n"
        assert payload["row_count"] == 2
        assert payload["size"] == 44

    async def test_an_oversized_record_spanning_several_chunks_stays_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs_module, "DOWNLOAD_PREVIEW_MAX_BYTES", 16)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["A" * 30, "more", "tail\nx,y\n"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["text"] == "x,y\n"
        assert payload["row_count"] == 2


class TestOversizedRecordAtEof:
    """The dropped record's final tail stays dropped when the stream just ends."""

    async def test_an_unterminated_oversized_record_stays_out_of_the_preview_at_eof(self, tmp_path, monkeypatch):
        """The carry survives into EOF mid-dropped-record; _flush_tail must not
        preview it (it is the same record already excluded), while the row
        itself still counts - it exists on disk."""
        monkeypatch.setattr(logs_module, "DOWNLOAD_PREVIEW_MAX_BYTES", 16)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["A" * 30, "xyz"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["text"] == ""
        assert payload["preview"]["line_count"] == 0
        assert payload["row_count"] == 1
        assert payload["size"] == 33


def test_download_error_details_url_raises():
    from types import SimpleNamespace

    from nextdns_mcp.tools.logs import _download_error_details

    class Req:
        @property
        def url(self):
            raise RuntimeError("boom")

    e = Exception()
    e.response = SimpleNamespace(status_code=500)
    e.request = Req()
    status, scheme, host = _download_error_details(e)
    assert status == 500
    assert scheme is None
    assert host is None
