# ---
# Extra Field Relaxation for MCP Tool Arguments
#
# AI clients (like OpenAI) often send extra/unknown fields with tool calls.
# StripExtraFieldsMiddleware intercepts tool calls and filters arguments to
# only include fields defined in the tool's schema, operating at the MCP call
# level so that unknown fields are dropped (not rejected) while required/typed
# fields are still validated. The dropped names are not lost: the result
# carries them under ``ignored_arguments`` with a hint naming the tool that
# actually declares each, so a caller learns its real intent was discarded
# instead of believing the call applied it.
#
# See docs/troubleshooting.md for details.
"""MCP server creation.

The tool catalog is generated from the shipped NextDNS OpenAPI document
(``spec_tools``): one tool per operation, named by its ``operationId``. FastMCP's
own OpenAPI *transport* is deliberately not used — it sends through an
``httpx2.AsyncClient`` of its own, which would bypass this project's
``AccessControlledClient`` (profile allowlists, read-only mode, the
protected-profile denylist) and raise away the upstream body that the typed error
payloads are built from. What is used is FastMCP's OpenAPI *parser*, which turns
the document into the names, descriptions and parameter schemas; the request then
goes through this project's own client.

That distinction is the whole answer to the two historical objections to
spec-based generation here (issues #141 and #146): the earlier attempt used
``FastMCP.from_openapi``, whose generated tools were both stripped from the server
and incompatible with ``httpx.AsyncClient``, so the server went back to hand-written
tools. Parsing the document and owning the transport keeps the single source of truth
without either problem.

See docs/troubleshooting.md for the extra-field relaxation described above.

SPDX-License-Identifier: MIT
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

import httpx
import mcp.types
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

from .call_log import CallLogMiddleware, call_log_path
from .client import close_api_client, get_api_client
from .config import ConfigurationError, get_api_key, get_default_profile, get_tool_search_mode
from .constants import SYNTHETIC_TOOL_NAMES
from .spec_tools import endpoints_for_routes
from .tool_search import CallToolShapeMiddleware, build_search_transform
from .tools.doh import close_doh_client
from .tools.logs import _download_registry, cleanup_successful_downloads
from .usage import server_instructions
from .utils import _BARE_NEGATIVE_RE, _NOW_OFFSET_RE, is_float_shaped, is_integer_shaped

logger = logging.getLogger(__name__)

# --- /health readiness probe tuning -----------------------------------------

HEALTH_PROBE_PATH = "/profiles"
"""Cheapest authenticated read on the NextDNS API.

