"""Progressive-disclosure search transforms tuned for small models.

FastMCP's ``BM25SearchTransform`` powers progressive tool disclosure:
``tools/list`` exposes only ``search_tools`` + ``call_tool``, and the model
discovers the real tools by searching. (FastMCP also offers a regex transform;
it is not mounted here — it ranks nothing, and no live validation ran under
it, so it was removed before shipping.)

The transform can return an EMPTY list when a query shares no tokens with any
tool, and it ranks by prose similarity rather than by what the caller asked to
*do*. Small models reliably query with guessed tool names (``updateProfile``,
``replace_parental_services``, ``add dns rewrite record``) rather than task
descriptions, so the search comes back empty and the model then invents a
non-existent tool and fails. This module adds two generic, task-independent
safety nets. Neither is a task hint — both behave identically for every query —
and both are client-agnostic (they live in the MCP server, not in a prompt):

1. Full-catalog fallback: when a search matches nothing, return the catalog.
2. Name-aware re-rank: a tool whose own NAME contains the caller's words ranks
   first, with BM25's order preserved on ties.

SPDX-License-Identifier: MIT
"""

import json
import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool

logger = logging.getLogger(__name__)

# The slice the answer carries. Four could not show a whole operation family:
# a noun-only query ('denylist', 'security tlds') names a family whose members
# all carry the word, so the sixth member the caller still needed
# (replaceDenylist, getSecurityTLDs) was crowded out and the caller acted
# through the wrong sibling (measured live on the target model). Six shows the
# family; the whole-answer byte budget below still bounds what each carries.
CATALOG_MAX_RESULTS = 6

# Per-result budget for the description in a search answer. Sized so the parts
# that decide the call - the list types, the operations, the metrics - fit, and
# the parts that do not (long prose, worked examples) do not ride along on every
# discovery.
SEARCH_DESCRIPTION_CHARS = 1400

# Budget for the whole answer. Four fully-described tools still measured ~10 KB,
# and the run that carried 10-18 KB per discovery stalled 27 times in 29 steps.
SEARCH_ANSWER_MAX_BYTES = 8000

# camelCase / snake_case / non-word splitter, used for both tool names and
# queries so 'queryTimeSeries' and 'query time series' compare equal.
_WORD_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[_\W]+")


def _words(text: str) -> set[str]:
    """Split a tool name or query into comparable words.

    Trailing 's' is stripped from longer words so the plural of a tool's name
    ('manageProfiles' -> 'profile') matches the caller's wording ('delete
    profile'), and '-ies' folds the same way ('categories' -> 'category') so a
    plural query reaches a tool whose name and prose spell it singular. Both
    sides go through this function, so a crude fold is fine as long as it is
    symmetric: it only needs to make a name and a short query comparable, not
    to parse language.
    """
    words = set()
    for raw in _WORD_SPLIT.split(text):
        word = raw.casefold()
        if len(word) > 3 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        if word:
            words.add(word)
    return words


def _catalog_name_words(tools: Sequence[Tool]) -> set[str]:
    """The raw casefolded words of every tool NAME — the guard for variants.

    A partner spelling earns a ride in the query only when a tool name is
    actually spelled that way: 'category' names update...CategoryEntry and
    'categories' names get...Categories. Prose-only partners ('adds',
    'deletes' from the 'Adds.../Deletes...' description boilerplate) describe
    every tool and no tool, so they stay out of the expansion.
    """
    words: set[str] = set()
    for tool in tools:
        words.update(w.casefold() for w in _WORD_SPLIT.split(tool.name) if w)
    return words


def _morphological_partners(word: str) -> set[str]:
    """The plural/singular spellings this word could trade places with."""
    partners = {word + "s", word + "es"}
    if word.endswith("ies"):
        partners.add(word[:-3] + "y")
    elif word.endswith("y"):
        partners.add(word[:-1] + "ies")
    if word.endswith("s"):
        partners.add(word[:-1])
    return partners


