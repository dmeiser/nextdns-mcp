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
"""Argument relaxation middleware for the NextDNS MCP server.

``StripExtraFieldsMiddleware`` drops top-level tool arguments the target
tool's published schema does not declare, coerces obvious string spellings of
typed fields, and reports the dropped names on the result instead of losing
them silently. The owner-hint helpers name the tool that actually declares
each dropped argument; the coercion helpers turn ``"true"``/``"42"``-shaped
strings into the typed values the schema expects.

This module deliberately keeps no import edge into ``spec_tools``: the
catalog argument-owner index is enumerated from the server's own registration
list, not from the document (see ``_catalog_argument_index``).

See docs/troubleshooting.md for details.

SPDX-License-Identifier: MIT
"""

import logging
import weakref
from collections.abc import Callable, Sequence
from typing import Any

import mcp.types
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from .constants import SYNTHETIC_TOOL_NAMES
from .utils import BARE_NEGATIVE_RE, NOW_OFFSET_RE, is_float_shaped, is_integer_shaped

logger = logging.getLogger(__name__)


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
    this module keeps no import edge into ``spec_tools`` for it. Synthetic
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


def _annotate_structured_ignored(
    structured: dict[str, Any],
    ignored: list[str],
    hint: str,
    hint_for: Callable[[list[str]], str] | None = None,
) -> None:
    """Record dropped argument names and owner hint onto structured result dictionary."""
    already = structured.get("ignored_arguments")
    prior_names = already if isinstance(already, list) else []
    merged = list(dict.fromkeys([*prior_names, *ignored]))
    structured["ignored_arguments"] = merged
    if hint_for is not None and prior_names:
        _rerender_owner_annotation(structured, merged, hint_for)
        return
    _merge_ignored_hints(structured, hint)


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

    A Prefab result (``$prefab`` in the payload) is the renderer's wire
    document, not this server's response shape: the note is said in the
    content and the document is left exactly as the tool emitted it.
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        _annotate_content_only(result, ignored, hint)
        return
    if "$prefab" in structured:
        _annotate_content_only(result, ignored, hint)
        return
    _annotate_structured_ignored(structured, ignored, hint, hint_for)


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


def _append_content_note(result: Any, text: str) -> None:
    """Append a text note to the result's content, best-effort.

    A middleware report whose payload cannot carry it - a Prefab wire
    document, an image-only answer - says itself in the content instead,
    beside what the tool produced. The append never raises: losing the note is
    recoverable, replacing the tool's own answer with a failure because its
    content is not a list is not. It lives here because this module owns the
    ignored-arguments note; the call-shape middleware reuses it rather than
    growing a second copy of the try.
    """
    try:
        result.content.append(mcp.types.TextContent(type="text", text=text))
    except Exception:
        logger.debug("middleware note could not be appended to the result content", exc_info=True)


def _annotate_content_only(result: ToolResult, ignored: list[str], hint: str) -> None:
    """Say the annotation in the content when no plain-dict payload can carry it.

    A Prefab answer (plotAnalytics, overviewDashboard) is the renderer's wire
    document: its keys belong to that document, so a note added to it is not
    this server's response shape. A result with no structured payload at all
    has nothing else to carry the note either; without this the argument drop
    would be silent for exactly the tools whose names (from/to on the series
    endpoints) differ from this one's (from_time/to_time).
    """
    if not ignored:
        return
    _append_content_note(result, f"Note: the server ignored argument(s) {', '.join(ignored)}; {hint}")


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


def _is_bool_shaped(s: str) -> bool:
    """Whether the string spells a boolean: ``true`` or ``false``, any case."""
    return s.lower() in ("true", "false")


def _to_bool(s: str) -> bool:
    """The boolean cast: only ``true`` in any capitalization reads as True."""
    return s.lower() == "true"