``GET /profiles`` returns the caller's profile list, so a 2xx proves the API
key is accepted without touching (or mutating) any specific profile."""

HEALTH_PROBE_TIMEOUT = 5.0
"""Seconds allowed for the readiness probe before NextDNS is called unreachable."""

HEALTH_CACHE_TTL = 30.0
"""Seconds a probe result is reused, so health polling cannot hammer the API."""

HEALTH_FAILURE_AUTH = "auth"
"""Failure class: NextDNS rejected the configured credentials, or no usable API key is
configured locally (missing or empty NEXTDNS_API_KEY, unreadable NEXTDNS_API_KEY_FILE)."""

HEALTH_FAILURE_UNREACHABLE = "unreachable"
"""Failure class: NextDNS could not be probed, or answered unusably."""


class HealthFailure(NamedTuple):
    """A failed readiness probe.

    Attributes:
        failure_class: ``"auth"`` or ``"unreachable"``.
        reason: Short operator-facing explanation. Built only from the HTTP
            status code, the exception type name, and configuration state, so it
            can never carry API key material, auth headers, or the upstream
            response body.
    """

    failure_class: str
    reason: str


# Cached probe result as (monotonic timestamp, failure or None when healthy).
_health_probe_cache: tuple[float, HealthFailure | None] | None = None

# Guards probe *construction*, not cache reads: concurrent ``/health`` pollers
# that all miss an empty (or just-expired) cache used to each fire their own
# upstream request - a thundering herd against the cheapest authenticated
# endpoint, which is exactly what the TTL cache exists to prevent (the cache
# bounds steady-state polling, not the simultaneous first miss). The lock
# makes the probe single-flight: one request goes upstream, everyone else
# waits on it and reads the filled cache.
_health_probe_lock = asyncio.Lock()


def _tool_property_names(tool: Any) -> list[str]:
    return [str(prop) for prop in (tool.parameters or {}).get("properties") or {}]


async def _catalog_argument_index(fastmcp_server: FastMCP, tool_names: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Each argument name mapped to the registered tools that declare it.

    Enumerated BY NAME (the server's own registration list, passed in) rather
    than by ``list_tools()``: under the default BM25 disclosure the registry
    listing - with middleware or without it - answers only with the synthetic
    pair, so a listing-built index is empty and every dropped argument would
    read "matched no single other tool", exactly in the mode where callers
    most need the owner hint. ``get_tool`` resolves the real tool either way
    (it is how the call itself dispatches). The composite operations
    (``plotAnalytics``, ``dohLookup``) declare arguments too and live outside
    the spec surface, so the name list is the server's, not the spec's - and
    ``openapi`` keeps no import edge into ``spec_tools`` for it. Synthetic
    entries are excluded: nobody should be told "these are parameters of
    call_tool".
    """
    index: dict[str, set[str]] = {}
    for name in tool_names:
        tool = None if name in SYNTHETIC_TOOL_NAMES else await fastmcp_server.get_tool(name)
        if tool is None:
            continue
        for prop in _tool_property_names(tool):
            index.setdefault(prop, set()).add(name)
    return _freeze_owner_index(index)


def _freeze_owner_index(index: dict[str, set[str]]) -> dict[str, tuple[str, ...]]:
    """The index in its served form: sorted owner tuples, stable for tests and diffs."""
    return {name: tuple(sorted(owners)) for name, owners in index.items()}


def _group_owners(
    ignored: list[str], tool_name: str, index: dict[str, tuple[str, ...]]
) -> tuple[dict[str, list[str]], list[str]]:
    """Group dropped arguments by the single other tool that declares each.

    Returns (grouped, unowned): an argument exactly one other tool declares is
    grouped under it; one no single other tool declares goes to unowned.
    """
    grouped: dict[str, list[str]] = {}
    unowned: list[str] = []
    for arg in ignored:
        owners = [op for op in index.get(arg, ()) if op != tool_name]
        if len(owners) == 1:
            grouped.setdefault(owners[0], []).append(arg)
        else:
            unowned.append(arg)
    return grouped, unowned


def _render_owner_hint(base: str, grouped: dict[str, list[str]], unowned: list[str]) -> str:
    """The actionable half of the owner annotation: who owns what, and what to do."""
    where = "; ".join(f"{', '.join(args)} are parameters of {tool}" for tool, args in sorted(grouped.items()))
    tail = " — call it with them" if len(grouped) == 1 else ": call each named tool with its own arguments"
    if unowned:
        where += f"; {', '.join(unowned)} matched no single other tool and was dropped too"
    return f"{base}. {where}{tail}"


def _owner_hint(ignored: list[str], tool_name: str, index: dict[str, tuple[str, ...]]) -> str:
    """Name the tool that actually declares each dropped argument.

    'check the parameter list' left the caller to rediscover what it had
    already held: a model that sent updateSettings the keys belonging to
    updateSecuritySettings read the generic annotation as success and reported
    the wrong endpoint changed (live, target-model run). Naming the owning
    tool turns the ignored-arguments annotation into a correction the caller
    can act on; arguments no single other tool declares stay a generic note.
    """
    grouped, unowned = _group_owners(ignored, tool_name, index)
    base = "these arguments are not parameters of this tool and were not applied"
    if not grouped:
        return base + "; check the tool's parameter list before assuming the change happened"
    return _render_owner_hint(base, grouped, unowned)


