"""Sandbox process management for the Generative UI provider.

Owns the Deno/Pyodide worker lifecycle end to end: the sanitized environment,
the bounded V8 heap, the cold-start handshake, the request/response wire
framing, the bounded stdout reader that rejects oversized results during the
read, and the reap that terminates the worker. Nothing here subclasses
prefab-ui's ``PyodideSandbox``: an upstream rename of its private process or
teardown state cannot silently drop a guard, because this module does not
read any of it. The only upstream values used are the runner path constants,
which are data, not a class body.

SPDX-License-Identifier: MIT
"""

import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

# The three paths that make the worker runnable: the runner script, the Prefab
# source tree it imports, and the lock that pins its dependencies. They are data
# (module constants), not behaviour, so reading them does not couple this module
# to any upstream class body.
from prefab_ui.sandbox._pyodide import DENO_LOCK, PREFAB_SRC, RUNNER_JS

logger = logging.getLogger(__name__)

MAX_RESULT_BYTES = 256 * 1024
EXECUTION_TIMEOUT = 10.0
# Cold-start (Deno + Pyodide boot) gets its own budget so it never consumes
# the user-code EXECUTION_TIMEOUT (issue #541).
SANDBOX_START_TIMEOUT = 60.0
# The sandbox renders component trees headlessly; 1 GiB is generous for that
# and bounds a runaway worker. The upstream default grants 4 GiB.
SANDBOX_V8_HEAP_MB = 1024

# The worker answers each request with a single JSON line on stdout:
# {"id": "<32-hex>", "result": <wire>} (or {"id": ..., "error": ...}). The
# wire is serialized with JSON.stringify, so the raw line is the UTF-8 wire
# plus a small ASCII envelope — no escaping expansion. Bound the line just
# above MAX_RESULT_BYTES so a runaway generated view is rejected during the
# stdout read, before the host buffers, JSON-decodes, and validates it.
WIRE_LINE_SLACK = 4096
MAX_WIRE_LINE_BYTES = MAX_RESULT_BYTES + WIRE_LINE_SLACK
_READ_CHUNK_SIZE = 65536

# Exact host environment variables approved for the Deno sandbox subprocess.
# Generated code executes with Pyodide's full ``os`` module, so anything in the
# subprocess environment is readable from LLM-generated code. A name-substring
# blocklist is not sufficient: credential-bearing variables with unmarked
# names (``DB_DSN``, ``SERVICE_ACCOUNT_JSON``, ``APP_CONFIG_JSON``) would cross
# the boundary, so the default is to forward nothing not explicitly approved.
# The ``DENO_`` name space is NOT blanket-approved: it contains credentials
# (``DENO_AUTH_TOKENS``, ``DENO_KV_ACCESS_TOKEN``), so the runtime variables
# the worker may honor are listed explicitly instead.
_SANDBOX_ENV_ALLOWLIST = frozenset(
    {
        # Process discovery and the user's home (Deno's cache dir).
        "PATH",
        "HOME",
        # Locale, so Pyodide/Deno decode UTF-8 consistently with the host.
        "LANG",
        "LC_ALL",
        # Temp-dir selection for the worker's scratch files.
        "TMPDIR",
        "TEMP",
        "TMP",
        # Windows equivalents of HOME/PATH-adjacent state.
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        # Deno runtime knobs the worker may honor. These are named one by one
        # because the DENO_ namespace also holds credentials, which must not
        # pass on a prefix: DENO_AUTH_TOKENS is a list of bearer tokens for
        # private module registries and DENO_KV_ACCESS_TOKEN a Deno KV PAT.
        # DENO_CERT/DENO_TLS_CA_STORE are certificate paths and stores, not
        # key material, and a private-CA host needs them to fetch the CDN.
        "DENO_DIR",
        "DENO_NO_UPDATE_CHECK",
        "DENO_V8_FLAGS",
        "DENO_UNSTABLE_DETECT_CJS",
        "DENO_CERT",
        "DENO_TLS_CA_STORE",
    }
)
# LC_* covers locale variants (LC_CTYPE, LC_MESSAGES, ...).
_SANDBOX_ENV_PREFIXES = ("LC_",)


