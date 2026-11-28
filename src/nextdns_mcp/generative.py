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
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import prefab_ui.generative as prefab_generative
from fastmcp.apps.config import AppConfig, ResourceCSP, app_config_to_meta_dict
from fastmcp.resources.base import Resource
from fastmcp.resources.types import TextResource
from fastmcp.server.providers.base import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolAnnotations, ToolResult
from fastmcp.utilities.mime import UI_MIME_TYPE
from prefab_ui.app import PrefabApp
from prefab_ui.renderer import get_generative_renderer_html

from .chart_placement import CHART_COMPONENT_NAMES, validate_chart_placement
from .errors import ErrorCode, error_payload
from .generative_sandbox import (
    EXECUTION_TIMEOUT,
    MAX_RESULT_BYTES,
    SANDBOX_START_TIMEOUT,
    DenoPyodideSandbox,
    _ResultTooLarge,
)
from .typed_error_tool import TypedArgumentTool

logger = logging.getLogger(__name__)

# Passive component allowlist exposed to generated code: every export of
# prefab_ui.components except Embed, whose un-sandboxed srcDoc iframe is
# active content. The prelude is ergonomics, not the security boundary —
# model code can import anything inside the sandbox — so
# _validate_generated_tree re-checks the returned tree server-side.
SANDBOX_PRELUDE = """from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    ERROR, EVENT, H1, H2, H3, H4, INDEX, ITEM, RESULT, STATE,
    Accordion, AccordionItem, Alert, AlertDescription, AlertTitle, Audio, Badge,
    BlockQuote, Button, ButtonGroup, Calendar, Card, CardContent, CardDescription,
    CardFooter, CardHeader, CardTitle, Carousel, Checkbox, ChoiceCard, Code, Column,
    Combobox, ComboboxGroup, ComboboxLabel, ComboboxOption, ComboboxSeparator,
    Component, Container, ContainerComponent, Dashboard, DashboardItem, DataTable,
    DataTableColumn, DatePicker, Dialog, Div, Dot, DropZone, ExpandableRow, Field,
    FieldContent, FieldDescription, FieldError, FieldTitle, Form, Grid, GridItem,
    Heading, Histogram, HoverCard, Icon, Image, Input, Kbd, Label, Large, Lead,
    Link, Loader, Markdown, Mermaid, Metric, Muted, P, Page, Pages, Popover,
    Progress, Radio, RadioGroup, Ring, Row, Rx, RxStr, Select, SelectGroup,
    SelectLabel, SelectOption, SelectSeparator, Separator, Slider, Slot, Small,
    Span, StatefulMixin, Svg, Switch, Tab, Table, TableBody, TableCaption,
    TableCell, TableFooter, TableHead, TableHeader, TableRow, Tabs, Text,
    Textarea, Tooltip, Video, defer, insert,
)
from prefab_ui.components.charts import (
    AreaChart, BarChart, ChartSeries, ChartValueFormat, LineChart, PieChart,
    RadarChart, RadialChart, ScatterChart, Sparkline, SparklineCurve,
    SparklineMode, SparklineVariant, WaterfallChart,
)
from prefab_ui.components.control_flow import Elif, Else, ForEach, If

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
MAX_JSON_DEPTH = 100
# How many renders may hold a live sandbox worker at once. Every render owns
# its own worker, so this bounds concurrent Deno subprocesses: a render paused
# in its multi-second reap holds only its own permit, never the renders behind
# it (issue #793).
MAX_CONCURRENT_SANDBOXES = 2

# The chart lists below come from the same constant the tab-placement validator
# reads, so guidance and validation name one set of components (WORK-369 / DRY-4).
_CHART_NAMES = ", ".join(CHART_COMPONENT_NAMES)
_CHART_PLACEMENT_NOTE = f"\n\nNote: Place charts ({_CHART_NAMES}) outside Tabs so their container width is measurable."

# Server-owned docs: upstream's execute() docstring names a ``components`` tool and
# a caller-supplied ``sandbox`` that this published tool does not take. Callers see
# this text, so it describes the two parameters the server actually exposes.
GENERATE_DOC = (
    "Execute Prefab Python code in a sandbox and render the result.\n\n"
    "Arguments:\n"
    "- `code`: Python code that builds a Prefab component tree. It runs in a Pyodide "
    "WASM sandbox; import everything you use, and use `PrefabApp` as the outermost "
    "container. The tool returns once the whole tree is built and validated: the "
    "renderer displays the completed result, it does not stream partial output.\n"
    "- `data`: optional dict, or a JSON object string, of values injected as global "
    "variables in the sandbox namespace. `data` is not client-side state; see the "
    "`PrefabApp(state=...)` note below.\n\n"
    "Use the `search_prefab_components` tool to look up available components, their "
    "import paths, argument names, and examples before writing code.\n\n"
    f"CRITICAL CHART SIZING RULE: NEVER place charts ({_CHART_NAMES}) "
    "inside Tabs or Tab components. Tab panels initialize with 0px width, which causes Recharts "
    "to render completely blank/invisible. Always place charts in Column, Row, Grid, or Card "
    "containers outside of Tabs.\n\n"
    "TAB COMPONENTS: Prefab's native form is a `Tabs` container whose children are `Tab` "
    "components, and each `Tab` holds its own panel content: `with Tabs():` wrapping "
    "`with Tab('General'):` blocks. `TabsList`, `TabsTrigger` and `TabsContent` are NOT available "
    "and code that uses them fails.\n\n"
    "DATA TABLE: always pass `columns` and `rows` by keyword, "
    "`DataTable(columns=[...], rows=[...])`; positional arguments are not supported.\n\n"
    "GENERATED UIs ARE DISPLAY-ONLY: Embed components, external stylesheets, inline CSS, "
    "MCP bridge actions (CallTool, SendMessage, UpdateContext, Fetch), non-https:// links, "
    "and state keys starting with '$' are rejected server-side.\n\n"
    "Charts live in `prefab_ui.components.charts`. Import and build them with their own "
    "`data` and `series` arguments, e.g. `BarChart(data=[...], "
    'series=[ChartSeries(data_key="rev", label="Revenue")], x_axis="month")`.\n\n'
    "INTERACTIVE UIs: client-side state is set by `PrefabApp(state=...)`, not by "
    "`data`, which only seeds Python globals in the sandbox. A value that comes from "
    "`data` still has to be handed to `state`: "
    '`with PrefabApp(state={"threshold": threshold}) as app:`. Then `.rx` on a '
    'stateful component binds it: `Text(f"{slider.rx}")` renders a client-side '
    "`{{ threshold }}` expression.\n\n"
    "Layout: prefer `Grid(columns=N, gap=4)` for equal-width panels; `Row` and `Column` "
    "accept `gap`, `align`, and `justify` props; Card sub-components (CardHeader, "
    "CardContent, CardFooter) carry their own padding.\n\n"
)


def _reject_json_constant(name: str) -> float:
    """Reject the ``NaN``/``Infinity`` spellings the default decoder accepts but ``JSON.parse`` does not."""
    raise ValueError(f"Invalid JSON in data parameter: {name} is not a valid JSON number")


def _decode_ui_json(data: str) -> Any:
    """Decode a JSON string, raising a clear validation error on failure.

    ``parse_constant`` closes the non-finite hole in the default decoder, and the
    recursion catch reports a decoder stack blowout — the C scanner recurses once
    per nesting level — as the same validation error the value walk reports.
    """
    try:
        return json.loads(data, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in data parameter: {exc}") from exc
    except RecursionError as exc:
        raise ValueError(f"data exceeds maximum nesting depth of {MAX_JSON_DEPTH}") from exc


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


def _json_children(node: Any) -> Iterable[Any]:
    """The values a JSON container holds; an empty tuple for a scalar.

    Tuples count as containers because ``json.dumps`` encodes them as arrays: a
    tuple chain reaching the serializer is just as deep as a list chain.
    """
    if isinstance(node, dict):
        return node.values()
    if isinstance(node, (list, tuple)):
        return node
    return ()


def _guard_json_number(node: Any) -> None:
    """Refuse a non-finite float, which the worker's ``JSON.parse`` cannot read."""
    if isinstance(node, float) and not math.isfinite(node):
        raise ValueError("data contains a number JSON.parse cannot read: NaN or Infinity")


