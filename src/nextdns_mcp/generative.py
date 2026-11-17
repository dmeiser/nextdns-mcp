"""FastMCP GenerativeUI provider with NextDNS compatibility and sizing guards.

SPDX-License-Identifier: MIT
"""

import ast
import asyncio
import json
import logging
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.apps.config import AppConfig
from fastmcp.apps.generative import (
    UI_MIME_TYPE,
    GenerativeUI,
    _build_csp,
    _gen,
    app_config_to_meta_dict,
    get_generative_renderer_html,
)
from fastmcp.resources.types import TextResource
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolAnnotations
from prefab_ui.app import PrefabApp
from prefab_ui.sandbox import PyodideSandbox
from prefab_ui.sandbox import Sandbox as _Sandbox
from prefab_ui.sandbox._pyodide import DENO_LOCK, PREFAB_SRC, RUNNER_JS

logger = logging.getLogger(__name__)

SANDBOX_PRELUDE = """from prefab_ui.app import PrefabApp
from prefab_ui.components import *
from prefab_ui.components.charts import *
from prefab_ui.components.control_flow import *

# Component aliases for common React / shadcn hallucinated imports
TabsList = Div
TabsTrigger = Tab
TabsContent = Div
def DonutChart(*args, **kwargs):
    kwargs.setdefault("inner_radius", 50)
    return PieChart(*args, **kwargs)

# DataTable positional argument compatibility wrapper
_BaseDataTable = DataTable

class DataTable(_BaseDataTable):
    def __init__(self, columns=None, rows=None, /, **kwargs):
        if columns is not None:
            kwargs["columns"] = columns
        if rows is not None:
            kwargs["rows"] = rows
        super().__init__(**kwargs)
"""

MAX_CODE_BYTES = 32 * 1024
MAX_DATA_BYTES = 128 * 1024
MAX_RESULT_BYTES = 256 * 1024
EXECUTION_TIMEOUT = 10.0
# Cold-start (Deno + Pyodide boot) gets its own budget so it never consumes
# the user-code EXECUTION_TIMEOUT (issue #541).
SANDBOX_START_TIMEOUT = 60.0
# The sandbox renders component trees headlessly; 1 GiB is generous for that
# and bounds a runaway worker. The upstream default grants 4 GiB.
SANDBOX_V8_HEAP_MB = 1024

# Host environment variables matching any of these markers (case-insensitive)
# are never forwarded to the Deno sandbox subprocess. Generated code executes
# with Pyodide's full ``os`` module, so anything in the subprocess environment
# is readable from LLM-generated code; key material such as NEXTDNS_API_KEY
# must not cross that boundary.
_SENSITIVE_ENV_MARKERS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE",
    "AUTH",
)


def _sandbox_env() -> dict[str, str]:
    """Copy of the host environment with secret-bearing variables removed."""
    return {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in _SENSITIVE_ENV_MARKERS)
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


async def _read_startup_line(
    proc: subprocess.Popen[bytes],
    proc_stderr: Any,
    stderr_lines: list[str],
) -> str:
    """Read one startup stderr line, raising if the worker died before signalling."""
    loop = asyncio.get_event_loop()
    line = await loop.run_in_executor(None, proc_stderr.readline)
    if not line and proc.poll() is not None:
        raise _startup_error(stderr_lines)
    decoded = line.decode().rstrip()
    stderr_lines.append(decoded)
    return decoded


async def _await_ready(proc: subprocess.Popen[bytes]) -> None:
    """Drain the worker's stderr until it signals readiness, as upstream does."""
    assert proc.stderr is not None
    stderr_lines: list[str] = []
    while True:
        decoded = await _read_startup_line(proc, proc.stderr, stderr_lines)
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


# The sandbox worker answers each request with a single JSON line on stdout:
# {"id": "<32-hex>", "result": <wire>} (or {"id": ..., "error": ...}). The
# wire is serialized with JSON.stringify, so the raw line is the UTF-8 wire
# plus a small ASCII envelope — no escaping expansion. Bound the line just
# above MAX_RESULT_BYTES so a runaway generated view is rejected during the
# stdout read, before the host buffers, JSON-decodes, and validates it.
WIRE_LINE_SLACK = 4096
MAX_WIRE_LINE_BYTES = MAX_RESULT_BYTES + WIRE_LINE_SLACK
_READ_CHUNK_SIZE = 65536

GENERATE_DOC = (
    "Execute Prefab Python code in a sandbox and render the result.\n\n"
    "CRITICAL CHART SIZING RULE: NEVER place charts (AreaChart, LineChart, BarChart, PieChart) "
    "inside Tabs or Tab components. Tab panels initialize with 0px width, which causes Recharts "
    "to render completely blank/invisible. Always place charts in Column, Row, Grid, or Card "
    "containers outside of Tabs.\n\n"
)


def _decode_ui_json(data: str) -> Any:
    """Decode a JSON string, raising a clear validation error on failure."""
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in data parameter: {exc}") from exc


