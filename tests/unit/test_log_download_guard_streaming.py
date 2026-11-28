"""The log download streams to disk under a cap without stalling the event loop.

This part pins the write path: the body is streamed to disk under a total-size cap,
disk writes do not stall the loop, cancellation during a write is honoured, and the
shared address-vetting fakes and fixtures it builds on are the same ones
``test_log_download_guard.py`` pins. The tool lifecycle lives in
``test_log_download_guard_download.py``.

SPDX-License-Identifier: MIT
"""

import asyncio
import io
import ipaddress
import os
import socket
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp.tools import logs as logs_module
from nextdns_mcp.tools.logs import preview, stream

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

    async def test_after_the_preview_fills_later_chunks_are_not_decoded_and_writes_are_coalesced(
        self, tmp_path, monkeypatch
    ):
        """The preview window is the last use of the body text (PERF-9)."""
        decode_calls = 0
        real_incremental_decoder = stream.codecs.getincrementaldecoder

        def counting_incremental_decoder(encoding):
            decoder_type = real_incremental_decoder(encoding)

            class CountingDecoder:
                def __init__(self, errors):
                    self._decoder = decoder_type(errors)

                def decode(self, data: bytes, final: bool = False) -> str:
                    nonlocal decode_calls
                    decode_calls += 1
                    return self._decoder.decode(data, final=final)

            return CountingDecoder

        monkeypatch.setattr(stream.codecs, "getincrementaldecoder", counting_incremental_decoder)

        real_fdopen = stream.os.fdopen
        writes: list[bytes] = []

        class CountingFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def write(self, data: bytes) -> int:
                writes.append(bytes(data))
                return self._wrapped.write(data)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._wrapped, name)

        monkeypatch.setattr(stream.os, "fdopen", lambda fd, mode: CountingFile(real_fdopen(fd, mode)))

        path = str(tmp_path / "download.csv")
        chunks = [f"row{i},host\n" for i in range(28)] + ["名前,値", "last"]
        payload = await logs_module._write_stream_to_tempfile(_FakeResponse("https://x/file.csv", chunks=chunks), path)

        expected_bytes = "".join(chunks).encode("utf-8")
        assert Path(path).read_bytes() == expected_bytes
        assert payload["row_count"] == 29
        assert payload["preview"]["line_count"] == logs_module.DOWNLOAD_PREVIEW_MAX_LINES
        assert payload["preview"]["text"] == "".join(chunks[: logs_module.DOWNLOAD_PREVIEW_MAX_LINES])
        # The decoder is needed only while text can enter the preview; the
        # skipped body still contributes raw bytes and raw newline counts.
        assert decode_calls == logs_module.DOWNLOAD_PREVIEW_MAX_LINES
        # One offload per preview-filling chunk, then the small body tail is
        # coalesced into one write instead of one offload per stream chunk.
        assert len(writes) == logs_module.DOWNLOAD_PREVIEW_MAX_LINES + 1
        assert b"".join(writes) == expected_bytes

    async def test_post_preview_chunks_flush_when_the_coalesce_buffer_fills(self, tmp_path, monkeypatch):
        """A full buffer lands immediately; only the remainder waits for EOF."""
        monkeypatch.setattr(stream, "_STREAM_WRITE_COALESCE_BYTES", 8)
        real_fdopen = stream.os.fdopen
        writes: list[bytes] = []

        class CountingFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def write(self, data: bytes) -> int:
                writes.append(bytes(data))
                return self._wrapped.write(data)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._wrapped, name)

        monkeypatch.setattr(stream.os, "fdopen", lambda fd, mode: CountingFile(real_fdopen(fd, mode)))

        path = str(tmp_path / "download.csv")
        chunks = [f"row{i}\n" for i in range(logs_module.DOWNLOAD_PREVIEW_MAX_LINES)] + [
            "a" * 5,
            "b" * 5,
            "c" * 3,
        ]
        payload = await logs_module._write_stream_to_tempfile(_FakeResponse("https://x/file.csv", chunks=chunks), path)

        expected_bytes = "".join(chunks).encode("utf-8")
        assert Path(path).read_bytes() == expected_bytes
        assert payload["row_count"] == logs_module.DOWNLOAD_PREVIEW_MAX_LINES + 1
        assert len(writes) == logs_module.DOWNLOAD_PREVIEW_MAX_LINES + 2
        assert writes[-2:] == [b"a" * 5 + b"b" * 5, b"c" * 3]
        assert b"".join(writes) == expected_bytes

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
        monkeypatch.setattr(preview, "DOWNLOAD_PREVIEW_MAX_LINES", 2)
        payload = await logs_module._write_stream_to_tempfile(
            _FakeResponse("https://x/file.csv", chunks=["l1\nl2\nl3\nl4\nl5\n"]), str(tmp_path / "d.csv")
        )
        assert payload["preview"]["line_count"] == 2
        assert payload["preview"]["text"] == "l1\nl2\n"
        # The rows past the window are still on disk and still counted.
        assert payload["row_count"] == 5

    async def test_the_preview_stops_at_the_byte_cap(self, tmp_path, monkeypatch):
        """A line that would cross the byte cap is left out rather than truncated in half."""
        monkeypatch.setattr(preview, "DOWNLOAD_PREVIEW_MAX_BYTES", 8)
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