def _variant_words(query: str, vocabulary: set[str], exclude: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Plural/singular partners of the query's words that tool names do use.

    bm25 indexes raw text with no stemming, so a query that says 'categories'
    never reaches a tool whose name and prose say 'category' - measured live,
    noun-only queries ('parental categories') left the per-entry update tool
    out of the answer entirely, and the caller bulk-replaced instead. The
    partner spellings ride along as extra query terms, but only when a tool
    name spells them, so the widening adds the missing signal and no guessed
    noise. A word the exclusion names says what every tool is scoped to and
    singles out nothing; widening it only strengthens every tool at once
    ('profiles' made listProfiles outrank deleteProfile for 'delete
    profile'), so it is left alone.
    """
    extra: set[str] = set()
    for raw in _WORD_SPLIT.split(query):
        word = raw.casefold()
        if len(word) < 3 or any(folded in exclude for folded in _words(word)):
            continue
        extra.update(c for c in _morphological_partners(word) if len(c) > 2 and c in vocabulary and c != word)
    return extra


# A word that names what every tool is scoped to, written straight before an
# identifier: `profile 87c444`, `profile_id=87c444`. The identifier is what
# makes it a scope reference rather than a subject.
_SCOPE_MENTION = re.compile(r"""(?i)\b([a-z][a-z0-9_-]*)[\s :=/#'.\-]{0,3}(?:0x)?([0-9a-f]{5,}|\d{4,})['\"]?""")


def _scope_words(tools: Sequence[Tool]) -> set[str]:
    """Words naming the object every tool takes, so they single out no tool.

    `profile_id` is a parameter of all eight tools in this catalog, and every
    task states the profile it acts on, so `profile` in a query says which
    account the work is on - not that the caller wants manageProfiles.
    """
    if len(tools) < 2:
        return set()
    threshold = max(2, (len(tools) + 1) // 2)
    counts: Counter[str] = Counter()
    for tool in tools:
        words: set[str] = set()
        for parameter in (tool.parameters or {}).get("properties") or {}:
            words.update(_words(parameter))
        counts.update(words)
    return {word for word, count in counts.items() if count >= threshold}


def _strip_scope_mentions(query: str, scope_words: set[str]) -> str:
    """Take scope references out of a query before anything scores it.

    Only the pair counts: `profile` on its own is a subject ("delete profile"),
    and stays. A word that is not a scope noun stays too, even before an
    identifier, because then the identifier is the thing being asked about.
    """

    def _replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1).casefold() not in scope_words else " "

    return _SCOPE_MENTION.sub(_replace, query)


def _leading_word(name: str) -> str:
    """The first word of a tool name — the operation that name performs.

    Every ``operationId`` in this document is verb-first (``getX``, ``addX``,
    ``removeX``, ``updateX``, ``plotAnalytics``), so the leading word is the one
    thing that separates two tools over the same object: ``addToDenylist`` and
    ``updateDenylistEntry`` differ in nothing else their name says.
    """
    for raw in _WORD_SPLIT.split(name):
        word = raw.casefold()
        if word:
            return word[:-1] if len(word) > 3 and word.endswith("s") else word
    return ""


def _name_words(tools: Sequence[Tool]) -> dict[str, set[str]]:
    """The words of each tool's name."""
    return {tool.name: set(_words(tool.name)) for tool in tools}


def _vocabulary_words(tools: Sequence[Tool]) -> dict[str, set[str]]:
    """The words each tool's own contract uses: its parameters and accepted values.

    `general` is a value of manageSettings' `category` and of nothing else, so a
    caller that writes "general settings" has named that tool even when it never
    writes the word "settings" the way its name spells it.
    """
    out: dict[str, set[str]] = {}
    for tool in tools:
        words: set[str] = set()
        for parameter, schema in ((tool.parameters or {}).get("properties") or {}).items():
            words.update(_words(parameter))
            accepted = schema.get("enum") if isinstance(schema, dict) else None
            if isinstance(accepted, list):
                for value in accepted:
                    words.update(_words(str(value)))
        out[tool.name] = words
    return out


# The slice of a catalog that stops a word singling a tool out; see _shared_words.
_SHARED_WORD_SHARE = 0.1


def _word_counts(by_tool: dict[str, set[str]]) -> Counter[str]:
    """How many tools carry each word."""
    counts: Counter[str] = Counter()
    for words in by_tool.values():
        counts.update(words)
    return counts


def _shared_words(by_tool: dict[str, set[str]]) -> set[str]:
    """Words carried by enough of the catalog that they single out no tool.

    The share, not the raw count, is what cancels a word. This rule was written
    against an eight-tool catalog, where "two tools carry it" meant a quarter of
    everything; on the 78-operation document a verb like `remove` (six names) or an
    object like `tld` (four) is carried by a small slice and does say which tool the
    caller means, while `get` (thirty names) and `profile` (every tool) still cancel
    themselves out. Counting any second carrier as shared made almost every verb and
    noun cancel out at this catalog size, which left ties broken by alphabetical
    order: "remove zip from security_tlds" answered `getSecurityTLDs`.
    """
    threshold = max(1, math.ceil(len(by_tool) * _SHARED_WORD_SHARE))
    return {word for word, count in _word_counts(by_tool).items() if count > threshold}


def _shared_word_sets(names: dict[str, set[str]], vocabulary: dict[str, set[str]]) -> tuple[set[str], set[str]]:
    """The words that say nothing about which tool is meant, per side of the comparison.

    Two sides are discounted separately on purpose. A word carried by much of the
    catalog's *prose* ("domain", "settings", "profile") describes what tools talk
    about, not what they do, so it cancels on both sides — unless exactly one tool
    carries it in its NAME, which is a signal no vocabulary can override: `settings`
    belongs to one name here, and a caller writing "read the current settings" means
    that tool.

    The exception on the name side is the leading word. Every ``operationId`` in this
    document is verb-first, so within a family of tools over one object the verb is
    the only difference the names carry: ``addToDenylist`` and ``updateDenylistEntry``
    share "denylist", and only "add"/"update" says which job the caller asked for. The
    verbs are numerous enough to be "shared" by the share rule, and discounting them
    was what made "add entry to denylist" answer the update tool.
    """
    vocab_shared = _shared_words(vocabulary)
    unique_to_one_name = {word for word, count in _word_counts(names).items() if count == 1}
    leading = {_leading_word(name) for name in names}
    name_shared = (_shared_words(names) | (vocab_shared - unique_to_one_name)) - leading
    return name_shared, vocab_shared


def _words_score(
    query_words: set[str],
    name_words: set[str] | None,
    vocabulary_words: set[str] | None,
    name_shared: set[str],
    vocab_shared: set[str],
    leading_word: str,
) -> int:
    """How much of the caller's wording one tool's own words account for.

    A name counts twice a vocabulary word, because a name is what the tool is for
    while prose is what it talks about. The name's leading word counts once more
    again: it is the verb, and the verb is the only thing two tools over the same
    object differ on, so "add entry to denylist" has to answer ``addToDenylist``
    rather than ``updateDenylistEntry`` on the strength of the word "add".
    """
    return (
        _overlap(query_words, name_words, name_shared) * 2
        + (1 if leading_word and leading_word in query_words else 0)
        + _overlap(query_words, vocabulary_words, vocab_shared)
    )


def _rank_by_name_overlap(
    tools: Sequence[Tool],
    query: str,
    names: dict[str, set[str]] | None = None,
    vocabulary: dict[str, set[str]] | None = None,
) -> list[Tool]:
    """Order matched tools by how much of the query their own words account for.

    BM25 ranks by prose similarity, so a long description can outrank the tool whose
    name is literally what the caller asked for: "generate analytics plot" put the
    analytics query tools above ``plotAnalytics``, and "delete profile" put four other
    tools above ``deleteProfile``. Ties keep the underlying ranking.

    A word much of the catalog carries is not counted, and a name counts for more than
    a parameter or an accepted value; see :func:`_shared_word_sets` for which words
    those are, and for the verb-first exception that keeps a family of tools over one
    object sortable by what the caller asked to *do* to it.
    """
    names = _name_words(tools) if names is None else names
    vocabulary = _vocabulary_words(tools) if vocabulary is None else vocabulary
    name_shared, vocab_shared = _shared_word_sets(names, vocabulary)
    leading = {name: _leading_word(name) for name in names}
    query_words = _words(query)
    if not query_words:
        return list(tools)
    indexed = list(enumerate(tools))
    indexed.sort(
        key=lambda pair: (
            -_words_score(
                query_words,
                names.get(pair[1].name),
                vocabulary.get(pair[1].name),
                name_shared,
                vocab_shared,
                leading.get(pair[1].name, ""),
            ),
            pair[0],
        )
    )
    return [tool for _, tool in indexed]


def _overlap(query_words: set[str], words: set[str] | None, shared: set[str]) -> int:
    """How many of a tool's own words the caller used."""
    return len(query_words & (words - shared)) if words else 0


class _CatalogAwareSearchMixin:
    """Full-catalog fallback for empty results, plus the name-aware re-rank.

    Mixin for the FastMCP search transforms. Keeps the disclosure flow intact
    (tools/list still hides the real tools; the model must still search then
    call_tool) but makes discovery robust to unhelpful queries.
    """

    _max_results: int

    def _augment_query(self, query: str, variants: set[str]) -> str:
        """Widen a lexical query with the catalog's own other spellings.

        bm25 indexes raw text with no stemming, so the partner spellings ride
        along as extra terms (see _variant_words). Without variants, the
        caller's query is exactly what was asked and stays untouched.
        """
        if not variants:
            return query
        return " ".join([query, *sorted(variants)])

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        # A scope reference is scored out before anything else, because it is
        # the one phrase every task in this API contains and the one that most
        # often points the ranking at the wrong tool.
        stripped = _strip_scope_mentions(query, _scope_words(tools))
        names = _name_words(tools)
        vocabulary = _vocabulary_words(tools)
        # The same discounting the re-rank uses, so a tool the caller named with a
        # verb ("add ... to the denylist") is kept in the answer rather than dropped
        # because the verb is common: within a family of tools the verb is the only
        # thing their names differ on.
        name_shared, vocab_shared = _shared_word_sets(names, vocabulary)
        # Only the scoring query widens; the name-inclusion below stays on the
        # caller's own words, which is what "named" has always meant. Shared
        # scope words are never widened - they name every tool at once.
        scored = self._augment_query(
            stripped,
            _variant_words(stripped, _catalog_name_words(tools), exclude=name_shared | vocab_shared),
        )
        results = list(await super()._search(tools, scored) or [])  # type: ignore[misc]
        # A tool whose own name carries the caller's word is never left out of
        # the answer. BM25 ranks prose, so which prose wins changes when a
        # description gets longer: publishing the full docstrings took `doh`
        # from dohLookup to queryAnalytics, and the tool named for the request
        # dropped out of the result set entirely.
        words = _words(stripped) - name_shared
        named = {name for name, words_in_name in names.items() if words & words_in_name}
        if not results:
            # Nothing matched at all: the caller's words are the only signal
            # left, so answer with the catalog and let the name order sort it.
            results = list(tools)
        merged = _merge_named_results(results, tools, named)
        return _rank_by_name_overlap(merged[: self._max_results], stripped, names, vocabulary)

    def _make_search_tool(self) -> Tool:
        """FastMCP's search tool, with this server's discovery contract on it.

        Under disclosure these two synthetic tools are the only entries in
        ``tools/list``, so their descriptions are the always-visible place to
        state how the server is meant to be used. FastMCP's own wording is a
        one-liner that never says the catalog is hidden or that guessing fails.
        """
        tool = super()._make_search_tool()  # type: ignore[misc]
        tool.description = _SEARCH_TOOL_DESCRIPTION
        return tool

    def _make_call_tool(self) -> Tool:
        tool = super()._make_call_tool()  # type: ignore[misc]
        tool.description = _CALL_TOOL_DESCRIPTION
        return tool


def _merge_named_results(results: Sequence[Tool], tools: Sequence[Tool], named: set[str]) -> list[Tool]:
    """Put every tool the caller named into the answer, keeping BM25's order.

    A tool the caller named is never left out, but one BM25 already ranked keeps
    the place BM25 gave it: when two tools tie on the words they carry, the tie
    should fall back to how well their descriptions matched, not to the order
    the tools happen to be registered in. So: named-and-ranked first, then
    named-but-unranked, then everything else in ranked order.
    """
    ranked_names = {tool.name for tool in results}
    picked = [tool for tool in results if tool.name in named]
    missing = [tool for tool in tools if tool.name in named and tool.name not in ranked_names]
    rest = [tool for tool in results if tool.name not in named]
    return picked + missing + rest


# Written for the model that reads them, and client-agnostic: nothing here
# assumes an agent CLI, a code-mode sandbox, or any particular call syntax, and
# nothing instructs the model how to behave — it describes what this server does.
# The argument shape is spelled out because it is part of the contract, and
# getting it wrong is the single most common way a call never reaches a tool:
# observed live, a model passed a bare string to search_tools and two objects to
# call_tool, and concluded from the rejections that the server had no tools.
_SEARCH_TOOL_DESCRIPTION = """Search this server's tools with the words of the task.

Takes one argument object, {"query": "<words of the task>"}. The answer is an
array of the matching tools in rank order, each an object with its exact `name`,
its `description`, and its full input schema. A name that search_tools did not
return, and that the server's instructions do not list, is not a tool of this
server.
"""

_CALL_TOOL_DESCRIPTION = """Run one of this server's tools by the exact name returned by search_tools.

Takes exactly one argument object, {"name": "<tool>", "arguments": {<that tool's
parameters>}}; the name and the arguments are not two separate inputs. `name`
selects one tool: one search result's `name` field. If the search answer itself
is passed instead - the array of results, or one result object - it resolves to
the highest-ranked tool in it, because the answer is in rank order. Anything
search_tools did not return is not a tool of this server. A failed call returns
a JSON payload with an error `code` and, when available, a `hint`.
"""


class FallbackBM25SearchTransform(_CatalogAwareSearchMixin, BM25SearchTransform):
    """BM25 search with a full-catalog fallback and name-aware ranking."""


def _compact_search_results(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    """FastMCP's search-result shape, with each description cut to a budget.

    A tool's own description is its full contract, and under disclosure the
    search answer is the only place a caller reads it. But the answer is also
    the biggest thing the model has to hold per discovery: with the full text
    and four results it measured 10-18 KB for a one-line task, and the run that
    did that stalled 27 times across 29 steps. So the search payload carries a
    bounded excerpt - cut at a line boundary, never mid-sentence - and the
    input schema, which is the part that decides whether the call is valid, is
    never cut.
    """
    results: list[dict[str, Any]] = []
    spent = 0
    for tool in tools:
        dumped = tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        description = str(dumped.get("description") or "")
        if len(description) > SEARCH_DESCRIPTION_CHARS:
            cut = description.rfind("\n", 0, SEARCH_DESCRIPTION_CHARS)
            if cut < SEARCH_DESCRIPTION_CHARS // 2:
                cut = SEARCH_DESCRIPTION_CHARS
            dumped["description"] = description[:cut].rstrip() + "\n[description truncated]"
        # The whole answer is bounded too: four fully-described tools is still
        # ~10 KB, and it is the lower-ranked ones that are least likely to be
        # the tool the caller wanted. At least one result is always returned.
        cost = len(json.dumps(dumped, sort_keys=True))
        if results and spent + cost > SEARCH_ANSWER_MAX_BYTES:
            break
        spent += cost
        results.append(dumped)
    return results


def _closest_tools(name: str, tools: Sequence[Tool], limit: int = 3) -> list[Tool]:
    """The tools a typed name's words come closest to, ranked as search_tools ranks them.

    A caller that types ``denylists.updateDenylistEntry`` or "generate analytics
    plot" has described a task in this catalog's vocabulary without naming a
    tool. Ranking those words with the rule search_tools already uses turns a
    dead end into a correction the caller can act on.
    """
    if not tools:
        return []
    names = _name_words(tools)
    vocabulary = _vocabulary_words(tools)
    name_shared, vocab_shared = _shared_word_sets(names, vocabulary)
    leading = {name: _leading_word(name) for name in names}
    query_words = _words(name)
    scored = [
        (
            _words_score(
                query_words,
                names.get(tool.name),
                vocabulary.get(tool.name),
                name_shared,
                vocab_shared,
                leading.get(tool.name, ""),
            ),
            tool,
        )
        for tool in tools
    ]
    scored.sort(key=lambda pair: -pair[0])
    return [tool for score, tool in scored[:limit] if score > 0]


def _required_arguments(tool: Tool) -> str:
    """The arguments a tool cannot be called without, read from its own schema."""
    required = [str(item) for item in ((tool.parameters or {}).get("required") or [])]
    return ", ".join(required) if required else "no arguments"


def _unknown_tool_message(name: str, tools: Sequence[Tool]) -> str:
    """Say what the caller can call instead of a name that is not a tool.

    FastMCP's own answer is ``Unknown tool: '<x>'``. Under disclosure the caller
    cannot see the names that do exist, so the only way out is to search again -
    and a caller that typed a name has already decided it knows the tool. Live,
    it guesses again instead, shorter each time.
    """
    closest = _closest_tools(name, tools)
    if not closest:
        return (
            f"{name!r} is not a tool of this server. search_tools(query) returns "
            "the tools there are, with the exact name each one takes."
        )
    listed = "; ".join(f"{tool.name} takes {_required_arguments(tool)}" for tool in closest)
    return (
        f"{name!r} is not a tool of this server. Its words match: {listed}. call_tool takes one of those names exactly."
    )


def _match_tool_name(name: str, names: Sequence[str]) -> str:
    """The catalog entry a mistyped name unambiguously means, or ''.

    Two readings only, both exact. The same name with separators and case
    removed: ``manage_profiles`` and ``manageprofiles`` are manageProfiles. The
    same words in another order: ``analytics/plot`` is plotAnalytics, because
    the caller used that tool's own words and nothing else. A near miss that is
    neither is left alone - the caller gets the diagnosis, not a guess.

    A reading is dropped when two catalog entries satisfy it, so an ambiguous
    name is never resolved.
    """
    if not name or name in names:
        return ""
    flat = _WORD_SPLIT.sub("", name.casefold())
    flat_matches = [candidate for candidate in names if _WORD_SPLIT.sub("", candidate.casefold()) == flat]
    if len(flat_matches) == 1:
        return flat_matches[0]
    words = _words(name)
    word_matches = [candidate for candidate in names if _words(candidate) == words]
    return word_matches[0] if len(word_matches) == 1 else ""


def _name_from_search_answer(value: Any) -> str:
    """The tool name a caller meant, when it sent the search answer instead.

    The single most common way a call never reaches a tool, seen repeatedly
    across live runs, is this expression::

        search_tools({query}).then(result => call_tool({ name: result.result, … }))

    `result.result` is the answer - an array of tool objects - and FastMCP
    rejects it against call_tool's own `name: str`, so nothing runs. The answer
    is in rank order and the caller passed all of it, so the top-ranked entry is
    the only defensible reading; taking it is not a guess about intent.

    Returns '' when the value cannot be read as a name, leaving the schema
    error to fire unchanged.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) else ""
    if isinstance(value, list):
        for entry in value:
            name = _name_from_search_answer(entry)
            if name:
                return name
    return ""


def _tool_properties(tool: Tool) -> dict[str, Any]:
    """The parameters a tool declares, as a name -> schema mapping (empty if none)."""
    return (tool.parameters or {}).get("properties") or {}


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


class CallToolShapeMiddleware(Middleware):
    """Make ``call_tool`` mean the call the caller was trying to make.

    Under disclosure the two synthetic tools are the only way in, and their
    shape - ``{name, arguments}`` - is the one thing in the flow no schema
    error can explain, because the mistake is in the wrapper, not in a
    parameter. Three shapes are handled, all seen live. Runs before the
    argument filter, which would otherwise drop the parameters first.

    Nothing here guesses at intent: the name is taken from the answer the
    caller itself supplied (in rank order), from the caller's own words, or the
    parameters it already named are moved to where they belong.
    """

    def __init__(self, transform: Any = None, call_tool_name: str = "call_tool") -> None:
        """`transform` supplies the real catalog under disclosure.

        ``tools/list`` shows only the synthetic pair there, so the names a
        caller may use are not visible to it; the transform's catalog is the
        same list FastMCP's own call_tool proxy dispatches against.
        """
        self._call_tool_name = call_tool_name
        self._transform = transform

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        message = getattr(context, "message", None)
        arguments = getattr(message, "arguments", None)
        if getattr(message, "name", "") != self._call_tool_name or not isinstance(arguments, dict):
            return await call_next(context)

        tools = await self._catalog(context)
        resolved, target = await self._resolve_tool_name(arguments.get("name"), tools)

        shaped = dict(arguments)
        if resolved:
            shaped["name"] = resolved
        if target is not None:
            lifted = self._lift_flat_arguments(shaped, target)
            if lifted is not shaped:
                logger.info("call_tool arguments for %r were sent beside name, not inside arguments", resolved)
                shaped = lifted

        if shaped != arguments:
            context.message.arguments = shaped
        return await call_next(context)

    async def _resolve_tool_name(self, supplied: Any, tools: Sequence[Tool]) -> tuple[str | None, Tool | None]:
        """The tool a call names, and the catalog entry it matches - or None, None.

        A name given as a search answer (a list or dict, not a bare string) is
        read out of it; a name that is not itself a tool but matches one once
        normalised is corrected to it; a name that matches nothing is refused
        with the catalog's own suggestions, because the caller has to see what
        exists to pick again. An empty catalog - no transform, or one that
        could not be read - leaves the name as given rather than refusing it.
        """
        resolved = _name_from_search_answer(supplied)
        if resolved and resolved != supplied:
            logger.info("call_tool name given as %s; resolved to the top-ranked %r", type(supplied).__name__, resolved)

        by_name = {tool.name: tool for tool in tools}
        if not resolved or not by_name or resolved in by_name:
            return resolved, by_name.get(resolved) if resolved else None

        matched = _match_tool_name(resolved, list(by_name))
        if matched:
            logger.info("call_tool name %r is not a tool; it matches %r once normalised", resolved, matched)
            return matched, by_name[matched]
        raise ToolError(_unknown_tool_message(resolved, tools))

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

    async def _catalog(self, context: MiddlewareContext) -> Sequence[Tool]:
        """Every tool this server will accept through call_tool."""
        fastmcp_context = getattr(context, "fastmcp_context", None)
        if self._transform is None or fastmcp_context is None:
            return ()
        try:
            return list(await self._transform.get_tool_catalog(fastmcp_context))
        except Exception:  # pragma: no cover - a shape check must never break the call
            logger.debug("could not read the tool catalog to shape a call_tool call", exc_info=True)
            return ()


def build_search_transform(mode: str) -> BM25SearchTransform | None:
    """Return the configured search transform for ``mode`` (``off`` -> None)."""
    if mode == "bm25":
        return FallbackBM25SearchTransform(
            max_results=CATALOG_MAX_RESULTS, search_result_serializer=_compact_search_results
        )
    return None
