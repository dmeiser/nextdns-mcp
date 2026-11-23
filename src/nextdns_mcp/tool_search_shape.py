"""Call-shape handling for the synthetic ``call_tool`` proxy.

Split out of ``tool_search`` so the serving side (the transform and its
fallback) and the call-shape side (resolving what a caller meant) stay
separately readable. Everything here is a pure function of the catalog and
the call the caller made: name resolution (from a search answer, a mistyped
name, or an already-exact one), argument lifting, and the write refusals.
The transform owns the synthetic pair's own descriptions.

The read/write classification of a name (:func:`_is_write_tool`) lives here
too, because the refusals are what make it a fact rather than a heuristic: a
re-target is kept inside the reads only if "write" is decided from the route
map, not from the shape of a name. The transform's full-catalog fallback sorts
reads first with the same function, so both paths ask one question the same
way.

SPDX-License-Identifier: MIT
"""

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

from .constants import is_write_operation
from .tool_search_rank import (
    _catalog_words_for,
    _catalog_words_memo,
    _CatalogWords,
    _flat_key,
    _tool_properties,
    _word_scorer,
    _words,
)
from .utils import schema_required

# The shape-repair WARNING records are a log contract: filters keyed to
# "nextdns_mcp.tool_search" must keep seeing them even though this module is no
# longer named tool_search — pin the name explicitly instead of using __name__.
logger = logging.getLogger("nextdns_mcp.tool_search")


def _annotation_says_write(tool: Any) -> bool:
    """Whether a tool's own annotations state that calling it is not a read.

    The route map can only speak for spec operations. A tool it does not carry
    (``generate_prefab_ui``, the three composite operations) has no HTTP method
    to read, and its annotations are the only place its side effects are
    declared: ``readOnlyHint is False`` is the server saying the call does
    something. An unset or read-only annotation keeps the traditional reading
    that a name off the map reads, which is what the composites rely on.
    """
    return getattr(getattr(tool, "annotations", None), "read_only_hint", None) is False


def _is_write_tool(name: str, endpoints: Mapping[str, tuple[str, str]] | None, tool: Any = None) -> bool:
    """Whether calling ``name`` changes something: the endpoint map first, then the tool.

    The map is the fact of what a spec route executes. For a name the map does
    not carry, the tool's own ``readOnlyHint`` decides - defaulting every
    off-map name to ``GET`` is what let a loose or ambiguous match execute
    sandboxed code through ``generate_prefab_ui``, a tool registered with
    ``read_only_hint=False``.
    """
    endpoint = (endpoints or {}).get(name)
    if endpoint is not None:
        return is_write_operation(endpoint[0])
    return _annotation_says_write(tool)


def _closest_tools(
    name: str, tools: Sequence[Tool], limit: int = 3, catalog: _CatalogWords | None = None
) -> list[Tool]:
    """The tools a typed name's words come closest to, ranked as search_tools ranks them.

    A caller that types ``denylists.updateDenylistEntry`` or "generate analytics
    plot" has described a task in this catalog's vocabulary without naming a
    tool. Ranking those words with the rule search_tools already uses turns a
    dead end into a correction the caller can act on.

    The ranking data comes from the catalog's word memo when the caller holds
    it — the shape middleware memoizes it per catalog, so a repeated unknown
    name performs no ranking-data rebuild; without it the structures are
    derived from ``tools`` here.
    """
    if not tools:
        return []
    if catalog is None:
        catalog = _catalog_words_for(tools)
    scorer = _word_scorer(
        _words(name),
        catalog.names,
        catalog.vocabulary,
        (catalog.name_shared, catalog.vocab_shared),
        catalog.leading,
        catalog.generic_verbs,
    )
    ordered = sorted(tools, key=lambda tool: -scorer(tool.name))
    return [tool for tool in ordered[:limit] if scorer(tool.name) > 0]


def _required_arguments(tool: Tool) -> str:
    """The arguments a tool cannot be called without, read from its own schema."""
    required = schema_required(tool.parameters)
    return ", ".join(required) if required else "no arguments"