# The holding sink below still needs the real ``os.fdopen`` to build the file
# behind it, so capture it before a test replaces the attribute.
_REAL_FDOPEN = os.fdopen

# Both sides of the held write are bounded, so a frozen loop produces an
# assertion failure rather than a CI job that hangs until its timeout.
_WRITE_START_SECONDS = 5.0
_PROGRESS_SECONDS = 1.0
_RELEASE_SECONDS = 10.0


class _HoldingSink:
    """Stands in for ``os.fdopen``: the real file, with its first write held open.

    ``write`` blocks on a thread event, which reproduces a slow disk without a
    slow disk: a write issued on the event loop freezes that loop for as long as
    the watcher keeps the event clear, and a write issued elsewhere does not.
    """

    def __init__(self) -> None:
        self._file: Any = None
        self.write_started = threading.Event()
        self._released = threading.Event()

    def __call__(self, fd: int, *args: Any, **kwargs: Any) -> Self:
        self._file = _REAL_FDOPEN(fd, *args, **kwargs)
        return self

    def write(self, raw: bytes) -> int:
        if not self.write_started.is_set():
            self.write_started.set()
            self._released.wait(_RELEASE_SECONDS)
        return self._file.write(raw)

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        # Production closes the handle on the worker pool, so the sink is closed
        # through here rather than by leaving a ``with`` block.
        self._file.close()


class _LoopStallWatcher:
    """Whether an independent coroutine finished while a write was being held.

    It has to run in its own thread: a blocked event loop never runs anything
    scheduled on it, so a watcher written as a coroutine would be frozen along
    with the loop it is measuring, and the check would hang rather than fail.
    """

    def __init__(self, sink: _HoldingSink, progress: threading.Event) -> None:
        self._sink = sink
        self._progress = progress
        self.loop_stayed_free = False
        self.note = ""

    def run(self) -> None:
        if not self._sink.write_started.wait(_WRITE_START_SECONDS):
            self.note = "the download never reached a write"
            self._sink.release()
            return
        self.loop_stayed_free = self._progress.wait(_PROGRESS_SECONDS)
        if not self.loop_stayed_free:
            self.note = f"nothing else on the loop ran for {_PROGRESS_SECONDS}s while one write was pending"
        self._sink.release()  # the download then finishes and the failure is reported, not swallowed


async def _second_request(progress: threading.Event) -> None:
    """Any other call this process would serve while the download is writing."""
    await asyncio.sleep(0)
    progress.set()


