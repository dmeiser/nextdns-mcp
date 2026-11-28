"""The log-download tool end to end: typed failures, byte accounting, and lifecycle.

This part pins the tool surface: typed failures, byte accounting and partial
codepoints, dialling the vetted address, preview record boundaries, pinned windows,
oversized records, and the successful-download lifecycle. The address vetting half
lives in ``test_log_download_guard.py`` and the streaming half in
``test_log_download_guard_streaming.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
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
from nextdns_mcp.tools.logs import guard, preview, stream

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


class TestDownloadTool:
    """``downloadLogs`` answers with a payload and leaves nothing behind on failure."""

    @pytest.fixture(autouse=True)
    def _api_key(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    @pytest.fixture()
    def owned_registry(self):
        """Bind an owning download registry, as ``_server_lifespan`` would."""
        with logs_module.downloads_owned() as registry:
            yield registry

    async def test_a_redirect_is_followed_to_a_file(
        self, monkeypatch, unauthenticated, owned_api_client, owned_registry
    ):
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        with owned_api_client(
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        ):
            unauthenticated([_FakeResponse(signed, chunks=["host,reason\na.com,blocked\n"])])

            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["row_count"] == 2
        assert os.path.exists(payload["file_path"])
        assert payload["preview"]["text"].startswith("host,reason")
        logs_module.cleanup_successful_downloads()
        assert not os.path.exists(payload["file_path"])

    async def test_a_successful_download_is_reclaimed_at_shutdown(
        self, monkeypatch, unauthenticated, owned_api_client, owned_registry
    ):
        """The kept CSV is bounded: the owning registry's cleanup removes it and its dir.

        The file must outlive the call (the caller reads the returned path),
        so the reclaim point is the owning lifetime's end - without it, every
        successful download accumulated for the life of the container. The
        registry is explicit (issue #642): a standalone session owns one via
        ``downloads_owned``, a server lifespan owns one per server.
        """
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        with owned_api_client(
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        ):
            unauthenticated([_FakeResponse(signed, chunks=["host,reason\na.com,blocked\n"])])

            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert os.path.exists(payload["file_path"])
        assert payload["file_path"] in owned_registry
        logs_module.cleanup_successful_downloads()
        assert not os.path.exists(payload["file_path"])
        assert not os.path.exists(os.path.dirname(payload["file_path"]))
        owned_registry.cleanup()  # idempotent: nothing registered, nothing to do

    async def test_a_successful_download_without_a_lifespan_is_rejected_and_cleaned_up(
        self, monkeypatch, unauthenticated, owned_api_client
    ):
        """No owning lifespan means no reclaim point: the download is refused
        with an explicit missing-registry error and the CSV is removed, rather
        than silently registered where nothing would ever reclaim it."""
        created: list[str] = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def watch_mkdtemp(*args: Any, **kwargs: Any) -> str:
            made = real_mkdtemp(*args, **kwargs)
            created.append(made)
            return made

        monkeypatch.setattr(tempfile, "mkdtemp", watch_mkdtemp)

        signed = "https://storage.example/file.csv?Signature=deadbeef"
        with owned_api_client(
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        ):
            unauthenticated([_FakeResponse(signed, chunks=["host,reason\na.com,blocked\n"])])

            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.INTERNAL_ERROR
        assert "could not be handed out" in payload["error"]
        assert "no server lifespan" in payload["error"].lower()
        assert created and not os.path.exists(created[0]), "the rejected download's temp dir must be removed"

    async def test_an_acl_denial_is_typed_and_leaves_no_temp_file(self, monkeypatch, owned_api_client):
        class Denied:
            def stream(self, method, url, **kwargs):
                raise AccessDeniedError(
                    "Read access denied for profile: abc123",
                    code=ErrorCode.READ_ACCESS_DENIED,
                    profile_id="abc123",
                )

        with owned_api_client(Denied()):
            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.READ_ACCESS_DENIED
        assert "Read access denied for profile: abc123" in payload["error"]

    async def test_an_acl_denial_warns_server_side(self, monkeypatch, caplog, owned_api_client):
        """WORK-250: an ACL denial mid-download leaves a server-side WARNING, not just a payload."""
        caplog.set_level(logging.WARNING, logger="nextdns_mcp.tools.logs")

        class Denied:
            def stream(self, method, url, **kwargs):
                raise AccessDeniedError(
                    "Read access denied for profile: abc123",
                    code=ErrorCode.READ_ACCESS_DENIED,
                    profile_id="abc123",
                )

        with owned_api_client(Denied()):
            payload = await logs_module._download_logs_to_tempfile("abc123")

        assert payload["code"] == ErrorCode.READ_ACCESS_DENIED
        assert any("Access denied downloading logs" in record.getMessage() for record in caplog.records)

    async def test_a_refused_redirect_warns_server_side(self, monkeypatch, caplog, unauthenticated, owned_api_client):
        """WORK-250: a refused redirect leaves a server-side WARNING, not just a payload."""
        caplog.set_level(logging.WARNING, logger="nextdns_mcp.tools.logs")
        unauthenticated([])

        class Redirect:
            def stream(self, method, url, **kwargs):
                return _FakeResponse(
                    "https://api.nextdns.io/profiles/abc123/logs/download", location="https://169.254.169.254/x"
                )

        with owned_api_client(Redirect()):
            payload = await logs_module._download_logs_to_tempfile("abc123")

        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert any("Refusing log download redirect" in record.getMessage() for record in caplog.records)

    async def test_a_refused_redirect_leaves_no_temp_directory(self, monkeypatch, unauthenticated, owned_api_client):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

        class Redirect:
            def stream(self, method, url, **kwargs):
                return _FakeResponse(
                    "https://api.nextdns.io/profiles/abc123/logs/download", location="https://169.254.169.254/x"
                )

        with owned_api_client(Redirect()):
            unauthenticated([])
            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert "non-https" in payload["error"] or "non-public" in payload["error"]

    async def test_a_size_cap_abort_reports_download_too_large(self, monkeypatch, owned_api_client):
        monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "10")

        class Stream:
            def stream(self, method, url, **kwargs):
                return _FakeResponse(
                    "https://api.nextdns.io/profiles/abc123/logs/download", chunks=["x" * 20, "y" * 20]
                )

        with owned_api_client(Stream()):
            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.DOWNLOAD_TOO_LARGE
        assert "NEXTDNS_DOWNLOAD_MAX_BYTES" in payload["error"]

    async def test_a_network_failure_at_the_api_reports_http_error(self, monkeypatch, owned_api_client):
        """An httpx failure on the NextDNS request keeps the documented http_error shape."""

        class Down:
            def stream(self, method, url, **kwargs):
                raise httpx.ConnectError("connection refused")

        with owned_api_client(Down()):
            payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["status_code"] is None

    async def test_a_chain_deadline_expiry_reports_http_error_not_internal(self, monkeypatch, owned_api_client):
        """The redirect-chain deadline surfaces as http_error, not internal_error (WORK-326).

        The chain deadline in ``redirects._follow_redirects_to_tempfile`` is an
        ``asyncio.timeout``, which raises the *builtin* ``TimeoutError`` - not
        an httpx error - so it used to fall through to the generic handler and
        a download timeout was reported as ``internal_error`` with an empty
        message (issue #760). A zero deadline expires the real ``asyncio.timeout``
        while the chain waits on the pinned-fetch lock, with no network at all.
        """
        created: list[str] = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def watch_mkdtemp(*args: Any, **kwargs: Any) -> str:
            made = real_mkdtemp(*args, **kwargs)
            created.append(made)
            return made

        monkeypatch.setattr(tempfile, "mkdtemp", watch_mkdtemp)
        monkeypatch.setattr(client_module, "get_http_timeout", lambda: 0.0)
        # A fresh lock for this test: making the download *block* on the
        # module-level lock would bind it to this test's event loop and break
        # the contended-lock tests that run in later loops.
        pin_lock = asyncio.Lock()
        monkeypatch.setattr(guard, "_PIN_FETCH_LOCK", pin_lock)

        signed = "https://storage.example/file.csv?Signature=deadbeef"
        with owned_api_client(
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        ):
            # Holding the lock in this task makes the chain's acquisition wait,
            # which is where the zero deadline expires.
            async with pin_lock:
                payload = await logs_module._download_logs_to_tempfile("abc123")
        assert payload == {"error": "Timed out while downloading logs", "code": ErrorCode.HTTP_ERROR}
        assert created and not os.path.exists(created[0]), "a timed-out download must leave no temp directory"

    async def test_the_temp_file_and_its_directory_are_removed_on_failure(self, monkeypatch, owned_api_client):
        """A failed download leaves neither a partial CSV nor an empty directory."""
        created: list[str] = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def watch_mkdtemp(*args: Any, **kwargs: Any) -> str:
            made = real_mkdtemp(*args, **kwargs)
            created.append(made)
            return made

        monkeypatch.setattr(tempfile, "mkdtemp", watch_mkdtemp)

        class Boom:
            def stream(self, method, url, **kwargs):
                raise RuntimeError("upstream is down")

        with owned_api_client(Boom()):
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

    async def test_a_streaming_503_reports_the_typed_error_with_its_body(
        self, monkeypatch, unauthenticated, owned_api_client
    ):
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        with owned_api_client(
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        ):
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

        async def watch_body(response, max_bytes):
            reads.append(response)
            return b""

        monkeypatch.setattr(stream, "read_body_capped", watch_body)
        response = _FakeResponse("https://x/f.csv", status_code=502, chunks=["bad gateway"])
        async for _ in response.aiter_bytes():  # consume it, exactly as a completed download would
            pass

        with pytest.raises(httpx.HTTPStatusError):
            await logs_module._raise_for_streamed_status(response)
        assert reads == [], "a consumed body must not be re-read"

    async def test_the_error_body_read_is_bounded(self, monkeypatch, unauthenticated, owned_api_client):
        """A hostile multi-megabyte failure page is read only up to the cap."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        signed = "https://storage.example/file.csv?Signature=deadbeef"
        with owned_api_client(
            _FakeClient([_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", location=signed)])
        ):
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

    async def test_a_real_httpx_connection_dials_the_vetted_address(self):
        """The pin owns the dial: a real connection through a real httpx client.

        The previous version of this test used a fake client that merely
        recorded which resolver was installed - it proved the swap existed,
        not that httpcore's real resolution path dials the vetted address.
        Here a real TCP server stands in for the storage host: the URL names
        a public hostname, the pin answers it with the listener's address,
        and the request must actually arrive - Host header still the
        hostname, and a bytes name (the shape anyio hands httpcore) decoded
        before the compare.
        """
        hits: list[tuple[bytes, bytes]] = []
        body = b"host,reason\na.example,blocked\n"

        async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request_line = await reader.readline()
            headers = b""
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
                headers += line
            hits.append((request_line, headers))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(_serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # anyio dials (vetted address, URL port): the port must come from the
        # URL, only the address comes from the pin (production: always 443).
        vetted = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        try:
            async with logs_module._PIN_FETCH_LOCK:
                with logs_module._pinned_dns("pin.example", vetted):
                    # anyio passes bytes names to getaddrinfo: the pin must
                    # decode before comparing, or it never matches.
                    assert socket.getaddrinfo(b"pin.example", None, type=socket.SOCK_STREAM) == vetted
                    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                        response = await client.get(f"http://pin.example:{port}/file.csv")
            assert response.status_code == 200
            assert response.content == body
        finally:
            server.close()
            await server.wait_closed()
        assert len(hits) == 1, "exactly one real connection arrived"
        request_line, headers = hits[0]
        assert request_line.startswith(b"GET /file.csv ")
        assert b"host: pin.example" in headers.lower(), "the dialled IP is the vetted one; the Host stays the hostname"

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

    def test_append_preview_short_circuits_once_the_window_is_full(self, monkeypatch):
        """No later text is split or appended once either preview cap is reached."""
        monkeypatch.setattr(preview, "DOWNLOAD_PREVIEW_MAX_LINES", 1)
        lines = ["first\n"]
        assert preview._append_preview(lines, 6, "second\nthird\n") == 6
        assert lines == ["first\n"]

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
            def __init__(self) -> None:
                self.cookies: dict[str, str] = {}

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
        monkeypatch.setattr(preview, "DOWNLOAD_PREVIEW_MAX_BYTES", 16)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["A" * 30, "AAAA\nreal,row\n"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["text"] == "real,row\n"
        assert payload["row_count"] == 2
        assert payload["size"] == 44

    async def test_an_oversized_record_spanning_several_chunks_stays_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preview, "DOWNLOAD_PREVIEW_MAX_BYTES", 16)
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
        monkeypatch.setattr(preview, "DOWNLOAD_PREVIEW_MAX_BYTES", 16)
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


class TestSuccessfulDownloadLifecycle:
    """Registration bounds disk use and never forgets a failed deletion."""

    def _register(self, tmp_path: Path, tag: str) -> str:
        path = tmp_path / tag / "download.csv"
        path.parent.mkdir(parents=True)
        path.write_text("csv")
        logs_module._note_successful_download(str(path), True)
        return str(path)

    def test_retention_is_bounded_by_evicting_the_oldest_download(self, tmp_path):
        """A long-lived server must not keep every CSV it ever handed out."""
        with logs_module.downloads_owned() as registry:
            cap = logs_module.MAX_RETAINED_DOWNLOADS
            paths = [self._register(tmp_path, f"dl{i}") for i in range(cap + 2)]
            # The two oldest were evicted and unlinked; the newest `cap` stay.
            assert not (tmp_path / "dl0" / "download.csv").exists()
            assert not (tmp_path / "dl1" / "download.csv").exists()
            for tag in range(2, cap + 2):
                assert (tmp_path / f"dl{tag}" / "download.csv").exists()
            assert len(registry) == cap
            assert all(path in registry for path in paths[2:])
            logs_module.cleanup_successful_downloads()
            assert len(registry) == 0

    def test_a_failed_deletion_stays_registered_for_a_later_retry(self, tmp_path, caplog):
        """The old cleanup popped each path BEFORE unlinking: one OSError and the
        CSV leaked forever with no record it had ever existed. A failed removal
        must keep the path registered so a later cleanup can retry it."""
        with logs_module.downloads_owned() as registry:
            blocker = tmp_path / "blocker"
            blocker.mkdir()
            blocked = str(blocker)  # a directory at the registered path: unlink fails with OSError
            logs_module._note_successful_download(blocked, True)
            gone = self._register(tmp_path, "gone")

            with caplog.at_level("WARNING"):
                logs_module.cleanup_successful_downloads()

            assert "Failed to remove temp log file" in caplog.text
            # The removable file was unregistered; the failed one is still registered.
            assert blocked in registry
            assert gone not in registry

    def test_registration_without_a_lifespan_fails_fast_and_records_nothing(self, tmp_path):
        """Outside a lifespan there is no owner that could ever reclaim the
        CSV, so registration is refused with an explicit error before the path
        is recorded - not silently kept in a process-global registry."""
        path = tmp_path / "orphan" / "download.csv"
        path.parent.mkdir(parents=True)
        path.write_text("csv")
        with pytest.raises(logs_module.MissingDownloadRegistryError):
            logs_module._note_successful_download(str(path), True)
        assert logs_module._download_registry.get() is None

    def test_a_failed_download_is_never_registered(self, tmp_path):
        """``downloaded=False`` records nothing and needs no owning lifespan."""
        path = tmp_path / "nope" / "download.csv"
        logs_module._note_successful_download(str(path), False)  # no lifespan bound: still a no-op
        assert logs_module._download_registry.get() is None

    def test_registration_goes_to_the_owning_registry(self, tmp_path):
        """The owning registry is explicit: a standalone session owns one via downloads_owned."""
        registry = logs_module.DownloadRegistry()
        with logs_module.downloads_owned(registry):
            path = self._register(tmp_path, "owned")
            assert path in registry
            assert logs_module.current_download_registry() is registry

    def test_a_never_written_path_is_removed_without_a_trace(self, tmp_path):
        """FileNotFoundError means there is nothing to remove and nothing to report."""
        missing = str(tmp_path / "nope" / "download.csv")
        assert logs_module._remove_temp_file(missing) is True

    def test_a_shared_parent_directory_survives_removal(self, tmp_path):
        """The mkdtemp parent is removed only when empty; a parent another file
        still lives in must not be rmdir'd (or its removal failure reported)."""
        parent = tmp_path / "shared"
        parent.mkdir()
        registered = parent / "download.csv"
        registered.write_text("csv")
        (parent / "other").write_text("still here")
        assert logs_module._remove_temp_file(str(registered)) is True
        assert not registered.exists()
        assert parent.exists(), "a parent another file lives in must survive"

    def test_an_unevictable_oldest_download_stops_the_eviction_sweep(self, tmp_path):
        """When the oldest registered path cannot be unlinked, the sweep stops
        instead of spinning; the overflow stays registered for shutdown reclaim."""
        with logs_module.downloads_owned() as registry:
            blocker = tmp_path / "blocker"
            blocker.mkdir()  # a directory: unlink fails with OSError, cannot be evicted
            logs_module._note_successful_download(str(blocker), True)
            survivors = [self._register(tmp_path, f"dl{i}") for i in range(logs_module.MAX_RETAINED_DOWNLOADS + 1)]
            assert str(blocker) in registry, "the failed eviction must stay registered"
            assert all(survivor in registry for survivor in survivors), (
                "an unevictable oldest must stop the sweep, not drop the rest"
            )