def _unknown_tool_message(name: str, tools: Sequence[Tool], catalog: _CatalogWords | None = None) -> str:
    """Say what the caller can call instead of a name that is not a tool.

    FastMCP's own answer is ``Unknown tool: '<x>'``. Under disclosure the caller
    cannot see the names that do exist, so the only way out is to search again -
    and a caller that typed a name has already decided it knows the tool. Live,
    it guesses again instead, shorter each time.
    """
    closest = _closest_tools(name, tools, catalog=catalog)
    if not closest:
        return (
            f"{name!r} is not a tool of this server. search_tools(query) returns "
            "the tools there are, with the exact name each one takes."
        )
    listed = "; ".join(f"{tool.name} takes {_required_arguments(tool)}" for tool in closest)
    return (
        f"{name!r} is not a tool of this server. Its words match: {listed}. call_tool takes one of those names exactly."
    )


def _flat_name_keys(names: Sequence[str], catalog: _CatalogWords | None) -> Mapping[str, str]:
    """The flat comparison key of each candidate — from the memo when held."""
    if catalog is not None:
        return catalog.flat_names
    return {candidate: _flat_key(candidate) for candidate in names}


def _name_word_sets(names: Sequence[str], catalog: _CatalogWords | None) -> Mapping[str, set[str]]:
    """The word set of each candidate — from the memo when held."""
    if catalog is not None:
        return catalog.names
    return {candidate: _words(candidate) for candidate in names}


def _flat_equal_matches(name: str, names: Sequence[str], catalog: _CatalogWords | None = None) -> list[str]:
    """Catalog entries equal to the typed name once separators and case are gone.

    The per-candidate flat keys are a constant of the catalog and live in its
    word memo; a caller holding the memo (the shape middleware does) passes it
    so a repeated unknown name re-normalizes nothing catalog-side. Without the
    memo each candidate is normalized here.
    """
    flat = _flat_key(name)
    flat_keys = _flat_name_keys(names, catalog)
    return [candidate for candidate in names if flat_keys.get(candidate) == flat]


def _word_equal_matches(name: str, names: Sequence[str], catalog: _CatalogWords | None = None) -> list[str]:
    """Catalog entries whose word set equals the typed name's word set.

    The candidate word sets live in the catalog's word memo when the caller
    holds it (the shape middleware does); without the memo each candidate is
    tokenized here.
    """
    words = _words(name)
    word_sets = _name_word_sets(names, catalog)
    return [candidate for candidate in names if word_sets.get(candidate) == words]


def _match_tool_name(name: str, names: Sequence[str], catalog: _CatalogWords | None = None) -> str:
    """The catalog entry a mistyped name unambiguously means, or ''.

    Two readings only, both exact. The same name with separators and case
    removed: ``get_denylist`` and ``getdenylist`` are getDenylist. The
    same words in another order: ``analytics/plot`` is plotAnalytics, because
    the caller used that tool's own words and nothing else. A near miss that is
    neither is left alone - the caller gets the diagnosis, not a guess.

    A reading is dropped when two catalog entries satisfy it, so an ambiguous
    name is never resolved. The catalog's word memo, when the caller holds it,
    supplies the per-candidate comparison data so nothing is recomputed.
    """
    if not name or name in names:
        return ""
    flat_matches = _flat_equal_matches(name, names, catalog)
    if len(flat_matches) == 1:
        return flat_matches[0]
    word_matches = _word_equal_matches(name, names, catalog)
    return word_matches[0] if len(word_matches) == 1 else ""


def _dict_entry_name(value: dict[str, Any]) -> str:
    """The `name` of one search-answer entry, '' when it has none."""
    name = value.get("name")
    return name.strip() if isinstance(name, str) else ""


def _named_entries(entries: Sequence[Any]) -> list[str]:
    """The names a search answer carries, in rank order (rank order = candidate order).

    Entries that carry no name are not candidates: they cannot be re-sent as a
    call, so they neither resolve nor count toward the multi-result write gate
    in CallToolShapeMiddleware.
    """
    names: list[str] = []
    for entry in entries:
        name = _name_from_search_answer(entry)
        if name:
            names.append(name)
    return names