def _report_ignored_arguments(
    result: ToolResult,
    ignored: list[str],
    hint: str,
    hint_for: Callable[[list[str]], str] | None = None,
) -> None:
    """Name the arguments the relaxation dropped instead of dropping them silently.

    Dropping unknown top-level arguments is deliberate (AI clients send them),
    but a *silent* drop is how a model ends up believing a call that carried
    ``privacy={...}`` actually changed privacy settings: the tool then answers
    "name is required for update operation" and the model never learns its real
    intent was discarded. The call still succeeds; the result just says which
    arguments were not applied.

    The result's own ``hint`` is never clobbered. A payload that already
    carries a hint - the profile-id shape hint, a 404 diagnosis - is the
    tool's answer to what *was* applied; the ignored-arguments annotation is
    appended to it, and the full owner text stays available under
    ``ignored_arguments_hint`` even when the merged hint had to be compact.

    ``hint_for`` (when the caller can re-derive owner text for an arbitrary
    name set) makes the merge re-render the owner attribution for the FULL
    merged set instead of concatenating the two passes' sentences: a name
    the inner pass and the outer pass both dropped would otherwise be
    attributed twice.
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        _annotate_content_only(result, ignored, hint)
        return
    # Merge, never overwrite: under the search transform an inner call reports
    # the names dropped from the target's own arguments, and the outer wrapper
    # pass reports the names dropped from the wrapper it unpacked. Both are
    # true about the same call; overwriting hides one pass behind the other.
    already = structured.get("ignored_arguments")
    prior_names = already if isinstance(already, list) else []
    merged = list(dict.fromkeys([*prior_names, *ignored]))
    structured["ignored_arguments"] = merged
    if hint_for is not None and prior_names:
        _rerender_owner_annotation(structured, merged, hint_for)
        return
    _merge_ignored_hints(structured, hint)


def _rerender_owner_annotation(
    structured: dict[str, Any], merged: list[str], hint_for: Callable[[list[str]], str]
) -> None:
    """Replace the earlier owner annotation with one rendering of the union.

    The tool's own base hint survives untouched; only the annotation this
    mechanism owns is swapped, so the result carries one owner sentence for
    all dropped names rather than two overlapping ones.
    """
    owner_text = hint_for(merged)
    prior_hint = structured.get("ignored_arguments_hint")
    structured["ignored_arguments_hint"] = owner_text
    base = structured.get("hint", "")
    if not isinstance(base, str):  # a payload with a non-string hint gets re-set, not crashed on
        base = ""
    if isinstance(prior_hint, str) and prior_hint:
        base = base.replace(prior_hint, "").strip(" |")
    structured["hint"] = f"{base} | {owner_text}" if base else owner_text


def _merge_ignored_hints(structured: dict[str, Any], hint: str) -> None:
    """Keep the inner pass's owner text beside the outer pass's, then merge into the tool hint."""
    prior_hint = structured.get("ignored_arguments_hint")
    if isinstance(prior_hint, str) and prior_hint and prior_hint != hint:
        hint = f"{prior_hint} | {hint}"
    structured["ignored_arguments_hint"] = hint
    _merge_result_hint(structured, hint)


def _annotate_content_only(result: ToolResult, ignored: list[str], hint: str) -> None:
    """A content-only result has no structured payload to annotate: say it in the content.

    plotAnalytics answers with image content and no structured dict; without
    this the argument drop would be silent for exactly the tool whose names
    (from/to on the series endpoints) differ from this one's
    (from_time/to_time).
    """
    if not ignored:
        return
    try:
        result.content.append(
            mcp.types.TextContent(
                type="text",
                text=f"Note: the server ignored argument(s) {', '.join(ignored)}; {hint}",
            )
        )
    except Exception:
        logger.debug("ignored-arguments annotation could not be appended", exc_info=True)


def _merge_result_hint(structured: dict[str, Any], hint: str) -> None:
    """Append the annotation hint to the tool's own hint without duplicating it."""
    existing = structured.get("hint")
    if isinstance(existing, str) and existing:
        if hint not in existing:
            structured["hint"] = f"{existing} | {hint}"
    else:
        structured["hint"] = hint


