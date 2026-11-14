"""FastMCP GenerativeUI provider with NextDNS compatibility and sizing guards.

SPDX-License-Identifier: MIT
"""

import asyncio
import json
import re
from typing import Any
from weakref import WeakSet

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
EXECUTION_TIMEOUT = 10.0

CHART_IN_TAB_PATTERN = re.compile(r"with\s+Tab\b[\s\S]*?\b(AreaChart|LineChart|BarChart|PieChart|DonutChart)\b")

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


def _validate_chart_placement(code: str) -> None:
    """Reject nesting charts in tabs to avoid 0px container collapse."""
    if CHART_IN_TAB_PATTERN.search(code):
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


_ACTIVE_GENERATIVE_UI: "WeakSet[NextDNSGenerativeUI]" = WeakSet()


class NextDNSGenerativeUI(GenerativeUI):
    """GenerativeUI provider customized with pre-execution validation and Pyodide prelude."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _ACTIVE_GENERATIVE_UI.add(self)

    def close(self) -> None:
        """Stop the lazily-started Deno/Pyodide sandbox subprocess, if any."""
        sandbox = getattr(self, "_sandbox", None)
        if sandbox is not None:
            try:
                sandbox._stop()
            finally:
                self._sandbox = None

    def _create_generate_tool(self, app_config: AppConfig, annotations: ToolAnnotations) -> Tool:
        """Create the generate_prefab_ui tool with interceptor and prelude."""
        sandbox_ref = self

        async def generate_ui(
            code: str,
            data: str | dict[str, Any] | None = None,
        ) -> PrefabApp:
            _validate_chart_placement(code)
            if len(code.encode("utf-8")) > MAX_CODE_BYTES:
                raise ValueError(f"code exceeds maximum size of {MAX_CODE_BYTES} bytes")
            if data is not None:
                data_size = (
                    len(data.encode("utf-8")) if isinstance(data, str) else len(json.dumps(data).encode("utf-8"))
                )
                if data_size > MAX_DATA_BYTES:
                    raise ValueError(f"data exceeds maximum size of {MAX_DATA_BYTES} bytes")
            parsed_data = _parse_ui_data(data)
            full_code = f"{SANDBOX_PRELUDE}\n{code}"
            sandbox = sandbox_ref._get_sandbox()
            try:
                return await asyncio.wait_for(
                    _gen.execute(full_code, data=parsed_data, sandbox=sandbox),
                    timeout=EXECUTION_TIMEOUT,
                )
            except TimeoutError:
                # asyncio.wait_for cancels the inner task; PyodideSandbox.run handles
                # CancelledError by stopping the worker, so the lock is released.
                raise TimeoutError(f"generate_prefab_ui execution exceeded {EXECUTION_TIMEOUT}s") from None

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
        generate_annotations = ToolAnnotations(read_only_hint=False, destructive_hint=False)
        components_annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False)

        self._local._add_component(self._create_generate_tool(app_config, generate_annotations))
        if self._include_components_tool:
            self._local._add_component(self._create_components_tool(components_annotations))
        self._local._add_component(_build_renderer_resource(csp))
        self._setup_done = True


def close_all_generative_ui() -> None:
    """Stop every registered GenerativeUI sandbox (called on server shutdown)."""
    for provider in list(_ACTIVE_GENERATIVE_UI):
        try:
            provider.close()
        except Exception:  # noqa: BLE001, S110
            pass
    _ACTIVE_GENERATIVE_UI.clear()