def _require_ui_object(value: Any, source: str) -> dict[str, Any]:
    """Ensure a decoded/passed value is a dict or None-compatible object."""
    if not isinstance(value, dict):
        raise ValueError(  # noqa: TRY004
            f"{source} must be a JSON object, got {type(value).__name__}"
        )
    return value


def _parse_ui_string(data: str) -> dict[str, Any] | None:
    """Parse a (possibly blank) JSON string into a dict or None."""
    if not data.strip():
        return None
    value = _decode_ui_json(data)
    if value is None:
        return None
    return _require_ui_object(value, "data parameter")


def _parse_ui_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse JSON string data or return dictionary."""
    if isinstance(data, str):
        return _parse_ui_string(data)
    if data is None or isinstance(data, dict):
        return data
    raise ValueError(f"data parameter must be a dict or JSON object string, got {type(data).__name__}")


def _result_size_bytes(result: Any) -> int:
    """Measure the serialized byte size of a sandbox result."""
    to_json = getattr(result, "to_json", None)
    if callable(to_json):
        try:
            return len(json.dumps(to_json()).encode("utf-8"))
        except (TypeError, ValueError):
            pass
    dump_json = getattr(result, "model_dump_json", None)
    if callable(dump_json):
        try:
            return len(dump_json().encode("utf-8"))
        except (TypeError, ValueError):
            pass
    return len(str(result).encode("utf-8"))


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


_CHART_NAMES = {"AreaChart", "LineChart", "BarChart", "PieChart", "DonutChart"}
_TAB_NAMES = {"Tab", "TabsTrigger"}


def _call_name(node: ast.AST) -> str | None:
    """Return the callee name for simple `Name(...)` calls, else None."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name):
        return None
    return node.func.id


def _is_tab_call(node: ast.AST) -> bool:
    return _call_name(node) in _TAB_NAMES


def _is_chart_call(node: ast.AST) -> bool:
    return _call_name(node) in _CHART_NAMES


def _call_children(node: ast.Call) -> list[ast.AST]:
    """Return a call's argument expressions, including keyword values."""
    children: list[ast.AST] = list(node.args)
    children.extend(keyword.value for keyword in node.keywords)
    return children


def _call_visits(node: ast.Call, in_tab: bool) -> list[tuple[ast.AST, bool]]:
    """Child visits for a call: a Tab(...) call opens tab scope for its arguments."""
    if _is_tab_call(node):
        return [(child, True) for child in _call_children(node)]
    return [(child, in_tab) for child in ast.iter_child_nodes(node)]


def _with_visits(node: ast.With | ast.AsyncWith, in_tab: bool) -> list[tuple[ast.AST, bool]]:
    """Child visits for a `with`: items keep the current scope; the body opens tab scope."""
    visits: list[tuple[ast.AST, bool]] = [(item.context_expr, in_tab) for item in node.items]
    body_scope = in_tab or any(_is_tab_call(item.context_expr) for item in node.items)
    visits.extend((stmt, body_scope) for stmt in node.body)
    return visits


def _node_visits(node: ast.AST, in_tab: bool) -> list[tuple[ast.AST, bool]]:
    """Return the (node, scope) pairs to visit for one node's children."""
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _with_visits(node, in_tab)
    if isinstance(node, ast.Call):
        return _call_visits(node, in_tab)
    return [(child, in_tab) for child in ast.iter_child_nodes(node)]


def _chart_in_tab(root: ast.AST) -> bool:
    """Return True if a chart call occurs inside a Tab/TabsTrigger subtree.

    Iterative stack walk: each `with` item's context expression is visited,
    each `with` body is visited once regardless of item count, and node depth
    costs no stack frames, so deeply nested expressions cannot raise
    RecursionError.
    """
    stack: list[tuple[ast.AST, bool]] = [(root, False)]
    while stack:
        node, in_tab = stack.pop()
        if in_tab and _is_chart_call(node):
            return True
        stack.extend(_node_visits(node, in_tab))
    return False


