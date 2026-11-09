"""FastMCP GenerativeUI provider with NextDNS compatibility and sizing guards.

SPDX-License-Identifier: MIT
"""

import json
import re
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

CHART_IN_TAB_PATTERN = re.compile(r"with\s+Tab\b[\s\S]*?\b(AreaChart|LineChart|BarChart|PieChart|DonutChart)\b")

GENERATE_DOC = (
    "Execute Prefab Python code in a sandbox and render the result.\n\n"
    "CRITICAL CHART SIZING RULE: NEVER place charts (AreaChart, LineChart, BarChart, PieChart) "
    "inside Tabs or Tab components. Tab panels initialize with 0px width, which causes Recharts "
    "to render completely blank/invisible. Always place charts in Column, Row, Grid, or Card "
    "containers outside of Tabs.\n\n"
)


def _parse_ui_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse JSON string data or return dictionary."""
    if isinstance(data, str):
        return json.loads(data) if data.strip() else None
    return data


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


class NextDNSGenerativeUI(GenerativeUI):
    """GenerativeUI provider customized with pre-execution validation and Pyodide prelude."""

    def _create_generate_tool(self, app_config: AppConfig, annotations: ToolAnnotations) -> Tool:
        """Create the generate_prefab_ui tool with interceptor and prelude."""
        sandbox_ref = self

        async def generate_ui(
            code: str,
            data: str | dict[str, Any] | None = None,
        ) -> PrefabApp:
            _validate_chart_placement(code)
            parsed_data = _parse_ui_data(data)
            full_code = f"{SANDBOX_PRELUDE}\n{code}"
            return await _gen.execute(
                full_code,
                data=parsed_data,
                sandbox=sandbox_ref._get_sandbox(),
            )

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
        annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False)

        self._local._add_component(self._create_generate_tool(app_config, annotations))
        if self._include_components_tool:
            self._local._add_component(self._create_components_tool(annotations))
        self._local._add_component(_build_renderer_resource(csp))
        self._setup_done = True
