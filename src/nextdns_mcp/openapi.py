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
import weakref
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
from .client import (
    ApiClientOwner,
    api_client_owner,
    close_raw_probe_client,
    get_raw_probe_client,
)
from .config import ConfigurationError, get_api_key, get_default_profile, get_tool_search_mode
from .constants import SYNTHETIC_TOOL_NAMES
from .spec_tools import endpoints_for_routes
from .state import RESOURCES_LIFESPAN_KEY
from .tool_search import CallToolShapeMiddleware, build_search_transform
from .tools.doh import DohClientOwner, doh_client_owner
from .tools.logs import DownloadRegistry, _download_registry, close_redirect_client
from .usage import server_instructions
from .utils import BARE_NEGATIVE_RE, NOW_OFFSET_RE, is_float_shaped, is_integer_shaped

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


class _HealthFailure(NamedTuple):
    """A failed readiness probe, kept on the operator's side of the boundary.

    Both fields are reported to the server log (`_log_readiness_failure`) and
    never over ``/health``: the HTTP transport is unauthenticated by design, so
    the class and reason would tell any host that can reach the port whether the
    configured API key is accepted (issue #603).

    Attributes:
        failure_class: ``"auth"`` or ``"unreachable"``.
        reason: Short operator-facing explanation. Built only from the HTTP
            status code, the exception type name, and configuration state, so it
            can never carry API key material, auth headers, or the upstream
            response body.
    """

    failure_class: str
    reason: str


# Cached probe result as (monotonic timestamp, failure or None when healthy),
# held by the ReadinessProber that owns it: one prober per FastMCP server, so
# the cache and the single-flight lock are per-server state, never process
# globals two embedded servers would share.


class ReadinessProber:
    """Owns the ``/health`` readiness cache and single-flight lock for one server.

    Explicit owner for the probe state that used to live in the module-global
    ``_health_probe_cache`` singleton and its module-global lock. Each
    ``FastMCP`` server gets its own prober when its ``/health`` route is
    registered, so two servers embedded in one process keep independent
    readiness state and never serialize each other's probes.
    """

    def __init__(self) -> None:
        self._cache: tuple[float, _HealthFailure | None] | None = None
        # Guards probe *construction*, not cache reads: concurrent ``/health``
        # pollers that all miss an empty (or just-expired) cache used to each
        # fire their own upstream request - a thundering herd against the
        # cheapest authenticated endpoint, which is exactly what the TTL cache
        # exists to prevent (the cache bounds steady-state polling, not the
        # simultaneous first miss). The lock makes the probe single-flight:
        # one request goes upstream, everyone else waits on it and reads the
        # filled cache.
        self._lock = asyncio.Lock()

    async def check(self) -> _HealthFailure | None:
        """Return the readiness result, probing NextDNS at most once per cache window.

        Single-flight: concurrent pollers that all miss an empty or expired
        cache wait on the one in-flight probe instead of each issuing their
        own (see ``self._lock``). A poller that arrives while a probe is
        running returns that probe's result, not a stale or duplicate one.

        The probe transport is the shared raw probe client (issue #603's
        ACL-free transport, owned upstream of this card's scope).

        Returns:
            None when the configured credentials work against NextDNS,
            otherwise a _HealthFailure describing why the service is not ready.
        """
        fresh, failure = self._cached()
        if fresh:
            logger.debug("Reusing cached /health readiness result")
            return failure

        async with self._lock:
            fresh, failure = self._cached()
            if fresh:
                logger.debug("Reusing /health readiness result filled by a concurrent probe")
                return failure
            failure = await self._probe()
            self._cache = (time.monotonic(), failure)
            if failure is not None:
                # One record per real probe, not per poll: the detail lives here
                # because it cannot live in the /health body (see #603).
                _log_readiness_failure(failure)
            return failure

    def _cached(self) -> tuple[bool, _HealthFailure | None]:
        """Whether the cached probe result is still valid, and what it said.

        ``(fresh, failure)``; ``failure`` is the cached result (``None``
        meaning *proven healthy*) only when ``fresh`` is True.
        """
        cached = self._cache
        if cached is None or time.monotonic() - cached[0] >= HEALTH_CACHE_TTL:
            return False, None
        return True, cached[1]

    async def _probe(self) -> _HealthFailure | None:
        """Probe the NextDNS API once with the configured credentials.

        The request goes through the raw probe transport
        (``client.get_raw_probe_client()``), which bypasses profile access
        control: a local ACL denial must not mask the true upstream
        authentication state. Only the status code and the exception type
        name are reported, never the API key, the auth headers, or the
        response body.

        Returns:
            None when NextDNS accepted the credentials, otherwise a
            _HealthFailure.
        """
        try:
            probe_client = get_raw_probe_client()
            response = await probe_client.request("GET", HEALTH_PROBE_PATH, timeout=HEALTH_PROBE_TIMEOUT)
        except ConfigurationError:
            return _configuration_failure()
        except httpx.TimeoutException:
            return _HealthFailure(
                HEALTH_FAILURE_UNREACHABLE,
                f"NextDNS API did not respond within {HEALTH_PROBE_TIMEOUT:g}s",
            )
        except httpx.HTTPError as exc:
            return _HealthFailure(
                HEALTH_FAILURE_UNREACHABLE,
                f"Could not reach the NextDNS API ({type(exc).__name__})",
            )

        if response.is_success:
            return None
        return _probe_status_failure(response)


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
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        _annotate_content_only(result, ignored, hint)
        return
    if "$prefab" in structured:
        _annotate_content_only(result, ignored, hint)
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