class TestDiskWritesDoNotStallTheLoop:
    """A download's disk writes must not hold the event loop (issue #648).

    One download is thousands of writes, so a write on the loop makes every
    other request in the process wait for it. The file's bytes are pinned by
    ``TestStreamingToDisk``; what is pinned here is that an unrelated coroutine
    still finishes while a write is in flight.
    """

    async def test_a_pending_write_leaves_the_loop_free(self, tmp_path, monkeypatch):
        path = str(tmp_path / "download.csv")
        chunks = ["a.com,blocked\n", "b.com,blocked\n", "c.com,blocked\n"]
        sink = _HoldingSink()
        monkeypatch.setattr(os, "fdopen", sink)

        progress = threading.Event()
        watcher = _LoopStallWatcher(sink, progress)
        thread = threading.Thread(target=watcher.run, name="loop-stall-watcher", daemon=True)
        thread.start()
        try:
            download = asyncio.create_task(
                logs_module._write_stream_to_tempfile(_FakeResponse("https://x/file.csv", chunks=chunks), path)
            )
            other = asyncio.create_task(_second_request(progress))
            payload = await download
            await other
        finally:
            thread.join(_WRITE_START_SECONDS + _RELEASE_SECONDS)

        assert watcher.loop_stayed_free, watcher.note
        # Offloading the write changes when bytes land, not which bytes land.
        assert payload["row_count"] == 3
        assert Path(path).read_text(encoding="utf-8") == "".join(chunks)


# The buffer is smaller than the first chunk on purpose: a chunk larger than the
# buffer is written straight at the raw layer, and that is the only state in
# which the buffered writer still owns its lock across a blocking write - the
# state a cancellation catches it in, and the reason close() has to wait.
_SINK_BUFFER_BYTES = 8192
_SINK_CHUNK_BYTES = 4 * _SINK_BUFFER_BYTES