def _guard_json_children(node: Any, level: int, active: set[int]) -> Iterable[Any]:
    """Return a node's children, refusing a too-deep or cyclic container first.

    ``active`` holds the ids of containers on the current descent path: a
    repeated id is a cycle, while an object shared by two sibling branches is
    fine because its id is released when that branch is left.
    """
    children = _json_children(node)
    if not children:
        return ()
    if level >= MAX_JSON_DEPTH:
        raise ValueError(f"data exceeds maximum nesting depth of {MAX_JSON_DEPTH}")
    ident = id(node)
    if ident in active:
        raise ValueError("data contains a circular reference")
    active.add(ident)
    return children


def _check_json_limits(value: Any) -> None:
    """Bound nesting depth and reject non-finite numbers and cycles in UI data.

    The walk is iterative and depth-first on purpose. Recursion would raise
    ``RecursionError`` at exactly the depth it exists to report; breadth-first
    traversal would queue an entire generation before checking it, which a
    branching cycle can grow without bound. The stack holds only the current
    path plus its siblings, and an active-path id set rejects a cycle as soon
    as a branch reaches an ancestor while still allowing shared subobjects.
    Depth is counted in container levels — the document root is level 1, and a
    value may sit no deeper than level ``MAX_JSON_DEPTH``. Both limits protect
    the worker: the runner parses this document with ``JSON.parse``, which
    rejects ``NaN``/``Infinity`` outright and deepens its own parse stack with
    every level. The string path rejects non-finite numbers at parse time; a
    dict the host handed over never passed a parser, so the value itself is
    walked here.
    """
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 1, False)]
    while stack:
        node, level, leaving = stack.pop()
        if leaving:
            active.discard(id(node))
            continue
        _guard_json_number(node)
        children = _guard_json_children(node, level, active)
        if children:
            stack.append((node, level, True))
            stack.extend((child, level + 1, False) for child in reversed(tuple(children)))