def _schema_declared_types(schema: dict[str, Any]) -> set[str]:
    """The types a schema node declares directly: ``type`` as a string or list."""
    type_value = schema.get("type")
    if isinstance(type_value, str):
        return {type_value}
    if isinstance(type_value, list):
        return set(type_value)
    return set()


def _cast_or_original(cast: Callable[[str], Any], s: str) -> Any:
    """Apply a numeric cast; a ValueError leaves the original string unchanged."""
    try:
        return cast(s)
    except ValueError:
        return s


def _coerce_bool_value(s: str, schema_types: set[str]) -> tuple[bool, Any]:
    """(handled, value) for a schema that explicitly expects a boolean."""
    lowered = s.lower()
    if "boolean" in schema_types and lowered in ("true", "false"):
        return True, lowered == "true"
    return False, s


def _coerce_int_value(s: str, schema_types: set[str]) -> tuple[bool, Any]:
    """(handled, value) for a schema that explicitly expects an integer.

    ``is_integer_shaped`` already ran ``int(s)`` and rejected anything it
    cannot parse, so the cast here cannot fail: it returns the value directly
    instead of re-entering the guarded-cast helper for a second parse.
    """
    if "integer" in schema_types and is_integer_shaped(s):
        return True, int(s)
    return False, s


def _coerce_float_value(s: str, schema_types: set[str]) -> tuple[bool, Any]:
    """(handled, value) for a schema that explicitly expects a number."""
    if "number" in schema_types and is_float_shaped(s):
        return True, _cast_or_original(float, s)
    return False, s


def _preserves_time_string(value: str, schema_types: set[str]) -> bool:
    """Whether a string time value must survive coercion untouched."""
    return "string" in schema_types and bool(_BARE_NEGATIVE_RE.match(value) or _NOW_OFFSET_RE.match(value))