def _annotate_content_only(result: ToolResult, ignored: list[str], hint: str) -> None:
    """Say the annotation in the content when no plain-dict payload can carry it.

    Prefab answers (plotAnalytics, overviewDashboard) arrive as a model rather
    than a dict, so the dict-merge path cannot touch them, and a result with
    no structured payload at all has nothing else to carry the note; without
    this the argument drop would be silent for exactly the tools whose names
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


class ServerResources:
    """One server instance's explicitly owned state.

    Owns the API client owner, DoH client owner, and download registry a
    server serves: the state that used to live in process-global module
    singletons any caller could reach. ``create_mcp_server()`` builds one per
    ``FastMCP`` instance and attaches it to the instance, so two embedded
    servers never read or reset each other's state. :meth:`aclose` closes the
    clients and reclaims the downloads - one server's shutdown cannot reach
    into a sibling server's connections or tracked CSVs.
    """

    def __init__(self) -> None:
        self.api_client = ApiClientOwner()
        self.doh_client = DohClientOwner()
        self.downloads = DownloadRegistry()

    async def aclose(self) -> None:
        """Release everything this server owns: clients first, then the downloads.

        Matches the shutdown order the lifespan has always used: close the
        API client even when the DoH close or the download cleanup fails,
        and never leave this server's tracked CSVs behind.
        """
        try:
            await self.api_client.aclose()
        finally:
            await self.doh_client.aclose()
            self.downloads.cleanup()


def _server_resources(mcp: FastMCP) -> ServerResources:
    """The resources attached to this FastMCP instance at creation.

    Raises:
        RuntimeError: When the instance carries no resources - every server
            this package creates attaches them in ``create_mcp_server``.
    """
    resources = getattr(mcp, "_nextdns_resources", None)
    if resources is None:
        raise RuntimeError("FastMCP instance carries no nextdns_mcp ServerResources")
    return resources


@asynccontextmanager
async def _server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage server lifespan: own the server's state explicitly, release it on shutdown.

    Own the state THIS server hands out: the API client, DoH client, and
    download registry attached to the instance by ``create_mcp_server``. The
    lifespan publishes them two ways, both explicit: into each module's owner
    context var for same-task callers, and into the lifespan context FastMCP
    hands every request this server serves (issue #642: the lifespan task
    does not propagate context variables into request tasks, so the lifespan
    context is the only channel served traffic can resolve owners through).
    A second embedded server owns its own resources, so one server's shutdown
    closes only its own clients and reclaims only its own downloads
    (issues #277, #642). The state used to be process-global singletons that
    every embedded server's lifespan emptied or closed, killing a live
    sibling's connections and deleting its tracked CSVs mid-read.
    """
    resources = _server_resources(server)
    api_token = api_client_owner.set(resources.api_client)
    doh_token = doh_client_owner.set(resources.doh_client)
    registry_token = _download_registry.set(resources.downloads)
    try:
        yield {RESOURCES_LIFESPAN_KEY: resources}
    finally:
        try:
            await resources.api_client.aclose()
        finally:
            await close_raw_probe_client()
            await resources.doh_client.aclose()
            resources.downloads.cleanup()
            # The downloadLogs redirect chain reuses one persistent unauthenticated
            # client for keep-alive reuse (issue #656); like the transport above
            # it is closed at shutdown.
            await close_redirect_client()
            # The lazily-started Pyodide/Deno sandbox subprocess (issue #408) is
            # stopped by PrefabGenerativeUI.lifespan(), which FastMCP unwinds
            # before this lifespan: this module has no dependency on the optional
            # generative module, and one server's shutdown no longer reaches into
            # its siblings' sandboxes (issue #542).
            _download_registry.reset(registry_token)
            doh_client_owner.reset(doh_token)
            api_client_owner.reset(api_token)