def _parse_ui_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse JSON string data or return dictionary."""
    if isinstance(data, str):
        parsed = _parse_ui_string(data)
    elif data is None or isinstance(data, dict):
        parsed = data
    else:
        raise ValueError(f"data parameter must be a dict or JSON object string, got {type(data).__name__}")
    if parsed is not None:
        _check_json_limits(parsed)
    return parsed


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
    if len(json.dumps(parsed_data, allow_nan=False).encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError(f"data exceeds maximum size of {MAX_DATA_BYTES} bytes")
    return parsed_data


def _validated_payload(code: str, data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply the code and data guards and return the parsed data payload."""
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError(f"code exceeds maximum size of {MAX_CODE_BYTES} bytes")
    validate_chart_placement(code)
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


# Wire shapes a generated tree may never carry (SEC-3, nextdns-mcp#749).
# prefab_generative.execute returns PrefabApp.from_json, which builds the app
# with model_construct and skips every pydantic guard, so the renderer would
# otherwise receive whatever the model-authored code produced.
_BLOCKED_COMPONENT_TYPES = frozenset({"Embed"})
_BLOCKED_ACTIONS = frozenset({"toolCall", "sendMessage", "updateContext", "fetch"})
_OPEN_LINK_ACTION = "openLink"
_LINK_COMPONENT_TYPE = "Link"
_RESERVED_STATE_PREFIX = "$"


def _reject_tree(reason: str) -> None:
    raise ValueError(f"Generated UI rejected: {reason}")


