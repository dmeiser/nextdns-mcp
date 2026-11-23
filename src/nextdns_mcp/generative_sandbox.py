"""Sandbox process management for the Generative UI provider.

Owns the Deno/Pyodide worker lifecycle: the sanitized environment, the
bounded V8 heap, the cold-start handshake, and the bounded stdout reader
that rejects oversized results during the wire read.

SPDX-License-Identifier: MIT
"""

import asyncio
import os
import shutil
import subprocess
from typing import Any

from prefab_ui.sandbox import PyodideSandbox
from prefab_ui.sandbox import Sandbox as _Sandbox
from prefab_ui.sandbox._pyodide import DENO_LOCK, PREFAB_SRC, RUNNER_JS

MAX_RESULT_BYTES = 256 * 1024
EXECUTION_TIMEOUT = 10.0
# Cold-start (Deno + Pyodide boot) gets its own budget so it never consumes
# the user-code EXECUTION_TIMEOUT (issue #541).
SANDBOX_START_TIMEOUT = 60.0
# The sandbox renders component trees headlessly; 1 GiB is generous for that
# and bounds a runaway worker. The upstream default grants 4 GiB.
SANDBOX_V8_HEAP_MB = 1024

# The sandbox worker answers each request with a single JSON line on stdout:
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
    return env


def _deno_command() -> list[str]:
    deno = shutil.which("deno")
    if deno is None:
        raise RuntimeError("Deno is required for the Prefab sandbox. Install it from https://deno.land")
    return [
        deno,
        "run",
        "--no-config",
        f"--lock={DENO_LOCK}",
        "--frozen",
        "--unstable-detect-cjs",
        "--no-prompt",
        "--allow-read",
        "--allow-write",
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


def _discard_startup(future: "asyncio.Future[subprocess.Popen[bytes]]") -> None:
    """Reclaim a worker whose start was cancelled before it completed."""
    if not future.cancelled() and future.exception() is None:
        PyodideSandbox._stop_process(future.result())


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


class HardenedPyodideSandbox(PyodideSandbox):
    """PyodideSandbox with host secrets stripped, a bounded V8 heap, and a fresh interpreter per execution.

    ``prefab_ui.sandbox.PyodideSandbox`` copies ``os.environ`` verbatim into the Deno
    subprocess (exposing ``NEXTDNS_API_KEY`` to generated code), grants the worker a 4 GiB
    V8 heap, and keeps one warm interpreter whose imported modules are shared across
    calls, so code from one ``generate_prefab_ui`` invocation can poison later ones.
    This subclass keeps the upstream subprocess protocol but vendors ``_start`` with a
    sanitized environment and a bounded heap, and discards the worker after every
    execution so no interpreter state survives between calls.
    """

    async def start(self) -> None:
        """Cold-start the worker, outside the user-code execution timeout."""
        async with self._lock:
            if self._process is None or self._process.poll() is not None:
                await self._start()

    def stop(self) -> None:
        """Discard the worker so the next execution gets a fresh interpreter."""
        self._stop()

    async def run(self, code: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute generated code, then discard the interpreter it ran on."""
        try:
            return await super().run(code, data=data)
        finally:
            self._stop()

    async def _start(self) -> None:
        # Vendored from PyodideSandbox._start (prefab-ui 0.21.1) with two changes:
        # the subprocess environment is sanitized and the V8 heap is bounded.
        # Keep in sync with prefab_ui.sandbox._pyodide on upgrades.
        starting = _spawn_deno(_sandbox_startup_env())
        try:
            proc = await asyncio.shield(starting)
        except asyncio.CancelledError:
            # Popen may still be running in its executor thread; reclaim it.
            starting.add_done_callback(_discard_startup)
            raise
        try:
            await _await_ready(proc)
        except (Exception, asyncio.CancelledError):
            PyodideSandbox._stop_process(proc)
            raise
        self._process = proc
        stdout = getattr(proc, "stdout", None)
        if stdout is None or isinstance(stdout, _BoundedStdout):
            return
        # Note: _start returns None; wrapping stdout here is best-effort.
        proc.stdout = _BoundedStdout(stdout, MAX_WIRE_LINE_BYTES, self._stop)  # type: ignore[assignment]


class _BoundedStdout:
    """File-like bounding each stdout line to ``limit`` bytes during the read.

    ``PyodideSandbox.run`` calls ``stdout.readline()`` with no cap, so a
    runaway generated view buffers unboundedly in the host before the result
    size check can run. This wrapper reads in chunks, preserves any bytes
    past the newline for the next response, and raises ``ValueError`` the
    moment a line exceeds ``limit`` — before JSON decode and
    ``PrefabApp.from_json`` allocation. ``on_overflow`` stops the sandbox so
    the next call starts a fresh worker (a partially consumed line would
    otherwise misalign the wire protocol).
    """

    def __init__(self, raw: Any, limit: int, on_overflow: Any) -> None:
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


class _BoundedResultSandbox(_Sandbox):
    """Sandbox whose worker stdout enforces MAX_RESULT_BYTES during the read.

    Wraps the worker process stdout in :class:`_BoundedStdout` after every
    start (including auto-restarts after a crash) so an oversized result is
    rejected while the response line is still being read — before the host
    buffers the whole line, JSON-decodes it, and allocates the validated
    ``PrefabApp``.
    """

    async def _start(self) -> None:
        await super()._start()
        proc = self._process
        if proc is None:
            return
        stdout = getattr(proc, "stdout", None)
        if stdout is None or isinstance(stdout, _BoundedStdout):
            return
        proc.stdout = _BoundedStdout(stdout, MAX_WIRE_LINE_BYTES, self._stop)  # type: ignore[assignment]


class _CombinedSandbox(HardenedPyodideSandbox, _BoundedResultSandbox):
    """Sandbox carrying both the hardened env/heap/isolation and the bounded stdout reader."""