def create_mcp_server(
    tool_names: Sequence[str] = (),
    endpoints: Mapping[str, tuple[str, str]] | None = None,
) -> FastMCP:
    """Create and configure the NextDNS MCP server.

    The tools themselves are registered by ``src/nextdns_mcp/server.py`` — the
    generated ones and the three composite operations. What happens here is the machinery
    around them: disclosure, argument relaxation, the call log, the health
    endpoint, and ownership of the server's long-lived state — one
    ``ServerResources`` (the API client, DoH client, and download registry)
    is built here and attached to the instance, so every server owns its state
    explicitly and embedded servers never share it.

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
    mcp._nextdns_resources = ServerResources()  # type: ignore[attr-defined]
    default_profile = get_default_profile()
    if default_profile:
        logger.info(f"Default profile: {default_profile}")

    _register_health_endpoint(mcp)
    _register_compat_endpoints(mcp)

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
    returns ``503`` with the minimal body ``{"status": "error"}`` — no failure
    class and no reason. The HTTP transport is unauthenticated by design
    (``docs/safety.md``: an authenticating reverse proxy is the caller's job),
    so anything this body says is readable by every host that can reach the
    port. A class of ``auth`` plus a reason naming the upstream status is an
    oracle for "is this API key accepted", which is not something a route that
    cannot identify its caller should answer. The class and reason stay on the
    operator's side of the boundary, in the process log
    (``_log_readiness_failure``). Results are cached for ``HEALTH_CACHE_TTL``
    seconds so a polling load balancer cannot hammer the API. The route is
    registered with FastMCP's ``custom_route`` so it is served alongside the
    ``/mcp`` streamable-HTTP endpoint; in stdio mode there is no HTTP surface,
    so the route is simply not reachable. The prober - the route's TTL cache
    and single-flight lock - is owned here, per server, so two embedded
    servers never share readiness state.

    Args:
        mcp: The FastMCP server to attach the route to.
    """
    prober = ReadinessProber()

    async def health_check(_request: Request) -> JSONResponse:
        """Report NextDNS API readiness as measured by a live credential probe."""
        failure = await prober.check()
        if failure is None:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "error"}, status_code=503)

    mcp.custom_route("/health", methods=["GET"])(health_check)


def _register_compat_endpoints(mcp: FastMCP) -> None:
    """Register OpenWebUI compatibility endpoints on the FastMCP HTTP app.

    These routes exist for compatibility with third-party clients (e.g.
    OpenWebUI's automatic model-discovery probe) rather than for the MCP
    protocol itself. They are registered separately from the health endpoint
    so health and compat concerns stay independent and independently testable.

    Args:
        mcp: The FastMCP server to attach the routes to.
    """

    async def v1_models(_request: Request) -> JSONResponse:
        """Answer OpenWebUI's automatic model-discovery probe to prevent noisy 404 logs."""
        return JSONResponse({"object": "list", "data": []})

    mcp.custom_route("/v1/models", methods=["GET"])(v1_models)


def _log_readiness_failure(failure: _HealthFailure) -> None:
    """Report a failed probe to the operator, where the response body cannot.

    The class and reason identify why the server is not ready — an auth failure
    and an unreachable API need different fixes — so closing the body has to
    move that detail, not drop it. Both fields are built from a status code, an
    exception type name, and configuration state (`_HealthFailure`), never from a
    request URL or an exception message, so this WARNING needs no redaction
    pass (`_redacted()` in `client.py` guards those shapes).
    """
    logger.warning("NextDNS readiness probe failed: class=%s reason=%s", failure.failure_class, failure.reason)


def _configuration_failure() -> _HealthFailure:
    """Classify a ConfigurationError from building the client: no key at all is auth."""
    if not get_api_key():
        return _HealthFailure(HEALTH_FAILURE_AUTH, "No usable NextDNS API key is configured")
    return _HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        "NextDNS client configuration is invalid (check NEXTDNS_HTTP_TIMEOUT)",
    )


def _probe_status_failure(response: httpx.Response) -> _HealthFailure:
    """Classify a non-success probe answer: 401/403 is auth, anything else unreachable."""
    if response.status_code in (401, 403):
        return _HealthFailure(
            HEALTH_FAILURE_AUTH,
            f"NextDNS API rejected the configured credentials (HTTP {response.status_code})",
        )
    return _HealthFailure(
        HEALTH_FAILURE_UNREACHABLE,
        f"NextDNS API answered with an unusable status (HTTP {response.status_code})",
    )