def _sandbox_env() -> dict[str, str]:
    """Allowlisted copy of the host environment for the Deno subprocess.

    Only variables the worker needs to start are forwarded; everything else —
    including every credential, whatever its name — stays on the host side of
    the boundary.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name in _SANDBOX_ENV_ALLOWLIST or name.startswith(_SANDBOX_ENV_PREFIXES)
    }


def _sandbox_startup_env() -> dict[str, str]:
    """Environment for the Deno worker: sanitized, with a bounded V8 heap."""
    env = _sandbox_env()
    env["DENO_V8_FLAGS"] = f"--max-old-space-size={SANDBOX_V8_HEAP_MB}"
    env["DENO_UNSTABLE_DETECT_CJS"] = "1"
    # Point the Deno cache at the sandbox's dedicated temp directory so the
    # module/wheel cache (Pyodide's downloaded wheels included) is the only
    # writable surface the worker can reach.
    env["DENO_DIR"] = str(_sandbox_deno_dir())
    return env


_SANDBOX_DENO_DIR_PREFIX = "nextdns-mcp-sandbox-"
_sandbox_deno_dir_lock = threading.Lock()
_sandbox_deno_dir_path: Path | None = None
_sandbox_deno_dir_cleanup_registered = False


def _sandbox_deno_dir() -> Path:
    """Unpredictable private temp directory that is the sandbox's only writable path.

    Created by ``tempfile.mkdtemp`` — ``O_CREAT | O_EXCL``, mode 0700 — so a
    symlink or another user's directory planted at a guessable name can never be
    adopted into the ``--allow-write`` grant, which would turn the sandbox's own
    cache directory into a host path (dmeiser/nextdns-mcp#593). The path is
    canonical and cached for the process lifetime, so Deno's module cache and
    Pyodide's wheel cache are reused across worker starts and both launch sites
    (:func:`_deno_command` and :func:`_sandbox_startup_env`) name the same
    directory; it is removed at interpreter exit so restarts do not accumulate
    directories, and a directory removed mid-run is recreated on the next start.
    """
    global _sandbox_deno_dir_path, _sandbox_deno_dir_cleanup_registered
    with _sandbox_deno_dir_lock:
        cached = _sandbox_deno_dir_path
        if cached is not None and cached.is_dir():
            return cached
        if not _sandbox_deno_dir_cleanup_registered:
            atexit.register(_drop_sandbox_deno_dir)
            _sandbox_deno_dir_cleanup_registered = True
        _sandbox_deno_dir_path = Path(tempfile.mkdtemp(prefix=_SANDBOX_DENO_DIR_PREFIX)).resolve()
        return _sandbox_deno_dir_path


def _drop_sandbox_deno_dir() -> None:
    """Remove this process's sandbox cache directory; registered with ``atexit``."""
    path = _sandbox_deno_dir_path
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _deno_command() -> list[str]:
    deno = shutil.which("deno")
    if deno is None:
        raise RuntimeError("Deno is required for the Prefab sandbox. Install it from https://deno.land")
    sandbox_dir = _sandbox_deno_dir()
    # Generated code executes in this subprocess with Pyodide's full ``os``
    # module, so the launch flags are the sandbox's filesystem boundary
    # (dmeiser/nextdns-mcp#593): reads are confined to the Prefab sandbox
    # assets plus the sandbox's own cache dir, and the cache dir is the only
    # writable path — a mounted NEXTDNS_API_KEY_FILE secret or the
    # application source tree is unreachable.
    return [
        deno,
        "run",
        "--no-config",
        f"--lock={DENO_LOCK}",
        "--frozen",
        "--unstable-detect-cjs",
        "--no-prompt",
        f"--allow-read={RUNNER_JS},{PREFAB_SRC},{DENO_LOCK},{sandbox_dir}",
        f"--allow-write={sandbox_dir}",
        "--allow-net=cdn.jsdelivr.net:443",
        str(RUNNER_JS),
        str(PREFAB_SRC),
    ]


def _spawn_deno(env: dict[str, str]) -> "asyncio.Future[subprocess.Popen[bytes]]":
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(
        None,
        lambda: subprocess.Popen(
            _deno_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        ),
    )


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the worker, escalate to kill if it ignores that, and close its pipes.

    Blocking: ``communicate`` waits up to 5s for the worker to exit. Event-loop
    callers must offload it to a worker thread (``asyncio.to_thread`` /
    ``run_in_executor``); the call sites below do.
    """
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("Generative UI sandbox worker ignored terminate for 5s; it was killed")
        process.kill()
        process.communicate()
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _discard_startup(future: "asyncio.Future[subprocess.Popen[bytes]]") -> None:
    """Reclaim a worker whose start was cancelled before it completed.

    Done callbacks run on the event loop, and reaping blocks for up to ~5s, so
    the reap is handed to the default executor instead of stalling the loop.
    """
    if future.cancelled() or future.exception() is not None:
        return
    future.get_loop().run_in_executor(None, _reap_process, future.result())


def _startup_error(stderr_lines: list[str]) -> RuntimeError:
    err = "\n".join(stderr_lines)
    if len(err) > 2000:
        err = f"{err[:1000]}\n...\n{err[-1000:]}"
    return RuntimeError(f"Pyodide sandbox failed to start:\n{err}")


async def _read_startup_line(proc_stderr: Any, stderr_lines: list[str]) -> str:
    """Read one startup stderr line, raising if the worker stopped signalling before readiness.

    An empty read from a pipe is EOF, not a quiet moment: it means every write end of
    the worker's stderr is closed, so the ``pyodide:ready`` signal can never arrive —
    whether the worker died or is still alive with that pipe closed. Returning the
    empty line instead would leave ``_await_ready`` submitting reads forever, one
    executor future per spin, until the caller's cold-start timeout cancelled it.
    """
    loop = asyncio.get_event_loop()
    line = await loop.run_in_executor(None, proc_stderr.readline)
    if not line:
        raise _startup_error(stderr_lines)
    decoded = line.decode().rstrip()
    stderr_lines.append(decoded)
    return decoded


async def _await_ready(proc: subprocess.Popen[bytes]) -> None:
    """Drain the worker's stderr until it signals readiness, as upstream does.

    Raises once stderr reaches EOF: the readiness signal can no longer arrive, so
    there is nothing left to wait for and ``_start`` reclaims the worker.
    """
    assert proc.stderr is not None
    stderr_lines: list[str] = []
    while True:
        decoded = await _read_startup_line(proc.stderr, stderr_lines)
        if "pyodide:ready" in decoded:
            break


class _BoundedStdout:
    """File-like bounding each stdout line to ``limit`` bytes during the read.

    The worker's answer line is read with no cap otherwise, so a runaway
    generated view buffers unboundedly in the host before the result size
    check can run. This wrapper reads in chunks, preserves any bytes past the
    newline for the next response, and raises ``ValueError`` the moment a line
    exceeds ``limit`` — before JSON decode and ``PrefabApp.from_json``
    allocation. ``on_overflow`` stops the sandbox so the next call starts a
    fresh worker (a partially consumed line would otherwise misalign the wire
    protocol).
    """

    def __init__(self, raw: Any, limit: int, on_overflow: Callable[[], None]) -> None:
        self._raw = raw
        self._limit = limit
        self._on_overflow = on_overflow
        self._pending = b""

    def _next_chunk(self) -> bytes:
        if self._pending:
            chunk, self._pending = self._pending, b""
            return chunk
        # read1 returns as soon as any bytes are available; read(n) would
        # block trying to fill the whole chunk and stall short responses.
        read1 = getattr(self._raw, "read1", self._raw.read)
        return read1(_READ_CHUNK_SIZE)

    def _feed(self, chunk: bytes, chunks: list[bytes], total: int) -> tuple[bool, int]:
        """Append one chunk, splitting at a newline; return (line_complete, new_total)."""
        newline = chunk.find(b"\n")
        if newline != -1:
            self._pending = chunk[newline + 1 :]
            chunk = chunk[: newline + 1]
        chunks.append(chunk)
        return newline != -1, total + len(chunk)

    def readline(self) -> bytes:
        chunks: list[bytes] = []
        total = 0
        complete = False
        while not complete and total <= self._limit:
            chunk = self._next_chunk()
            if not chunk:
                break
            complete, total = self._feed(chunk, chunks, total)
        if total > self._limit:
            logger.warning(
                "Generative UI sandbox answer exceeded the %d byte wire cap; the worker was discarded", self._limit
            )
            self._on_overflow()
            raise ValueError(f"result exceeds maximum size of {MAX_RESULT_BYTES} bytes")
        return b"".join(chunks)

    def close(self) -> None:
        self._raw.close()

    @property
    def closed(self) -> bool:
        return bool(self._raw.closed)

    def fileno(self) -> int:
        return self._raw.fileno()


class _WorkerGone(RuntimeError):
    """The worker is unusable — a pipe died, it answered with nothing, or its answer was malformed.

    ``DenoPyodideSandbox.run`` reports it to the caller as ``RuntimeError``, the
    failure type ``prefab_ui.generative.execute`` turns into the tool's
    ``ValueError``, and discards the worker.
    """


def _round_trip(stdin: Any, stdout: Any, frame: bytes) -> bytes:
    """Write one request frame and read the worker's answer line (blocking I/O, off the loop).

    Runs in the default executor: ``stdin.write``/``flush`` can block past the
    64 KiB pipe buffer, and ``stdout.readline`` blocks until the worker answers.
    """
    stdin.write(frame)
    stdin.flush()
    return stdout.readline()


class DenoPyodideSandbox:
    """A Deno/Pyodide worker this class owns outright: sanitized env, bounded heap, disposable interpreter.

    prefab-ui's ``PyodideSandbox`` copies ``os.environ`` verbatim into the Deno
    subprocess (exposing ``NEXTDNS_API_KEY`` to generated code), grants the
    worker a 4 GiB V8 heap, keeps one warm interpreter whose imported modules
    are shared across calls (so code from one ``generate_prefab_ui`` call can
    poison later ones), and reads stdout with no cap. Rather than subclass it
    and reach into its process, teardown and startup internals to fix each of
    those, this class owns the whole lifecycle itself: spawn, readiness
    handshake, request/response framing, bounded reads, and reaping. The only
    upstream values it uses are the runner paths in :func:`_deno_command`.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = asyncio.Lock()
        # ``stop()`` is called from a worker thread (``asyncio.to_thread``) and
        # synchronously from the bounded-stdout overflow callback inside the
        # executor thread that reads the answer, so the process slot and the
        # closed flag are guarded by a threading lock — an ``asyncio.Lock``
        # cannot be held by those callers, and an unguarded swap let a shutdown
        # race a cold start (issue #746).
        self._state_lock = threading.Lock()
        self._closed = False

    async def start(self) -> None:
        """Cold-start the worker, outside the user-code execution timeout."""
        async with self._lock:
            await self._ensure_worker()

    def stop(self) -> None:
        """Discard the worker and close this sandbox against any later start.

        The slot is taken under ``_state_lock`` so a concurrent cold start
        cannot publish a worker behind the shutdown, and the reaping happens
        outside the lock because it blocks for up to 5s. Calling ``stop()``
        twice is safe: the second call finds no process.
        """
        with self._state_lock:
            self._closed = True
            process, self._process = self._process, None
        if process is not None:
            _reap_process(process)

    async def run(self, code: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute Prefab code and return the wire protocol dict.

        The worker is discarded whichever way the execution ends — an answer, a
        crashed worker, or a cancellation — which is what keeps one generated
        view from poisoning the next (issue #541). Cancellation is re-raised
        unchanged so ``asyncio.wait_for`` still reports it as a timeout.
        """
        async with self._lock:
            try:
                return await self._execute(code, data)
            except _WorkerGone as exc:
                raise RuntimeError(str(exc)) from None
            finally:
                await asyncio.to_thread(self.stop)

    async def _execute(self, code: str, data: dict[str, Any] | None) -> dict[str, Any]:
        request_id = uuid4().hex
        logger.info(
            "Generative UI sandbox running request %s (%d bytes of code)", request_id, len(code.encode("utf-8"))
        )
        worker = await self._ensure_worker()
        return _decode_response(await self._exchange(worker, request_id, code, data), request_id)

    async def _ensure_worker(self) -> subprocess.Popen[bytes]:
        """Return a live worker, cold-starting one when none is running.

        Raises once this sandbox has been stopped: a sandbox is disposable by
        design, and a render that arrives after teardown must not spawn a
        worker nothing will ever reap.
        """
        process = self._process
        if process is not None and process.poll() is None:
            return process
        if self._closed:
            raise _WorkerGone("Sandbox is shut down")
        return await self._start()

    async def _exchange(
        self,
        process: subprocess.Popen[bytes],
        request_id: str,
        code: str,
        data: dict[str, Any] | None,
    ) -> bytes:
        """Write one request frame and read the worker's answer line off bounded stdout."""
        stdin, stdout = process.stdin, process.stdout
        if stdin is None or stdout is None:
            raise _WorkerGone("Sandbox failed to start")
        frame = (json.dumps({"id": request_id, "code": code, "data": data or {}}) + "\n").encode()
        try:
            return await asyncio.get_running_loop().run_in_executor(None, _round_trip, stdin, stdout, frame)
        except OSError:
            raise _WorkerGone("Sandbox process died unexpectedly") from None

    async def _start(self) -> subprocess.Popen[bytes]:
        """Spawn the worker with a sanitized environment, a bounded heap, and a bounded stdout."""
        starting = _spawn_deno(_sandbox_startup_env())
        try:
            process = await asyncio.shield(starting)
        except asyncio.CancelledError:
            # Popen may still be running in its executor thread; reclaim it.
            starting.add_done_callback(_discard_startup)
            raise
        try:
            await _await_ready(process)
        except (Exception, asyncio.CancelledError):
            # Reaping blocks for up to ~5s: never do it on the loop.
            await asyncio.to_thread(_reap_process, process)
            raise
        process.stdout = _BoundedStdout(process.stdout, MAX_WIRE_LINE_BYTES, self.stop)  # type: ignore[assignment]
        if not self._register_worker(process):
            # ``stop()`` landed while this worker was booting: reclaim it rather
            # than hand back a subprocess no owner will ever terminate.
            await asyncio.to_thread(_reap_process, process)
            raise _WorkerGone("Sandbox is shut down")
        return process

    def _register_worker(self, process: subprocess.Popen[bytes]) -> bool:
        """Publish a cold-started worker, unless this sandbox was stopped while it booted."""
        with self._state_lock:
            if self._closed:
                return False
            self._process = process
            return True


def _frame_answers(response: Any, request_id: str) -> bool:
    """True when the frame answers our request with a result object or an error string."""
    if not isinstance(response, dict) or response.get("id") != request_id:
        return False
    return isinstance(response.get("result"), dict) or isinstance(response.get("error"), str)


def _decode_response(line: bytes, request_id: str) -> dict[str, Any]:
    """Parse one answer frame off the wire and return its result half."""
    if not line:
        raise _WorkerGone("Sandbox process died unexpectedly")
    try:
        response = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _WorkerGone("Invalid sandbox response") from exc
    if not _frame_answers(response, request_id):
        raise _WorkerGone("Invalid sandbox response")
    error = response.get("error")
    if isinstance(error, str):
        raise RuntimeError(error)  # noqa: TRY004 - the worker's error text, surfaced for execute() to map
    return response["result"]