def _validate_chart_placement(code: str) -> None:
    """Reject nesting charts in tabs to avoid 0px container collapse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return
    if _chart_in_tab(tree):
        raise ValueError(
            "Recharts chart components (AreaChart, LineChart, BarChart, PieChart) cannot be placed "
            "inside Tab components because tab panels initialize with 0px width, causing charts to "
            "render completely invisible. Place the chart outside of Tabs in a Column, Row, Card, or Grid."
        )


def _build_renderer_resource(csp: Any) -> TextResource:
    """Build the Generative UI iframe renderer HTML resource."""
    resource_config = AppConfig(csp=csp)
    return TextResource(
        uri=_gen.RESOURCE_URI,  # type: ignore[arg-type]
        name="Prefab Generative Renderer",
        text=get_generative_renderer_html(),
        mime_type=UI_MIME_TYPE,
        meta={"ui": app_config_to_meta_dict(resource_config)},
    )


class NextDNSGenerativeUI(GenerativeUI):
    """GenerativeUI provider customized with pre-execution validation and Pyodide prelude."""

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Own this provider's sandbox for exactly one server runtime.

        FastMCP enters each provider's lifespan inside the lifespan of the
        server it was added to, so teardown here stops only this instance's
        subprocess - the server that is shutting down. Registering instances in
        a process-global set instead (issue #542) made one embedded server's
        shutdown kill the sandboxes of every live sibling.
        """
        self._ensure_setup()
        try:
            async with self._local.lifespan():
                yield
        finally:
            await self._stop_sandbox()

    def _get_sandbox(self) -> Any:
        """Lazily create the hardened, bounded Pyodide sandbox."""
        if self._sandbox is None:
            self._sandbox = _CombinedSandbox()
        return self._sandbox

    async def _stop_sandbox(self) -> None:
        """Stop the lazily-started Deno/Pyodide subprocess, off the event loop.

        ``Sandbox._stop()`` terminates the process and waits up to 5s for it,
        which is blocking I/O: it runs in a worker thread so a slow teardown
        cannot stall the rest of the server's shutdown.
        """
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            await asyncio.to_thread(sandbox._stop)
        except Exception:  # teardown is best-effort: it must not abort the rest of shutdown
            logger.warning("Failed to stop the Generative UI sandbox subprocess", exc_info=True)
        finally:
            self._sandbox = None

    def _create_generate_tool(self, app_config: AppConfig, annotations: ToolAnnotations) -> Tool:
        """Create the generate_prefab_ui tool with interceptor and prelude."""
        sandbox_ref = self

        async def generate_ui(
            code: str,
            data: str | dict[str, Any] | None = None,
        ) -> PrefabApp:
            if len(code.encode("utf-8")) > MAX_CODE_BYTES:
                raise ValueError(f"code exceeds maximum size of {MAX_CODE_BYTES} bytes")
            _validate_chart_placement(code)
            if isinstance(data, str):
                if len(data.encode("utf-8")) > MAX_DATA_BYTES:
                    raise ValueError(f"data exceeds maximum size of {MAX_DATA_BYTES} bytes")
                parsed_data = _parse_ui_data(data)
            else:
                # Validate the type before serializing so non-dict values raise
                # ValueError here instead of TypeError from json.dumps.
                parsed_data = _parse_ui_data(data)
                if parsed_data is not None and len(json.dumps(parsed_data).encode("utf-8")) > MAX_DATA_BYTES:
                    raise ValueError(f"data exceeds maximum size of {MAX_DATA_BYTES} bytes")
            full_code = f"{SANDBOX_PRELUDE}\n{code}"
            sandbox = sandbox_ref._get_sandbox()
            try:
                # Cold-start gets its own budget so Pyodide boot never
                # consumes the user-code EXECUTION_TIMEOUT (issue #541).
                try:
                    await asyncio.wait_for(sandbox.start(), timeout=SANDBOX_START_TIMEOUT)
                except TimeoutError:
                    raise TimeoutError(f"Pyodide sandbox cold start exceeded {SANDBOX_START_TIMEOUT}s") from None
                try:
                    result = await asyncio.wait_for(
                        _gen.execute(full_code, data=parsed_data, sandbox=sandbox),
                        timeout=EXECUTION_TIMEOUT,
                    )
                except TimeoutError:
                    # asyncio.wait_for cancels the inner task; PyodideSandbox.run handles
                    # CancelledError by stopping the worker, so the lock is released.
                    raise TimeoutError(f"generate_prefab_ui execution exceeded {EXECUTION_TIMEOUT}s") from None
            finally:
                sandbox.stop()
            result_size = _result_size_bytes(result)
            if result_size > MAX_RESULT_BYTES:
                raise ValueError(f"result exceeds maximum size of {MAX_RESULT_BYTES} bytes")
            return result

        doc = GENERATE_DOC + (_gen.execute.__doc__ or "")
        return Tool.from_function(
            generate_ui,
            name=self._tool_name,
            description=doc,
            annotations=annotations,
            meta={"ui": app_config_to_meta_dict(app_config)},
        )

    def _create_components_tool(self, annotations: ToolAnnotations) -> Tool:
        """Create the search_prefab_components tool."""
        doc = (
            (_gen.search_components.__doc__ or "")
            + "\n\nNote: Place charts (AreaChart, BarChart, LineChart, PieChart) outside Tabs so their container width is measurable."
        )
        return Tool.from_function(
            _gen.search_components,
            name=self._components_tool_name,
            description=doc,
            annotations=annotations,
        )

    def _ensure_setup(self) -> None:
        """Register Generative UI tools with annotations and sizing rule guardrails."""
        if self._setup_done:
            return

        csp = _build_csp()
        app_config = AppConfig(resource_uri=_gen.RESOURCE_URI)
        generate_annotations = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
        components_annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

        self._local._add_component(self._create_generate_tool(app_config, generate_annotations))
        if self._include_components_tool:
            self._local._add_component(self._create_components_tool(components_annotations))
        self._local._add_component(_build_renderer_resource(csp))
        self._setup_done = True