class StripExtraFieldsMiddleware(Middleware):
    """Middleware that strips unknown fields and coerces types in tool arguments.

    AI clients (like OpenAI) often send extra/unknown fields with tool calls
    that don't match the tool's input schema. Docker MCP CLI also passes values
    as strings (e.g., "true" instead of true). This middleware:
    1. Filters arguments to only include fields defined in the tool's parameter schema
    2. Coerces string values to proper types (booleans, integers, floats)
    3. Reports the dropped names on the result (``ignored_arguments``) with a
       hint naming the tool that declares each

    Example:
        A tool with parameters {"domain": str, "enabled": bool} receiving
        {"domain": "example.com", "enabled": "true", "extra_field": "ignored"}
        will have arguments filtered and coerced to {"domain": "example.com", "enabled": true}

    The per-tool parameter schema and the catalog argument-owner index are
    memoized: the tool catalog is complete and static before the first call is
    served (everything is registered at startup), so re-fetching each tool's
    schema through FastMCP re-decodes the same large JSON Schema on every
    single call, and rebuilding the owner index re-walks the whole catalog.
    The caches key on the server identity, so a second server instance in the
    same process cannot read the first one's catalog.
    """

    def __init__(self, tool_names: Sequence[str] = ()) -> None:
        # (server identity, tool name) -> that tool's parameter schema.
        self._schema_cache: dict[tuple[int, str], dict[str, Any]] = {}
        # (server identity) -> argument-name owner index, built on first use.
        self._owner_cache: tuple[int, dict[str, tuple[str, ...]]] | None = None
        # The server's registration list: the only source that enumerates the
        # real tools under disclosure (see _catalog_argument_index).
        self._tool_names: tuple[str, ...] = tuple(tool_names)

    async def _tool_parameters(self, fastmcp_server: FastMCP, tool_name: str) -> dict[str, Any] | None:
        """The tool's parameter schema, memoized; None when the tool is unknown.

        Unknown tools are not cached: a tool registered after a miss must not
        be shadowed by it, and misses are off the served path anyway (FastMCP
        rejects unknown names right after this middleware returns).
        """
        key = (id(fastmcp_server), tool_name)
        cached = self._schema_cache.get(key)
        if cached is not None:
            return cached
        tool = await fastmcp_server.get_tool(tool_name)
        if tool is None:
            return None
        parameters: dict[str, Any] = tool.parameters or {}
        self._schema_cache[key] = parameters
        return parameters

    async def _owner_index(self, fastmcp_server: FastMCP) -> dict[str, tuple[str, ...]]:
        """The registered catalog's argument-owner index, built once per server."""
        if self._owner_cache is None or self._owner_cache[0] != id(fastmcp_server):
            self._owner_cache = (id(fastmcp_server), await _catalog_argument_index(fastmcp_server, self._tool_names))
        return self._owner_cache[1]

    def _get_schema_property_types(self, prop_schema: Any) -> set[str]:
        """Extract JSON Schema type(s) from a property schema.

        Handles ``type`` (string or list) and ``anyOf``/``oneOf`` subschemas.
        A value that is not a schema object (including None) declares nothing.
        """
        if not isinstance(prop_schema, dict):
            return set()
        types = _schema_declared_types(prop_schema)
        for sub_key in ("anyOf", "oneOf"):
            for subschema in prop_schema.get(sub_key, []):
                if isinstance(subschema, dict):
                    types.update(_schema_declared_types(subschema))
        return types

    def _coerce_string_value(self, s: str, schema_types: set[str]) -> Any:
        """Coerce a string based on the expected JSON Schema types.

        Only coerces to bool, int, or float when the schema explicitly expects
        that type. Identifier-like strings (e.g. profile IDs, entry IDs) declared
        as ``string`` are left untouched.
        """
        if _preserves_time_string(s, schema_types):
            return s
        for coerce in (_coerce_bool_value, _coerce_int_value, _coerce_float_value):
            handled, value = coerce(s, schema_types)
            if handled:
                return value
        return s

    def _coerce_value(self, value: Any, prop_schema: dict[str, Any] | None = None) -> Any:
        """Coerce a value using its property schema.

        Top-level values are coerced according to the declared schema. Nested
        dict/list values are recursed without schema context to avoid coercing
        identifier-like strings inside opaque objects.
        """
        if isinstance(value, str):
            return self._coerce_string_value(value, self._get_schema_property_types(prop_schema))
        if isinstance(value, list):
            return self._coerce_list_value(value, prop_schema)
        if isinstance(value, dict):
            return {k: self._coerce_value(v, None) for k, v in value.items()}
        return value

    def _coerce_list_value(self, value: list[Any], prop_schema: dict[str, Any] | None) -> list[Any]:
        """Recurse into list items with the list's own item schema."""
        items_schema = prop_schema.get("items") if isinstance(prop_schema, dict) else None
        return [self._coerce_value(item, items_schema) for item in value]

    async def _prepare_arguments(
        self,
        fastmcp_server: FastMCP,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """Fetch the tool schema, strip unknown fields, and coerce argument types.

        Returns the prepared arguments plus the names of any arguments that were
        dropped, so the caller can report them instead of losing them silently.

        Fails closed: if the tool's parameter schema cannot be fetched, the call
        is aborted with a ToolError instead of passing arguments through
        unstripped/un-coerced, so callers see a clear error rather than the
        downstream validation failures the middleware exists to prevent.
        """
        try:
            parameters = await self._tool_parameters(fastmcp_server, tool_name)
            if parameters is None:
                # Deliberate pass-through (covered by test_handles_tool_not_found):
                # a None tool means FastMCP could not resolve the tool name and
                # will reject the call immediately after this middleware returns,
                # so there is no unstripped-argument request to make. The
                # fail-closed branches below guard real failures; this branch
                # only avoids masking FastMCP's own unknown-tool error.
                return arguments, []

            return self._filter_and_coerce(tool_name, parameters, arguments)
        except NotFoundError:
            raise
        except Exception as e:
            # Schema fetch/parse failure: fail closed so callers get a clear error
            # instead of downstream validation failures from unstripped args.
            logger.exception(f"Failed to prepare arguments for tool '{tool_name}'; aborting call")
            raise ToolError(
                f"Tool call for '{tool_name}' failed closed: could not fetch its parameter "
                f"schema to validate arguments (root cause: {e})"
            ) from e

    def _filter_and_coerce(
        self, tool_name: str, parameters: dict[str, Any], arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Strip keys the tool's schema does not declare, then coerce what remains.

        The stripped names are returned so the caller can report them instead of
        losing them silently: silently dropping e.g. a `privacy` payload from a
        manageProfiles call leaves the model believing it acted.
        """
        properties = parameters.get("properties", {})
        known_params = set(properties.keys())
        filtered_args = {k: v for k, v in arguments.items() if k in known_params}
        stripped_keys = set(arguments) - known_params
        if stripped_keys:
            logger.debug(f"Tool '{tool_name}': Stripped unknown fields: {stripped_keys}")
        coerced_args = self._coerce_arguments(tool_name, properties, filtered_args)
        return coerced_args, sorted(stripped_keys)

    def _coerce_arguments(
        self, tool_name: str, properties: dict[str, Any], filtered_args: dict[str, Any]
    ) -> dict[str, Any]:
        """Coerce string values to the types the parameter schema declares."""
        coerced_args = {k: self._coerce_value(v, properties.get(k)) for k, v in filtered_args.items()}
        if coerced_args != filtered_args:
            logger.debug(f"Tool '{tool_name}': Coerced types in arguments")
        return coerced_args

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next: CallNext[mcp.types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Filter tool arguments to only include known parameters and coerce types.

        Fails closed: if the tool's parameter schema cannot be fetched, the call
        is aborted with a ToolError instead of passing arguments through
        unstripped/un-coerced.
        """
        arguments = context.message.arguments
        ignored: list[str] = []
        if arguments and context.fastmcp_context:
            context.message.arguments, ignored = await self._prepare_arguments(
                context.fastmcp_context.fastmcp, context.message.name, arguments
            )

        result = await call_next(context)
        if ignored:
            await self._annotate_ignored(result, context, ignored)
        return result

    async def _annotate_ignored(self, result: ToolResult, context: MiddlewareContext, ignored: list[str]) -> None:
        """Report this pass's dropped arguments, able to re-render the union's owner text.

        ``ignored`` is only non-empty when a FastMCP context was present (the
        strip needs the tool's schema), but the None path keeps a defensive
        generic hint rather than an assumption about harness shape.
        """
        server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
        index = await self._owner_index(server) if server is not None else {}
        _report_ignored_arguments(
            result,
            ignored,
            _owner_hint(ignored, context.message.name, index),
            hint_for=lambda names: _owner_hint(names, context.message.name, index),
        )


@asynccontextmanager
async def _server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage server lifespan: clients on shutdown, and the downloads they left (issue #277)."""
    # Own the downloads THIS server hands out: registration goes through the
    # context var (see nextdns_mcp.tools.logs), so shutdown below reclaims only
    # this lifespan's registry. The registries used to be one process-global
    # set that every embedded server's lifespan emptied, deleting a live
    # sibling server's tracked CSVs mid-read.
    owned_downloads: dict[str, None] = {}
    registry_token = _download_registry.set(owned_downloads)
    try:
        yield {}
    finally:
        try:
            await close_api_client()
        finally:
            await close_doh_client()
            # Successful downloadLogs CSVs are kept after their call (the caller
            # reads the path post-call); shutdown is the first safe reclaim point.
            # The context var still names this lifespan's registry, so only this
            # server's downloads are reclaimed - then the var is released.
            cleanup_successful_downloads()
            _download_registry.reset(registry_token)


def create_mcp_server(
    tool_names: Sequence[str] = (),
    endpoints: Mapping[str, tuple[str, str]] | None = None,
) -> FastMCP:
    """Create and configure the NextDNS MCP server.

    The tools themselves are registered by ``src/nextdns_mcp/server.py`` — the
    generated ones and the two local operations. What happens here is the machinery
    around them: disclosure, argument relaxation, the call log, and the health
    endpoint.

        Args:
        tool_names: The names of the tools registered after this call, so the
            initialize instructions can describe a small catalog directly.
        endpoints: ``tool name -> (method, path template)`` for the call log,
            built once from the parsed routes by the server, so the log and the
            tool surface are two views of one parse. ``None`` falls back to the
            shipped document's own parse; a caller registering tools from a
            modified document must pass the parse it registered from.

    Returns:
        FastMCP: Configured MCP server instance
    """
    logger.info("Creating NextDNS MCP server...")
    mcp, endpoints = _build_fastmcp_instance(tool_names, endpoints)

    # Add middleware to strip unknown fields from tool arguments
    # This allows AI clients (like OpenAI) that send extra fields to work properly
    mcp.add_middleware(StripExtraFieldsMiddleware(tool_names=tool_names))

    _add_call_log_middleware(mcp, endpoints)

    logger.info("MCP server created successfully")
    default_profile = get_default_profile()
    if default_profile:
        logger.info(f"Default profile: {default_profile}")

    _register_health_endpoint(mcp)

    return mcp


def _build_fastmcp_instance(
    tool_names: Sequence[str],
    endpoints: Mapping[str, tuple[str, str]] | None,
) -> tuple[FastMCP, Mapping[str, tuple[str, str]]]:
    """Build the ``FastMCP`` instance with progressive disclosure wired.

    Constructing the instance and the shape middleware stay one step because
    the middleware must exist on the instance before any tool call is served.
    The resolved endpoint map is returned beside the instance so the caller can
    add the strip and call-log middlewares between/after the shape middleware
    without re-deriving the map (or deriving one that disagrees with it).
    """
    if endpoints is None:
        endpoints = endpoints_for_routes()

    transforms: list[Any] = []
    tool_search_mode = get_tool_search_mode()
    search_transform = build_search_transform(tool_search_mode, endpoints=endpoints)
    if search_transform is not None:
        transforms.append(search_transform)
        logger.info(
            "Tool search enabled (%s): tools/list exposes search_tools + call_tool instead of the full catalog",
            tool_search_mode,
        )
    mcp = FastMCP(
        name="NextDNS MCP Server",
        instructions=server_instructions(tool_search_mode, tool_names),
        lifespan=_server_lifespan,
        transforms=transforms,
    )

    # Under disclosure the caller reaches tools only through call_tool, and
    # live runs show three ways it gets that wrapper wrong: the whole search
    # answer where a name belongs (`name: result.result`), a name typed from
    # memory, and the tool's parameters sent beside `name` instead of inside
    # `arguments`. Each is refused by call_tool's own schema or stripped by the
    # filter below, so the step ends with no call at all. Added first, so it
    # runs before that filter (which would drop the misplaced parameters) and
    # before the call log (which then records the call that actually happened).
    if search_transform is not None:
        mcp.add_middleware(CallToolShapeMiddleware(transform=search_transform, endpoints=endpoints))

    return mcp, endpoints


def _add_call_log_middleware(mcp: FastMCP, endpoints: Mapping[str, tuple[str, str]]) -> None:
    """Attach the call log when a path is configured (see ``nextdns_mcp.call_log``).

    Record what this server actually executed. A read leaves no trace in the
    API, so a harness that verifies over REST cannot tell "the model read it"
    from "the model did nothing and the state was already right". The server
    can say, and it does so here. The path is read from the environment once
    and passed in, so the middleware cannot disagree with the "enabled" line
    logged beside it, and the endpoint map arrives from the server's single
    route parse rather than a registry this module would have to import and
    populate.
    """
    log_path = call_log_path()
    if log_path:
        mcp.add_middleware(CallLogMiddleware(path=log_path, endpoints=endpoints))
        logger.info("Call log enabled: %s", log_path)


def _register_health_endpoint(mcp: FastMCP) -> None:
    """Register the ``/health`` readiness endpoint on the FastMCP HTTP app.

    ``GET /health`` is a real readiness check, not a liveness constant: it
    probes the NextDNS API with the configured API key and returns ``200 OK``
    with ``{"status": "ok"}`` only when that probe succeeds. A failing probe
    returns ``503`` together with the failure class (``auth`` or
    ``unreachable``) and a short reason. Results are cached for
    ``HEALTH_CACHE_TTL`` seconds so a polling load balancer cannot hammer the
    API. The route is registered with FastMCP's ``custom_route`` so it is served
    alongside the ``/mcp`` streamable-HTTP endpoint; in stdio mode there is no
    HTTP surface, so the route is simply not reachable.

    Args:
        mcp: The FastMCP server to attach the route to.
    """

    async def health_check(_request: Request) -> JSONResponse:
        """Report NextDNS API readiness as measured by a live credential probe."""
        failure = await check_nextdns_readiness()
        if failure is None:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {"status": "error", "class": failure.failure_class, "reason": failure.reason},
            status_code=503,
        )

    mcp.custom_route("/health", methods=["GET"])(health_check)


def _cached_readiness() -> tuple[bool, HealthFailure | None]:
    """Whether the cached probe result is still valid, and what it said.

    ``(fresh, failure)``; ``failure`` is the cached result (``None`` meaning
    *proven healthy*) only when ``fresh`` is True.
    """
    cached = _health_probe_cache
    if cached is None or time.monotonic() - cached[0] >= HEALTH_CACHE_TTL:
        return False, None
    return True, cached[1]


async def check_nextdns_readiness() -> HealthFailure | None:
    """Return the readiness result, probing NextDNS at most once per cache window.

    Single-flight: concurrent pollers that all miss an empty or expired cache
    wait on the one in-flight probe instead of each issuing their own (see
    ``_health_probe_lock``). A poller that arrives while a probe is running
    returns that probe's result, not a stale or duplicate one.

    Returns:
        None when the configured credentials work against NextDNS, otherwise a
        HealthFailure describing why the service is not ready.
    """
    global _health_probe_cache

    fresh, failure = _cached_readiness()
    if fresh:
        logger.debug("Reusing cached /health readiness result")
        return failure

    async with _health_probe_lock:
        fresh, failure = _cached_readiness()
        if fresh:
            logger.debug("Reusing /health readiness result filled by a concurrent probe")
            return failure
        failure = await _run_health_probe()
        _health_probe_cache = (time.monotonic(), failure)
        return failure


async def _run_health_probe() -> HealthFailure | None:
    """Probe the NextDNS API once with the configured credentials.

    The request goes through the client's RAW transport (``raw_request``), which
    bypasses profile access control: a local ACL denial must not mask the true
    upstream authentication state. Only the status code and the exception type
    name are reported, never the API key, the auth headers, or the response
    body.

    Returns:
        None when NextDNS accepted the credentials, otherwise a HealthFailure.
    """
    try:
        client = get_api_client()
        response = await client.raw_request("GET", HEALTH_PROBE_PATH, timeout=HEALTH_PROBE_TIMEOUT)
    except ConfigurationError:
        return _configuration_failure()
    except httpx.TimeoutException:
        return HealthFailure(
            HEALTH_FAILURE_UNREACHABLE,
            f"NextDNS API did not respond within {HEALTH_PROBE_TIMEOUT:g}s",
        )
    except httpx.HTTPError as exc:
        return HealthFailure(
            HEALTH_FAILURE_UNREACHABLE,
            f"Could not reach the NextDNS API ({type(exc).__name__})",
        )

    if response.is_success:
        return None
    return _probe_status_failure(response)


def _configuration_failure() -> HealthFailure:
    """Classify a ConfigurationError from building the client: no key at all is auth."""
    if not get_api_key():
        return HealthFailure(HEALTH_FAILURE_AUTH, "No usable NextDNS API key is configured")
    return HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        "NextDNS client configuration is invalid (check NEXTDNS_HTTP_TIMEOUT)",
    )


def _probe_status_failure(response: httpx.Response) -> HealthFailure:
    """Classify a non-success probe answer: 401/403 is auth, anything else unreachable."""
    if response.status_code in (401, 403):
        return HealthFailure(
            HEALTH_FAILURE_AUTH,
            f"NextDNS API rejected the configured credentials (HTTP {response.status_code})",
        )
    return HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        f"NextDNS API answered with an unusable status (HTTP {response.status_code})",
    )
