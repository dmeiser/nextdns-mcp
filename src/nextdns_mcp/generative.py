"""FastMCP GenerativeUI provider with NextDNS compatibility and sizing guards.

The sandbox worker lifecycle lives in ``generative_sandbox`` and the
chart-placement AST check in ``chart_placement``; this module owns the tool
factories that bind them to the MCP interface.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import logging
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

from .chart_placement import _validate_chart_placement
from .generative_sandbox import (
    EXECUTION_TIMEOUT,
    MAX_RESULT_BYTES,
    SANDBOX_START_TIMEOUT,
    _CombinedSandbox,
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
