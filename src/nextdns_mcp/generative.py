"""FastMCP GenerativeUI provider with NextDNS compatibility and sizing guards.

The sandbox worker lifecycle lives in ``generative_sandbox`` and the
chart-placement AST check in ``chart_placement``; this module owns the tool
factories that bind them to the MCP interface. The provider composes its own
components and its own worker instead of extending FastMCP's ``GenerativeUI``
and reaching into its private setup, registration and sandbox state — a rename
there would drop a guard at request time, invisibly to mypy.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import prefab_ui.generative as prefab_generative
from fastmcp.apps.config import AppConfig, ResourceCSP, app_config_to_meta_dict
from fastmcp.resources.base import Resource
from fastmcp.resources.types import TextResource
from fastmcp.server.providers.base import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolAnnotations
from fastmcp.utilities.mime import UI_MIME_TYPE
from prefab_ui.app import PrefabApp
from prefab_ui.renderer import get_generative_renderer_csp, get_generative_renderer_html

from .chart_placement import _validate_chart_placement
from .generative_sandbox import (
    EXECUTION_TIMEOUT,
    MAX_RESULT_BYTES,
    SANDBOX_START_TIMEOUT,
    DenoPyodideSandbox,
)

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


def _guard_string_data(data: str) -> dict[str, Any] | None:
    """Measure the JSON string as sent, then parse it."""
    if len(data.encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError(f"data exceeds maximum size of {MAX_DATA_BYTES} bytes")
    return _parse_ui_data(data)


def _guard_object_data(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the type before serializing so non-dict values raise ValueError, not TypeError from json.dumps."""
    parsed_data = _parse_ui_data(data)
    if parsed_data is None:
        return None
    if len(json.dumps(parsed_data).encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError(f"data exceeds maximum size of {MAX_DATA_BYTES} bytes")
    return parsed_data


def _validated_payload(code: str, data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply the code and data guards and return the parsed data payload."""
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError(f"code exceeds maximum size of {MAX_CODE_BYTES} bytes")
    _validate_chart_placement(code)
    if isinstance(data, str):
        return _guard_string_data(data)
    return _guard_object_data(data)


def _result_size_bytes(result: Any) -> int:
    """Measure the serialized byte size of a sandbox result.

    JSON is measured as UTF-8, not as ASCII-escaped text, so multibyte output is
    not charged six bytes per character. A serializer that fails is an error:
    falling back to ``str(result)`` let short representations sneak an
    otherwise unserializable result past the size guard.
    """
    to_json = getattr(result, "to_json", None)
    if callable(to_json):
        try:
            return len(json.dumps(to_json(), ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"result serialization via to_json failed: {exc}") from exc
    dump_json = getattr(result, "model_dump_json", None)
    if callable(dump_json):
        try:
            return len(dump_json().encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"result serialization via model_dump_json failed: {exc}") from exc
    return len(str(result).encode("utf-8"))


def _renderer_csp() -> ResourceCSP:
    """CSP the renderer resource advertises, read from the renderer's own declaration."""
    declared = get_generative_renderer_csp()
    return ResourceCSP(
        resource_domains=declared.get("resource_domains"),
        connect_domains=declared.get("connect_domains"),
    )


def _build_renderer_resource(csp: Any) -> TextResource:
    """Build the Generative UI iframe renderer HTML resource."""
    resource_config = AppConfig(csp=csp)
    return TextResource(
        uri=prefab_generative.RESOURCE_URI,  # type: ignore[arg-type]
        name="Prefab Generative Renderer",
        text=get_generative_renderer_html(),
        mime_type=UI_MIME_TYPE,
        meta={"ui": app_config_to_meta_dict(resource_config)},
    )


async def _start_within_budget(worker: DenoPyodideSandbox) -> None:
    """Cold-start the worker on its own budget so Pyodide boot cannot eat EXECUTION_TIMEOUT (#541)."""
    try:
        await asyncio.wait_for(worker.start(), timeout=SANDBOX_START_TIMEOUT)
    except TimeoutError:
        raise TimeoutError(f"Pyodide sandbox cold start exceeded {SANDBOX_START_TIMEOUT}s") from None


async def _execute_within_budget(code: str, data: dict[str, Any] | None, worker: DenoPyodideSandbox) -> PrefabApp:
    """Run the generated code on the execution timeout."""
    try:
        return await asyncio.wait_for(
            prefab_generative.execute(code, data=data, sandbox=worker),
            timeout=EXECUTION_TIMEOUT,
        )
    except TimeoutError:
        # asyncio.wait_for cancels the inner task; DenoPyodideSandbox.run
        # discards the worker on CancelledError, so the lock is released.
        raise TimeoutError(f"generate_prefab_ui execution exceeded {EXECUTION_TIMEOUT}s") from None


class PrefabGenerativeUI(Provider):
    """Generative UI provider for this server: guarded Prefab tools plus the renderer resource.

    Composition, not inheritance from FastMCP's ``GenerativeUI``: this class
    builds its own tools and resource, owns its own sandbox worker, and reaps
    that worker itself. What it uses from upstream is ``Provider``'s documented
    extension points (``_list_tools``, ``_list_resources``, ``lifespan``) and
    prefab-ui's public ``generative`` and ``renderer`` functions.

    Tool names, descriptions, annotations, the renderer resource, the size and
    placement guards, the separate cold-start and execution timeouts, and the
    per-server teardown are the contract this class owns.
    """

    def __init__(
        self,
        *,
        tool_name: str = "generate_prefab_ui",
        include_components_tool: bool = True,
        components_tool_name: str = "search_prefab_components",
    ) -> None:
        super().__init__()
        app_config = AppConfig(resource_uri=prefab_generative.RESOURCE_URI)
        self._tools: list[Tool] = [_build_generate_tool(tool_name, app_config, self._run_in_sandbox)]
        if include_components_tool:
            self._tools.append(_build_components_tool(components_tool_name))
        self._resources: list[TextResource] = [_build_renderer_resource(_renderer_csp())]
        self._worker: DenoPyodideSandbox | None = None

    async def _list_tools(self) -> Sequence[Tool]:
        return list(self._tools)

    async def _list_resources(self) -> Sequence[Resource]:
        return list(self._resources)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Own this provider's worker for exactly one server runtime.

        FastMCP enters each provider's lifespan inside the lifespan of the
        server it was added to, so teardown here stops only this instance's
        subprocess - the server that is shutting down. Registering instances in
        a process-global set instead (issue #542) made one embedded server's
        shutdown kill the sandboxes of every live sibling.
        """
        try:
            yield
        finally:
            await self._stop_worker()

    def _get_worker(self) -> DenoPyodideSandbox:
        """Lazily create the hardened, bounded Pyodide worker this provider owns."""
        if self._worker is None:
            self._worker = DenoPyodideSandbox()
        return self._worker

    async def _stop_worker(self) -> None:
        """Stop the lazily-started Deno/Pyodide subprocess, off the event loop.

        ``DenoPyodideSandbox.stop`` terminates the process and waits up to 5s
        for it, which is blocking I/O: it runs in a worker thread so a slow
        teardown cannot stall the rest of the server's shutdown.
        """
        worker = self._worker
        if worker is None:
            return
        try:
            await asyncio.to_thread(worker.stop)
        except Exception:  # teardown is best-effort: it must not abort the rest of shutdown
            logger.warning("Failed to stop the Generative UI sandbox subprocess", exc_info=True)
        finally:
            self._worker = None

    async def _run_in_sandbox(self, code: str, data: dict[str, Any] | None) -> PrefabApp:
        """Cold-start, execute, and always discard this provider's worker."""
        worker = self._get_worker()
        try:
            await _start_within_budget(worker)
            return await _execute_within_budget(code, data, worker)
        finally:
            # Reaping blocks for up to ~5s: never do it on the loop. This is the
            # safety net for a worker cold-started here but never executed.
            await asyncio.to_thread(worker.stop)


def _build_generate_tool(
    name: str, app_config: AppConfig, run: Callable[[str, dict[str, Any] | None], Awaitable[PrefabApp]]
) -> Tool:
    """The generate_prefab_ui tool: size guards and placement guard, then the sandbox run."""

    async def generate_ui(code: str, data: str | dict[str, Any] | None = None) -> PrefabApp:
        payload = _validated_payload(code, data)
        result = await run(f"{SANDBOX_PRELUDE}\n{code}", payload)
        result_size = _result_size_bytes(result)
        if result_size > MAX_RESULT_BYTES:
            raise ValueError(f"result exceeds maximum size of {MAX_RESULT_BYTES} bytes")
        return result

    return Tool.from_function(
        generate_ui,
        name=name,
        description=GENERATE_DOC + (prefab_generative.execute.__doc__ or ""),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
        meta={"ui": app_config_to_meta_dict(app_config)},
    )


def _build_components_tool(name: str) -> Tool:
    """The search_prefab_components tool."""
    doc = (
        (prefab_generative.search_components.__doc__ or "")
        + "\n\nNote: Place charts (AreaChart, BarChart, LineChart, PieChart) outside Tabs so their container width is measurable."
    )
    return Tool.from_function(
        prefab_generative.search_components,
        name=name,
        description=doc,
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    )