def _name_from_search_answer(value: Any) -> str:
    """The tool name a caller meant, when it sent the search answer instead.

    The single most common way a call never reaches a tool, seen repeatedly
    across live runs, is this expression::

        search_tools({query}).then(result => call_tool({ name: result.result, … }))

    `result.result` is the answer - an array of tool objects - and FastMCP
    rejects it against call_tool's own `name: str`, so nothing runs. The answer
    is in rank order and the caller passed all of it, so the top-ranked entry is
    the only defensible reading; taking it is not a guess about intent. When the
    answer holds several named entries and the top-ranked one is a write, the
    middleware refuses the call instead of executing the guess (see
    CallToolShapeMiddleware._refuse_ambiguous_write) - reading the head here is
    not permission to run it.

    Returns '' when the value cannot be read as a name, leaving the schema
    error to fire unchanged.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _dict_entry_name(value)
    if isinstance(value, list):
        names = _named_entries(value)
        return names[0] if names else ""
    return ""


def _dict_or_empty(value: Any) -> dict[str, Any]:
    """A supplied value as a dict copy, or empty when it is not a dict at all."""
    return dict(value) if isinstance(value, dict) else {}


def _moved_into_arguments(arguments: dict[str, Any], nested: dict[str, Any], moved: list[str]) -> dict[str, Any]:
    """The wrapper with ``moved`` removed from it and folded into the nested block.

    Nested values win nothing here: ``moved`` never contains a key ``nested``
    already holds (the caller checked), so the merge cannot overwrite a value
    the caller put inside ``arguments``.
    """
    shaped = {key: value for key, value in arguments.items() if key not in moved}
    shaped["arguments"] = {**nested, **{key: arguments[key] for key in moved}}
    return shaped


def _note_search_answer_name(supplied: Any, resolved: str) -> None:
    """Log once when the name arrived as a search answer rather than a bare string.

    A fallback correction, not normal traffic: the caller's client assembled a
    call shape this server had to repair, and production logs should show how
    often that happens (WARNING, beside the payload's `resolved_from`).
    """
    if resolved and resolved != supplied:
        logger.warning("call_tool name given as %s; resolved to the top-ranked %r", type(supplied).__name__, resolved)


def _ambiguous_write_message(candidates: Sequence[str]) -> str:
    """Say what the answer offered and that its head is a write, so the caller picks one."""
    listed = ", ".join(repr(name) for name in candidates)
    return (
        f"The search answer names {len(candidates)} tools and its highest-ranked {candidates[0]!r} "
        f"is a write, so nothing ran. Send one name exactly - call_tool takes {listed}."
    )


def _supplied_name_label(supplied: Any) -> str:
    """A short readable form of whatever the caller sent as `name`."""
    if isinstance(supplied, str):
        return supplied
    return json.dumps(supplied, default=str)[:120]


def _annotate_resolved_from(result: Any, supplied: Any, resolved: str | None) -> Any:
    """Say on the payload when the executed call is not the call the caller named.

    A re-target the caller cannot see is a wrong discovery it cannot notice:
    the shape repair is a fallback, so it announces itself in the structured
    content, beside `ignored_arguments`, with what the caller actually sent.
    Only a dict payload can carry the note; anything else passes through.
    """
    if not resolved or resolved == supplied:
        return result
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        structured["resolved_from"] = _supplied_name_label(supplied)
        # Name it in the other direction too: a caller that reads only the
        # result (not the transport logs) learns which tool actually ran.
        structured["resolved_name"] = resolved
    return result


def _passthrough_resolution(resolved: str, by_name: dict[str, Tool]) -> tuple[str | None, Tool | None] | None:
    """The answer when there is nothing to match: no name given, or no catalog.

    An empty catalog - no transform, or one that could not be read - leaves the
    name exactly as the caller sent it rather than refusing it.
    """
    if not resolved or not by_name:
        return resolved, by_name.get(resolved) if resolved else None
    return None


def _resolved_entry(
    resolved: str, by_name: dict[str, Tool], catalog: _CatalogWords | None = None
) -> tuple[str, Tool] | None:
    """The (name, entry) the supplied name refers to in the catalog, or None if it matches nothing.

    A name that is itself a tool is used as given; one that normalises to exactly
    one entry is corrected to it.
    """
    if resolved in by_name:
        return resolved, by_name[resolved]
    matched = _match_tool_name(resolved, list(by_name), catalog)
    if matched:
        logger.warning("call_tool name %r is not a tool; it matches %r once normalised", resolved, matched)
        return matched, by_name[matched]
    return None


class CallToolShapeMiddleware(Middleware):
    """Make ``call_tool`` mean the call the caller was trying to make.

    Under disclosure the two synthetic tools are the only way in, and their
    shape - ``{name, arguments}`` - is the one thing in the flow no schema
    error can explain, because the mistake is in the wrapper, not in a
    parameter. Three shapes are handled, all seen live. Runs before the
    argument filter, which would otherwise drop the parameters first.

    Nothing here guesses at intent: the name is taken from the answer the
    caller itself supplied (in rank order), from the caller's own words, or the
    parameters it already named are moved to where they belong. A re-target
    says so on the payload (`resolved_from`) and in the log (WARNING), and an
    answer whose head is a write is refused rather than run.

    A `name` that re-targets is the one repair with consequences: executing a
    tool the caller did not name, on a server that mutates NextDNS state, is
    kept inside the reads. A multi-entry answer resolving to a WRITE is the
    case where rank order is a guess, and the endpoint map is what makes
    "write" a fact rather than a name-shape heuristic; for a name the map does
    not carry, the tool's own `readOnlyHint` is that fact.
    """

    def __init__(
        self,
        transform: Any = None,
        call_tool_name: str = "call_tool",
        endpoints: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        """`transform` supplies the real catalog under disclosure.

        ``tools/list`` shows only the synthetic pair there, so the names a
        caller may use are not visible to it; the transform's catalog is the
        same list FastMCP's own call_tool proxy dispatches against.

        `endpoints` is the ``tool name -> (method, path template)`` map from
        the server's route parse: the sound read of whether a name executes a
        write. A name the map does not carry - one of the three composite tools
        (``plotAnalytics``, ``dohLookup``, ``overviewDashboard``) or the
        generative-UI pair - is classified from its own ``readOnlyHint``, so a
        tool that runs code is not mistaken for a read.
        """
        self._call_tool_name = call_tool_name
        self._transform = transform
        self._endpoints = endpoints
        self._catalog_memos: dict[tuple[str, ...], _CatalogWords] = {}

    def _is_exact_ready_call(self, supplied: Any, arguments: Mapping[str, Any]) -> bool:
        """A well-formed exact call: the name is a real operation, parameters are inside.

        The catalog fetch exists only to resolve names, and this shape needs
        none: the endpoint map carries every spec operation's name. Parameters
        sent beside ``name`` (the wrapper-shape repair) or an unlisted name
        (aliases, search answers, the three composite operations (plotAnalytics, dohLookup, overviewDashboard)) take the catalog
        path, where resolution happens.
        """
        return (
            isinstance(supplied, str)
            and self._endpoints is not None
            and supplied in self._endpoints
            and set(arguments) <= {"name", "arguments"}
        )

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        message = getattr(context, "message", None)
        arguments = getattr(message, "arguments", None)
        if getattr(message, "name", "") != self._call_tool_name or not isinstance(arguments, dict):
            return await call_next(context)

        supplied = arguments.get("name")
        # Exact-name fast path: the catalog fetch only exists to resolve
        # names, and a well-formed exact call needs no resolution.
        if self._is_exact_ready_call(supplied, arguments):
            return await call_next(context)
        tools = await self._catalog(context)
        resolved, target = await self._resolve_tool_name(supplied, tools)
        shaped = self._shape_arguments(arguments, resolved, target)
        if shaped != arguments:
            context.message.arguments = shaped
        return _annotate_resolved_from(await call_next(context), supplied, resolved)

    def _shape_arguments(self, arguments: dict[str, Any], resolved: str | None, target: Tool | None) -> dict[str, Any]:
        """Rewrite the wrapper: the resolved name, and any parameters lifted into it."""
        shaped = dict(arguments)
        if resolved:
            shaped["name"] = resolved
        if target is None:
            return shaped
        lifted = self._lift_flat_arguments(shaped, target)
        if lifted is not shaped:
            logger.warning("call_tool arguments for %r were sent beside name, not inside arguments", resolved)
            return lifted
        return shaped

    async def _resolve_tool_name(self, supplied: Any, tools: Sequence[Tool]) -> tuple[str | None, Tool | None]:
        """The tool a call names, and the catalog entry it matches - or None, None.

        A name given as a search answer (a list or dict, not a bare string) is
        read out of it - unless the answer holds several named entries and the
        top-ranked one is a write, which is refused (see
        _refuse_ambiguous_write); a name that is not itself a tool but matches
        one once normalised is corrected to it; a name that matches nothing is
        refused with the catalog's own suggestions, because the caller has to
        see what exists to pick again. An empty catalog - no transform, or one
        that could not be read - leaves the name as given rather than refusing
        it.
        """
        resolved = _name_from_search_answer(supplied)
        _note_search_answer_name(supplied, resolved)

        by_name = {tool.name: tool for tool in tools}
        self._refuse_ambiguous_write(supplied, by_name)
        passthrough = _passthrough_resolution(resolved, by_name)
        if passthrough is not None:
            return passthrough
        return self._resolve_against_catalog(resolved, tools, by_name)

    def _resolve_against_catalog(
        self, resolved: str, tools: Sequence[Tool], by_name: dict[str, Tool]
    ) -> tuple[str | None, Tool | None]:
        """The resolution for a name that is not itself a tool.

        An exact name needs no catalog word data: the matching and the
        suggestion ranking exist for names that are NOT tools, so the memo
        is built only when one of those paths actually runs; the first
        unknown request on a catalog builds it and every later one reuses it.
        """
        if resolved in by_name:
            return resolved, by_name[resolved]
        catalog = self._catalog_words(tools)
        entry = _resolved_entry(resolved, by_name, catalog)
        if entry is None:
            raise ToolError(_unknown_tool_message(resolved, tools, catalog))
        if self._loose_write_refused(resolved, by_name, entry):
            raise ToolError(_unknown_tool_message(resolved, tools, catalog))
        return entry

    def _loose_write_refused(self, resolved: str, by_name: dict[str, Tool], entry: tuple[str, Tool]) -> bool:
        """Whether a loosely matched name landed on a write operation."""
        return resolved not in by_name and _is_write_tool(entry[0], self._endpoints, entry[1])

    def _refuse_ambiguous_write(self, supplied: Any, by_name: Mapping[str, Tool]) -> None:
        """Refuse a multi-entry answer whose head is a write; resolve everything else.

        A single-entry answer has one reading, and the caller chose it; a
        multi-entry answer resolved to a write executes a mutation the caller
        never singled out - live, a piped "delete profile" answer would run
        whatever BM25 ranked first. The refusal names the candidates so the
        caller re-sends one name explicitly.
        """
        if not isinstance(supplied, list):
            return
        candidates = _named_entries(supplied)
        head = candidates[0] if candidates else ""
        if len(candidates) > 1 and _is_write_tool(head, self._endpoints, by_name.get(head)):
            raise ToolError(_ambiguous_write_message(candidates))

    @staticmethod
    def _lift_flat_arguments(arguments: dict[str, Any], tool: Tool) -> dict[str, Any]:
        """Move a tool's own parameters out of the wrapper they were sent beside.

        Live, 16 times in one run, on the denylist step::

            call_tool({name: "addToDenylist", profile_id: "81b222", id: "x.com"})

        The caller named the tool and gave its parameters beside ``name``
        instead of inside ``arguments``. They are that tool's parameters, so
        they belong to it; dropping them - which is what the schema filter
        does, since call_tool's own schema does not declare them - leaves an
        empty call rejected for a required argument the caller did supply.

        Only keys the named tool declares are moved, and never over one
        already inside ``arguments``. Anything else is left where it is for the
        filter to report.
        """
        properties = _tool_properties(tool)
        nested = _dict_or_empty(arguments.get("arguments"))
        # The wrapper's own keys, and anything already supplied nested: those
        # stay exactly where they are.
        taken = {"name", "arguments"} | set(nested)
        moved = [key for key in arguments if key in properties and key not in taken]
        if not moved:
            return arguments
        return _moved_into_arguments(arguments, nested, moved)

    def _catalog_words(self, tools: Sequence[Tool]) -> _CatalogWords:
        """The catalog's word structures, memoized by its sorted tool names.

        Unknown-name matching and the suggestion ranking read these
        structures; they are constants of a static catalog, so the first
        unknown request on a catalog builds them and every later one reuses
        them instead of re-normalizing and re-ranking the whole catalog.
        """
        return _catalog_words_memo(self._catalog_memos, tools)

    async def _catalog(self, context: MiddlewareContext) -> Sequence[Tool]:
        """Every tool this server will accept through call_tool."""
        fastmcp_context = getattr(context, "fastmcp_context", None)
        if self._transform is None or fastmcp_context is None:
            return ()
        try:
            return list(await self._transform.get_tool_catalog(fastmcp_context))
        except Exception:
            # A shape check must never break the call: no catalog read, no
            # repair; the call proceeds with the name exactly as sent.
            logger.debug("could not read the tool catalog to shape a call_tool call", exc_info=True)
            return ()