def _is_reserved_state_key(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_RESERVED_STATE_PREFIX)


def _require_https_url(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value.strip().lower().startswith("https://"):
        _reject_tree(f"{context} must be an https:// URL, got {value!r}")


def _reject_reserved_key(value: Any, context: str) -> None:
    if _is_reserved_state_key(value):
        _reject_tree(f"{context} {value!r} uses the reserved '$' state-key prefix")


def _check_reserved_state_map(state: Any) -> None:
    if isinstance(state, dict) and any(_is_reserved_state_key(key) for key in state):
        _reject_tree("initial state uses a reserved '$'-prefixed state key")


def _check_component_type(node: dict[str, Any]) -> None:
    node_type = node.get("type")
    if not isinstance(node_type, str):
        # Data rows and state values may carry list/dict "type" fields (e.g.
        # DNS record-type filters); only string component types are checkable,
        # and the renderer's own schema rejects non-string component types.
        return
    if node_type in _BLOCKED_COMPONENT_TYPES:
        _reject_tree(f"component type {node_type!r} is not allowed in generated UIs")


def _check_link_target(node: dict[str, Any]) -> None:
    if node.get("type") == _LINK_COMPONENT_TYPE and node.get("href") is not None:
        _require_https_url(node.get("href"), "Link href")


def _check_action(node: dict[str, Any]) -> None:
    action = node.get("action")
    if not isinstance(action, str):
        return
    if action in _BLOCKED_ACTIONS:
        _reject_tree(f"action {action!r} is not allowed in generated UIs")
    if action == _OPEN_LINK_ACTION:
        _require_https_url(node.get("url"), "openLink action url")
    _reject_reserved_key(node.get("key"), f"{action} action state key")


def _check_state_key_name(node: dict[str, Any]) -> None:
    node_type = node.get("type")
    if node_type is not None:
        _reject_reserved_key(node.get("name"), f"{node_type} component state key")


def _check_wire_node(node: dict[str, Any]) -> None:
    _check_component_type(node)
    _check_action(node)
    _check_link_target(node)
    _check_state_key_name(node)
    _check_reserved_state_map(node.get("state"))


def _walk_wire_tree(wire: dict[str, Any]) -> None:
    """Visit every dict/list in the wire tree iteratively (no recursion)."""
    stack: list[Any] = [wire]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            _check_wire_node(node)
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def _wire_dict(result: Any) -> dict[str, Any]:
    to_json = getattr(result, "to_json", None)
    wire = to_json() if callable(to_json) else None
    if not isinstance(wire, dict):
        raise ValueError(  # noqa: TRY004
            "generate_prefab_ui expected the sandbox to return a Prefab app"
        )
    return wire


def _reject_app_level_assets(wire: dict[str, Any]) -> None:
    if wire.get("stylesheets"):
        _reject_tree("external stylesheets are not allowed in generated UIs")
    if wire.get("css"):
        _reject_tree("inline CSS is not allowed in generated UIs")


def _validate_generated_tree(result: Any) -> None:
    """Reject active or unvalidated content in a sandbox-built Prefab tree.

    The sandbox hands back ``PrefabApp.from_json`` output, which is built with
    ``model_construct`` and bypasses pydantic validation, so this gate checks
    the serialized wire tree — iteratively, before the result leaves the
    server — for the shapes the renderer would otherwise execute or embed:
    MCP bridge actions (``toolCall``, ``sendMessage``, ``updateContext``,
    ``fetch``), ``Embed`` iframes, non-HTTPS ``Link``/``openLink`` targets,
    injected stylesheets/CSS (including theme CSS, which serializes into
    ``css``), and reserved ``$``-prefixed state keys.
    """
    wire = _wire_dict(result)
    _reject_app_level_assets(wire)
    _walk_wire_tree(wire)


def _renderer_csp() -> ResourceCSP:
    """CSP for the bundled renderer: generated Python runs server-side, not here.

    The bundle carries a browser-side Pyodide streaming bridge, but no external
    origin is needed to display a completed result. An empty CSP keeps that
    bridge from fetching its loader, so partial tool input cannot execute Python
    in the browser while the guarded Deno sandbox remains the only executor.
    """
    return ResourceCSP(
        resource_domains=[],
        connect_domains=[],
        frame_domains=[],
        base_uri_domains=[],
    )


def _build_renderer_resource(csp: Any) -> TextResource:
    """Build the Generative UI iframe renderer HTML resource."""
    resource_config = AppConfig(csp=csp)
    return TextResource(
        uri=prefab_generative.RESOURCE_URI,  # type: ignore[arg-type]
        name="Prefab Generative Renderer",
        text=get_generative_renderer_html(mode="bundled"),
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
    builds its own tools and resource, starts a sandbox worker per render, and
    reaps those workers itself. What it uses from upstream is ``Provider``'s
    documented extension points (``_list_tools``, ``_list_resources``,
    ``lifespan``) and prefab-ui's public ``generative`` and ``renderer``
    functions.

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
        # Every worker a render started and has not reaped yet. Per-render
        # ownership is the point: a shared worker let one render's cleanup stop
        # another render's worker mid-flight (issue #746). The set is instance
        # state, so one server's teardown still reaps only its own subprocesses.
        self._active_workers: set[DenoPyodideSandbox] = set()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_SANDBOXES)

    async def _list_tools(self) -> Sequence[Tool]:
        return list(self._tools)

    async def _list_resources(self) -> Sequence[Resource]:
        return list(self._resources)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Own every worker this provider starts for exactly one server runtime.

        FastMCP enters each provider's lifespan inside the lifespan of the
        server it was added to, so teardown here stops only this instance's
        subprocesses - the server that is shutting down. Registering instances in
        a process-global set instead (issue #542) made one embedded server's
        shutdown kill the sandboxes of every live sibling.
        """
        try:
            yield
        finally:
            await self._stop_workers()

    def _new_worker(self) -> DenoPyodideSandbox:
        """Create a hardened, bounded Pyodide worker owned by exactly one render.

        Registered in ``_active_workers`` so server shutdown still reaps a
        render that is in flight, and dropped by that render as soon as it ends.
        """
        worker = DenoPyodideSandbox()
        self._active_workers.add(worker)
        return worker

    async def _stop_workers(self) -> None:
        """Stop every worker this provider still owns, off the event loop.

        ``DenoPyodideSandbox.stop`` terminates the process and waits up to 5s
        for it, which is blocking I/O: it runs in a worker thread so a slow
        teardown cannot stall the rest of the server's shutdown. It is also what
        closes a sandbox whose cold start is still in flight, so that start
        reclaims its own worker instead of leaving it running (#746).
        """
        workers, self._active_workers = self._active_workers, set()
        for worker in workers:
            try:
                await asyncio.to_thread(worker.stop)
            except Exception:  # teardown is best-effort: it must not abort the rest of shutdown
                logger.warning("Failed to stop the Generative UI sandbox subprocess", exc_info=True)

    async def _run_in_sandbox(self, code: str, data: dict[str, Any] | None) -> PrefabApp:
        """Cold-start, execute, and discard a worker this call owns alone.

        Cleanup for one render can then never stop another render's worker: the
        worker this call stops is the only one this call has. The semaphore is
        held through the reap, which bounds the live subprocess count while
        rendering itself stays concurrent (issue #793).
        """
        async with self._semaphore:
            worker = self._new_worker()
            try:
                await _start_within_budget(worker)
                return await _execute_within_budget(code, data, worker)
            finally:
                self._active_workers.discard(worker)
                # Reaping blocks for up to ~5s: never do it on the loop. This is
                # the safety net for a worker cold-started but never executed.
                await asyncio.to_thread(worker.stop)


async def _run_guarded(
    run: Callable[[str, dict[str, Any] | None], Awaitable[PrefabApp]], code: str, payload: dict[str, Any] | None
) -> PrefabApp | ToolResult:
    """Execute validated code in the sandbox, mapping every failure class to the typed contract.

    The sandbox surfaces a cold start or execution timeout as ``TimeoutError``,
    an answer past the wire cap as ``_ResultTooLarge``, and a dead worker, a
    missing Deno, a failed spawn, a serializer failure, or the generated
    code's own failure as ``RuntimeError``/``ValueError``/``OSError`` — each
    becomes the flagged typed payload callers branch on instead of a bare
    exception FastMCP renders as text. The runtime classes are logged:
    returning a payload instead of raising means FastMCP's own error logging
    no longer sees them.
    """
    try:
        result = await run(f"{SANDBOX_PRELUDE}\n{code}", payload)
        result_size = _result_size_bytes(result)
    except TimeoutError as exc:
        logger.warning("generate_prefab_ui timed out: %s", exc)
        return _generation_error(ErrorCode.TIMEOUT, str(exc))
    except _ResultTooLarge as exc:
        return _generation_error(ErrorCode.RESPONSE_TOO_LARGE, str(exc))
    except (RuntimeError, ValueError, OSError) as exc:
        logger.warning("generate_prefab_ui execution failed: %s", exc, exc_info=True)
        return _generation_error(ErrorCode.INTERNAL_ERROR, str(exc))
    if result_size > MAX_RESULT_BYTES:
        return _generation_error(
            ErrorCode.RESPONSE_TOO_LARGE, f"result exceeds maximum size of {MAX_RESULT_BYTES} bytes"
        )
    return result


def _generation_error(code: str, message: str) -> ToolResult:
    """A generation failure as the typed, flagged error payload callers branch on.

    A raised exception reaches the caller as bare text with no structured
    content; the server's error contract is ``{"error": ..., "code": ...}``
    with ``isError`` set, so every refusal and failure here answers that shape.
    """
    return ToolResult(structured_content=error_payload(code, message), is_error=True)


def _build_generate_tool(
    name: str, app_config: AppConfig, run: Callable[[str, dict[str, Any] | None], Awaitable[PrefabApp]]
) -> Tool:
    """The generate_prefab_ui tool: size and placement guards, the sandbox run, then tree validation."""

    async def generate_ui(code: str, data: str | dict[str, Any] | None = None) -> PrefabApp | ToolResult:
        try:
            payload = _validated_payload(code, data)
        except ValueError as exc:
            return _generation_error(ErrorCode.INVALID_ARGUMENT, str(exc))
        result = await _run_guarded(run, code, payload)
        if isinstance(result, ToolResult):
            return result
        _validate_generated_tree(result)
        return result

    # A plain Tool.from_function would let FastMCP reject a wrong-typed
    # argument (e.g. data=["value"]) before generate_ui could answer it, so the
    # caller would get a bare ValidationError with structured_content=None.
    # The subclass converts the signature's refusals into the typed payload;
    # the published input schema is still built from the same signature.
    return TypedArgumentTool.from_function(
        generate_ui,
        name=name,
        description=GENERATE_DOC,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
        meta={"ui": app_config_to_meta_dict(app_config)},
    )


def _build_components_tool(name: str) -> Tool:
    """The search_prefab_components tool.

    A server-owned wrapper publishes only the caller's three parameters:
    upstream's ``components`` argument is test dependency injection, and a
    client that supplied a registry would never reach the sandbox the search
    actually runs in.
    """

    def search_prefab_components(query: str = "", detail: bool | None = None, limit: int | None = None) -> str:
        """Search the Prefab component library (see the tool description)."""
        return prefab_generative.search_components(query, detail=detail, limit=limit)

    doc = (prefab_generative.search_components.__doc__ or "") + _CHART_PLACEMENT_NOTE
    return Tool.from_function(
        search_prefab_components,
        name=name,
        description=doc,
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    )