def _to_float_or_original(s: str) -> Any:
    """Apply the number cast; a ValueError leaves the original string unchanged.

    ``is_float_shaped`` is a shape test, not a parse, so an accepted string can
    still fail ``float()``; it comes back unchanged rather than raising out of
    the coercion layer. The integer row needs no such guard: ``is_integer_shaped``
    already ran ``int(s)`` and rejected anything it cannot parse.
    """
    try:
        return float(s)
    except ValueError:
        return s


def _coerce_string_by_type(s: str, schema_types: set[str]) -> Any:
    """Coerce a string to the scalar type the schema declares, or leave it.

    One table, one row per JSON Schema scalar type: the declared type that
    authorizes the coercion, the shape guard the string must pass, and the cast
    to apply. The first match wins, so a schema declaring both ``integer`` and
    ``number`` gets the int. A string no row claims is returned as written —
    identifier-like values (profile and entry IDs) stay strings.

    The rows are assembled per call so the shape guards are read from this
    module's globals each time, which keeps a test that patches
    ``is_float_shaped`` here able to reach the guarded number cast.
    """
    rows: tuple[tuple[str, Callable[[str], bool], Callable[[str], Any]], ...] = (
        ("boolean", _is_bool_shaped, _to_bool),
        ("integer", is_integer_shaped, int),
        ("number", is_float_shaped, _to_float_or_original),
    )
    for declared_type, is_shaped, cast in rows:
        if declared_type in schema_types and is_shaped(s):
            return cast(s)
    return s


def _preserves_time_string(value: str, schema_types: set[str]) -> bool:
    """Whether a string time value must survive coercion untouched."""
    return "string" in schema_types and bool(BARE_NEGATIVE_RE.match(value) or NOW_OFFSET_RE.match(value))


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
    The caches key on the server object itself — never ``id(server)``, whose
    value Python recycles after garbage collection — so a second server
    instance in the same process cannot read a dead server's catalog.
    """

    def __init__(self, tool_names: Sequence[str] = ()) -> None:
        # server -> tool name -> that tool's parameter schema. The weak key is
        # the server object, not id(server): id() values are reused once an
        # object is collected, which let a new server read a dead server's
        # cached schema. Entries vanish with the server they belong to.
        self._schema_cache: weakref.WeakKeyDictionary[FastMCP, dict[str, dict[str, Any]]] = weakref.WeakKeyDictionary()
        # server -> argument-name owner index, built on first use. Same weak
        # keying as _schema_cache, for the same id()-reuse reason.
        self._owner_cache: weakref.WeakKeyDictionary[FastMCP, dict[str, tuple[str, ...]]] = weakref.WeakKeyDictionary()
        # The server's registration list: the only source that enumerates the
        # real tools under disclosure (see _catalog_argument_index).
        self._tool_names: tuple[str, ...] = tuple(tool_names)

    async def _tool_parameters(self, fastmcp_server: FastMCP, tool_name: str) -> dict[str, Any] | None:
        """The tool's parameter schema, memoized; None when the tool is unknown.

        Unknown tools are not cached: a tool registered after a miss must not
        be shadowed by it, and misses are off the served path anyway (FastMCP
        rejects unknown names right after this middleware returns).
        """
        server_cache = self._schema_cache.get(fastmcp_server)
        if server_cache is None:
            server_cache = {}
            self._schema_cache[fastmcp_server] = server_cache
        cached = server_cache.get(tool_name)
        if cached is not None:
            return cached
        tool = await fastmcp_server.get_tool(tool_name)
        if tool is None:
            return None
        parameters: dict[str, Any] = tool.parameters or {}
        server_cache[tool_name] = parameters
        return parameters

    async def _owner_index(self, fastmcp_server: FastMCP) -> dict[str, tuple[str, ...]]:
        """The registered catalog's argument-owner index, built once per server."""
        cached = self._owner_cache.get(fastmcp_server)
        if cached is None:
            cached = await _catalog_argument_index(fastmcp_server, self._tool_names)
            self._owner_cache[fastmcp_server] = cached
        return cached

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
        return _coerce_string_by_type(s, schema_types)

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