class _BlockingRawSink(io.RawIOBase):
    """A raw file over the real fd whose first write waits to be released.

    It sits under a real ``io.BufferedWriter``, so a write in progress holds the
    buffered writer's lock exactly as a real one does; nothing here fakes the
    lock, it only delays the syscall underneath it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._fd: int | None = None
        self.chunks = [b"x" * _SINK_CHUNK_BYTES, b"y" * _SINK_CHUNK_BYTES]
        self.raw_write_started = threading.Event()
        self._released = threading.Event()

    def open_over(self, fd: int, *args: Any, **kwargs: Any) -> io.BufferedWriter:
        self._fd = fd
        return io.BufferedWriter(self, buffer_size=_SINK_BUFFER_BYTES)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def write(self, view: memoryview) -> int:
        if not self.raw_write_started.is_set():
            self.raw_write_started.set()
            self._released.wait(_RELEASE_SECONDS)
        assert self._fd is not None
        return os.write(self._fd, view)

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        super().close()


class _CancellationWatcher:
    """Asks for the cancellation off the loop, then records whether the loop survived it.

    Both halves happen in a thread because a blocked loop runs nothing
    scheduled on it: a watcher written as a coroutine would be frozen along with
    the loop it measures, and the check would hang rather than fail.
    """

    def __init__(
        self,
        sink: _BlockingRawSink,
        cancel_requested: threading.Event,
        loop_progress: threading.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._sink = sink
        self._cancel_requested = cancel_requested
        self._loop_progress = loop_progress
        self._loop = loop
        self._task: asyncio.Task[Any] | None = None
        self.loop_stayed_free = False
        self.note = ""

    def attach(self, task: asyncio.Task[Any]) -> None:
        self._task = task

    def run(self) -> None:
        if not self._sink.raw_write_started.wait(_WRITE_START_SECONDS):
            self.note = "the download never reached a write below the buffer"
            self._sink.release()
            return
        if self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self._cancel_requested.set()
        self.loop_stayed_free = self._loop_progress.wait(_PROGRESS_SECONDS)
        if not self.loop_stayed_free:
            self.note = "the loop stayed frozen from the cancel until the in-flight write was released"
        self._sink.release()


async def _progress_after(cancel_requested: threading.Event, done: threading.Event) -> None:
    """The next request this server would serve, held back until the cancel is queued.

    Waiting for the queueing rather than starting at the top of the download is
    the point: a coroutine that finished before the cancellation was requested
    says nothing about the unwinding path, and that is where the stall is.
    """
    while not cancel_requested.is_set():
        await asyncio.sleep(0.01)
    done.set()


class TestCancellationDuringAWrite:
    """Cancelling a download while a write is in flight must not stall the loop either.

    Cancellation unwinds the file's close, and close needs the buffered writer's
    lock that the in-flight write is holding. A close taken on the loop waits
    for that lock there, which stops every other request for the rest of the
    write - the same failure mode the write offload removed, arriving by a
    different route. So the check has two halves: the loop keeps running, and
    the cancellation still reaches the caller and removes the partial CSV and
    its directory.
    """

    async def test_cancelling_during_a_raw_write_keeps_the_loop_running(self, tmp_path, monkeypatch):
        path = str(tmp_path / "download.csv")
        sink = _BlockingRawSink()
        monkeypatch.setattr(os, "fdopen", sink.open_over)

        loop = asyncio.get_running_loop()
        cancel_requested = threading.Event()
        loop_progress = threading.Event()
        watcher = _CancellationWatcher(sink, cancel_requested, loop_progress, loop)
        download = asyncio.create_task(
            logs_module._write_stream_to_tempfile(_FakeResponse("https://x/f.csv", chunks=sink.chunks), path)
        )
        other = asyncio.create_task(_progress_after(cancel_requested, loop_progress))
        watcher.attach(download)
        thread = threading.Thread(target=watcher.run, name="cancellation-watcher", daemon=True)
        thread.start()
        try:
            with pytest.raises(asyncio.CancelledError):
                await download
            await other
        finally:
            thread.join(_WRITE_START_SECONDS + _RELEASE_SECONDS)

        assert watcher.loop_stayed_free, watcher.note

    async def test_a_cancelled_download_leaves_no_partial_file_and_no_directory(self, monkeypatch):
        """The caller's cleanup guard still runs when the download is cancelled mid-write."""
        created: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def watch_mkdtemp(*args: Any, **kwargs: Any) -> str:
            made = real_mkdtemp(*args, **kwargs)
            created.append(made)
            return made

        sink = _BlockingRawSink()
        monkeypatch.setattr(os, "fdopen", sink.open_over)
        monkeypatch.setattr(tempfile, "mkdtemp", watch_mkdtemp)
        # The download resolves its client through the current owner (issue
        # #642): the key comes first, then an owner carrying the scripted
        # client is published around the whole flow.
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setattr(
            client_module,
            "create_nextdns_client",
            lambda: _FakeClient(
                [_FakeResponse("https://api.nextdns.io/profiles/abc123/logs/download", chunks=sink.chunks)]
            ),
        )
        owner_cm = client_module.api_client_owned(client_module.ApiClientOwner())
        owner_cm.__enter__()  # exited in this test's own finally, same task

        loop = asyncio.get_running_loop()
        cancel_requested = threading.Event()
        loop_progress = threading.Event()
        watcher = _CancellationWatcher(sink, cancel_requested, loop_progress, loop)
        download = asyncio.create_task(logs_module._download_logs_to_tempfile("abc123"))
        other = asyncio.create_task(_progress_after(cancel_requested, loop_progress))
        watcher.attach(download)
        thread = threading.Thread(target=watcher.run, name="cancellation-watcher", daemon=True)
        thread.start()
        try:
            # The caller's handlers never see a cancelled task, so nothing is
            # returned here: the task raises and its guard runs in ``finally``.
            await download
        except asyncio.CancelledError:
            pass
        finally:
            thread.join(_WRITE_START_SECONDS + _RELEASE_SECONDS)
            await other
            owner_cm.__exit__(None, None, None)

        assert watcher.loop_stayed_free, watcher.note
        assert created, "no temp directory was created, so the cleanup claim is unproven"
        assert not os.path.exists(created[0]), f"the cancelled download left {created[0]} behind"
