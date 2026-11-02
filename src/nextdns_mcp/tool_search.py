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

The two synthetic entry points keep FastMCP's fixed names: ``search_tools`` is
plural and ``call_tool`` singular — the asymmetry is FastMCP's contract, not a
local inconsistency to rename.

SPDX-License-Identifier: MIT
"""

import json
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool

from .config import ConfigurationError
from .constants import is_write_operation
from .utils import schema_required

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


def _fold_word(word: str) -> str:
    """Fold a plural word to its singular stem ('categories' -> 'category', 'profiles' -> 'profile')."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    """Split a tool name or query into comparable words.

    Trailing 's' is stripped from longer words so the plural of a tool's name
    ('profiles' -> 'profile') matches the caller's wording ('delete profile'),
    and '-ies' folds the same way ('categories' -> 'category') so a plural
    query reaches a tool whose name and prose spell it singular. Both
    sides go through this function, so a crude fold is fine as long as it is
    symmetric: it only needs to make a name and a short query comparable, not
    to parse language.
    """
    return {_fold_word(raw.casefold()) for raw in _WORD_SPLIT.split(text) if raw.casefold()}


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


def _usable_partners(word: str, vocabulary: set[str]) -> set[str]:
    """Partner spellings that are real catalog words and differ from the word itself."""
    return {p for p in _morphological_partners(word) if len(p) > 2 and p in vocabulary and p != word}


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
        extra.update(_usable_partners(word, vocabulary))
    return extra


# A word that names what every tool is scoped to, written straight before an
# identifier with a real separator between them: `profile 87c444`,
# `profile_id=87c444`. The separator is what makes the pair a reference; an
# identifier with the word fused into one token (`TEST_PROFILE_20261029`) is a
# name, not a scope mention, and the empty separator keeps it.
_SCOPE_MENTION = re.compile(r"""(?i)\b([a-z][a-z0-9_-]*)([\s :=/#'.\-]{0,3})(?:0x)?([0-9a-f]{5,}|\d{4,})['\"]?""")


def _parameter_words(tool: Tool) -> set[str]:
    """The words of one tool's parameter names."""
    words: set[str] = set()
    for parameter in _tool_properties(tool):
        words.update(_words(parameter))
    return words


def _scope_words(tools: Sequence[Tool]) -> set[str]:
    """Words naming the object every tool takes, so they single out no tool.

    `profile_id` is a parameter of nearly every tool in this 78-operation
    catalog, and every task states the profile it acts on, so `profile` in a
    query says which account the work is on - not which tool the caller wants.
    """
    if len(tools) < 2:
        return set()
    threshold = max(2, (len(tools) + 1) // 2)
    counts: Counter[str] = Counter()
    for tool in tools:
        counts.update(_parameter_words(tool))
    return {word for word, count in counts.items() if count >= threshold}


def _strip_scope_mentions(query: str, scope_words: set[str]) -> str:
    """Take scope references out of a query before anything scores it.

    Only the pair counts: `profile` on its own is a subject ("delete profile"),
    and stays. A word that is not a scope noun stays too, even before an
    identifier, because then the identifier is the thing being asked about.
    The captured token is compared through :func:`_words` because the regex
    keeps a compound spelling whole (`profile_id`) while the scope set holds
    the split folded words (`profile`, `id`); without the split the `=`-mention
    the comment above cites never matched, measured live.
    """

    def _replace(match: re.Match[str]) -> str:
        separator = match.group(2)
        if separator and _words(match.group(1)) & scope_words:
            return " "
        return match.group(0)

    return _SCOPE_MENTION.sub(_replace, query)


def _leading_word(name: str) -> str:
    """The first word of a tool name — the operation that name performs.

    Every ``operationId`` in this document is verb-first (``getX``, ``addX``,
    ``removeX``, ``updateX``, ``plotAnalytics``), so the leading word is the one
    thing that separates two tools over the same object: ``addToDenylist`` and
    ``updateDenylistEntry`` differ in nothing else their name says. It folds
    through :func:`_fold_word` like every other word, so a verb spelled plural
    compares equal to the query's singular.
    """
    for raw in _WORD_SPLIT.split(name):
        word = raw.casefold()
        if word:
            return _fold_word(word)
    return ""


def _name_words(tools: Sequence[Tool]) -> dict[str, set[str]]:
    """The words of each tool's name."""
    return {tool.name: set(_words(tool.name)) for tool in tools}


def _accepted_value_words(schema: Any) -> set[str]:
    """The words of a parameter's accepted enum values ('countries' names the analytics `type`)."""
    accepted = schema.get("enum") if isinstance(schema, dict) else None
    if not isinstance(accepted, list):
        return set()
    return {word for value in accepted for word in _words(str(value))}


def _vocabulary_words(tools: Sequence[Tool]) -> dict[str, set[str]]:
    """The words each tool's own contract uses: its parameters and accepted values.

    `countries` is an accepted value of the analytics-destination tools' `type`
    and of nothing else, so a caller that writes "countries" has named that
    family even when it never writes the words the tool's name spells.
    """
    out: dict[str, set[str]] = {}
    for tool in tools:
        words: set[str] = set()
        for parameter, schema in _tool_properties(tool).items():
            words.update(_words(parameter))
            words.update(_accepted_value_words(schema))
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
    carries it in its NAME, which is a signal no vocabulary can override: `clear`
    belongs to one name here (clearLogs), and a caller writing "clear the logs"
    means that tool whatever the rest of the catalog's prose says.

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


def _generic_leading_words(names: dict[str, set[str]]) -> set[str]:
    """Leading words that lead more than one name - verbs, not identities.

    A leading word unique to one name (`doh` in dohLookup, `plot` in
    plotAnalytics) IS that tool's word, and a query saying it names the tool.
    A word leading a dozen names (`get`, `update`, `delete`) says what to do,
    not which tool, so it carries weight only beside a second word the tool
    itself matches - otherwise "get rewrites" reaches getPrivacySettings on
    `get` alone and outranks listRewrites, measured live.
    """
    counts = Counter(_leading_word(name) for name in names)
    return {word for word, count in counts.items() if count > 1}


def _matches_beyond_verb(
    query_words: set[str],
    name_words: set[str] | None,
    vocabulary_words: set[str] | None,
    leading_word: str,
    name_shared: set[str],
    vocab_shared: set[str],
) -> bool:
    """Whether the query reaches a tool on an INFORMATIVE word that is not its verb.

    The second word must single the tool out, not just describe every tool:
    "get profile" rides on `profile`, a word every tool's contract carries, and
    says nothing about which tool the caller wants - that is the scope-word rule
    again, so a shared word does not open the gate. A word in the tool's name or
    accepted values that the rest of the catalog does not carry ("privacy" for
    the privacy tools' updater, "performance" for updatePerformanceSettings) is
    the query naming a subject.
    """
    informative = ((name_words or set()) - name_shared) | ((vocabulary_words or set()) - vocab_shared)
    return bool((query_words - {leading_word}) & informative)


def _verb_counts(
    query_words: set[str],
    name_words: set[str] | None,
    vocabulary_words: set[str] | None,
    leading_word: str,
    generic_verbs: set[str] | frozenset[str],
    name_shared: set[str],
    vocab_shared: set[str],
) -> bool:
    """Whether a name's leading word carries weight for this query."""
    if leading_word not in generic_verbs:
        return True
    return _matches_beyond_verb(query_words, name_words, vocabulary_words, leading_word, name_shared, vocab_shared)


def _words_score(
    query_words: set[str],
    name_words: set[str] | None,
    vocabulary_words: set[str] | None,
    name_shared: set[str],
    vocab_shared: set[str],
    leading_word: str,
    generic_verbs: set[str] | frozenset[str] = frozenset(),
) -> int:
    """How much of the caller's wording one tool's own words account for.

    A name counts twice a vocabulary word, because a name is what the tool is for
    while prose is what it talks about. The name's leading word counts once more
    again: it is the verb, and the verb is the only thing two tools over the same
    object differ on, so "add entry to denylist" has to answer ``addToDenylist``
    rather than ``updateDenylistEntry`` on the strength of the word "add". A verb
    generic to the catalog (`get`, `update`) earns that weight - its name hit and
    its bonus - only when an informative second word also reaches the tool; a
    query of verb plus shared scope word ("update profile") says what to do and
    where, not to what, and must not lift every ``update*`` tool at once.
    """
    name_hits = query_words & (name_words - name_shared) if name_words else set()
    verb_hit = leading_word in query_words
    gate = _verb_counts(
        query_words, name_words, vocabulary_words, leading_word, generic_verbs, name_shared, vocab_shared
    )
    if verb_hit and not gate:
        name_hits -= {leading_word}
        verb_hit = False
    return len(name_hits) * 2 + int(verb_hit) + _overlap(query_words, vocabulary_words, vocab_shared)


def _tool_named_by(
    query_words: set[str],
    name_words: set[str],
    vocabulary_words: set[str] | None,
    leading_word: str,
    generic_verbs: set[str] | frozenset[str],
    name_shared: set[str],
    vocab_shared: set[str],
) -> bool:
    """Whether the caller's own words name this tool (see _named_tools)."""
    if (query_words & name_words) - {leading_word}:
        return True
    verb_match = leading_word in query_words and leading_word in name_words
    return verb_match and _verb_counts(
        query_words, name_words, vocabulary_words, leading_word, generic_verbs, name_shared, vocab_shared
    )


def _named_tools(
    query_words: set[str],
    names: dict[str, set[str]],
    vocabulary: dict[str, set[str]],
    name_shared: set[str],
    vocab_shared: set[str],
    leading: Mapping[str, str],
    generic_verbs: set[str] | frozenset[str],
) -> set[str]:
    """Tools the caller's own words reach by name, with the generic-verb gate.

    Inclusion rides on the UNDISCOUNTED name overlap minus the leading verb:
    a tool the caller worded, even with a catalog-shared word ("profile" in
    deleteProfile), is never left out of the answer. A tool reached ONLY by a
    verb generic to the catalog is, unless an informative second word also
    reaches it - that verb reaches a dozen tools at once, and it used to drag
    every ``get*`` tool into the answer for "get rewrites" while listRewrites,
    the tool the query actually named, fought for its place.
    """
    return {
        name
        for name, name_words in names.items()
        if _tool_named_by(
            query_words,
            name_words,
            vocabulary.get(name),
            leading.get(name, ""),
            generic_verbs,
            name_shared,
            vocab_shared,
        )
    }


def _word_scorer(
    query_words: set[str],
    names: dict[str, set[str]],
    vocabulary: dict[str, set[str]],
    shared: tuple[set[str], set[str]] | None = None,
) -> Callable[[str], int]:
    """A scorer for how much of the query each tool's own words account for.

    Binds the catalog-wide discounting (shared names/values) and the leading-verb
    exception once, so ranking callers score by tool name alone. `shared` carries
    the precomputed ``(name_shared, vocab_shared)`` when the caller already has
    them (the transform memoizes them per catalog); without it they are derived
    here, which is what direct callers of this function expect.
    """
    name_shared, vocab_shared = _shared_word_sets(names, vocabulary) if shared is None else shared
    leading = {name: _leading_word(name) for name in names}
    generic = frozenset(_generic_leading_words(names))
    return lambda name: _words_score(
        query_words,
        names.get(name),
        vocabulary.get(name),
        name_shared,
        vocab_shared,
        leading.get(name, ""),
        generic,
    )


def _rank_by_name_overlap(
    tools: Sequence[Tool],
    query: str,
    names: dict[str, set[str]] | None = None,
    vocabulary: dict[str, set[str]] | None = None,
    shared: tuple[set[str], set[str]] | None = None,
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

    A tie on the weighted score breaks on the UNDISCOUNTED name overlap - how many
    of the caller's words the tool's name simply contains - before falling through
    to the underlying rank: the shared-word discount made "delete profile" score
    equal for deleteProfile and deleteRewrite (both match on the verb "delete",
    "profile" is discounted), and only the raw name overlap says the caller also
    spelled deleteProfile itself. `names`/`vocabulary`/`shared` let the transform
    hand in its memoized catalog structures instead of rebuilding them.
    """
    names = _name_words(tools) if names is None else names
    vocabulary = _vocabulary_words(tools) if vocabulary is None else vocabulary
    query_words = _words(query)
    if not query_words:
        return list(tools)
    scorer = _word_scorer(query_words, names, vocabulary, shared)

    def _key(pair: tuple[int, Tool]) -> tuple[int, int, int]:
        index, tool = pair
        raw_overlap = len(query_words & names.get(tool.name, set()))
        return (-scorer(tool.name), -raw_overlap, index)

    indexed = sorted(enumerate(tools), key=_key)
    return [tool for _, tool in indexed]


def _overlap(query_words: set[str], words: set[str] | None, shared: set[str]) -> int:
    """How many of a tool's own words the caller used."""
    return len(query_words & (words - shared)) if words else 0


@dataclass
class _CatalogWords:
    """One catalog's word structures, computed once and reused on every query.

    The catalog is registered at startup and static while the server runs, so
    every structure here is a constant of its tool NAMES; the transform's memo
    keys on the sorted names, and a catalog under a different set of names
    rebuilds them. FastMCP's own content hash still detects re-registered
    descriptions for the BM25 index; this memo deliberately keys on names only,
    because names are what these structures are keyed by.
    """

    names: dict[str, set[str]]
    vocabulary: dict[str, set[str]]
    leading: dict[str, str]
    generic_verbs: frozenset[str]
    scope: set[str]
    raw_names: set[str]
    name_shared: set[str]
    vocab_shared: set[str]


def _catalog_words_for(tools: Sequence[Tool]) -> _CatalogWords:
    """Compute every catalog-derived word structure once (see _CatalogWords)."""
    names = _name_words(tools)
    vocabulary = _vocabulary_words(tools)
    name_shared, vocab_shared = _shared_word_sets(names, vocabulary)
    return _CatalogWords(
        names=names,
        vocabulary=vocabulary,
        leading={name: _leading_word(name) for name in names},
        generic_verbs=frozenset(_generic_leading_words(names)),
        scope=_scope_words(tools),
        raw_names=_catalog_name_words(tools),
        name_shared=name_shared,
        vocab_shared=vocab_shared,
    )


class FallbackBM25SearchTransform(BM25SearchTransform):
    """BM25 search with a full-catalog fallback and name-aware ranking.

    Keeps the disclosure flow intact (tools/list still hides the real tools;
    the model must still search then call_tool) but makes discovery robust to
    unhelpful queries. FastMCP keeps ``_search``, ``_make_search_tool``,
    ``_make_call_tool`` and ``_render_results`` private, and
    ``BaseSearchTransform.__init__`` sets ``_max_results`` privately too; this
    subclass borrows exactly that surface, on the only subclass there is, and
    pins ``fastmcp==4.1.2`` with it - the annotations below name the borrowed
    attributes so a FastMCP bump finds them instead of failing at request time.

    The catalog-derived word structures, the compacted per-tool payloads, and
    the two synthetic Tools are memoized on the instance: all of them are
    constants of a static catalog, and rebuilding them per query was the
    measured hot path.
    """

    _max_results: int  # private attribute set by BaseSearchTransform.__init__

    def __init__(
        self,
        *,
        max_results: int = 5,
        always_visible: list[str] | None = None,
        search_tool_name: str = "search_tools",
        call_tool_name: str = "call_tool",
        search_result_serializer: Any = None,
    ) -> None:
        super().__init__(
            max_results=max_results,
            always_visible=always_visible,
            search_tool_name=search_tool_name,
            call_tool_name=call_tool_name,
            search_result_serializer=search_result_serializer,
        )
        self._word_memos: dict[tuple[str, ...], _CatalogWords] = {}
        self._compacted_cache: dict[int, tuple[Tool, dict[str, Any], int]] = {}
        self._synthetic_tools: dict[str, Tool] = {}

    def _catalog_words(self, tools: Sequence[Tool]) -> _CatalogWords:
        """The catalog's word structures, memoized by its sorted tool names."""
        key = tuple(sorted(tool.name for tool in tools))
        memo = self._word_memos.get(key)
        if memo is None:
            memo = _catalog_words_for(tools)
            self._word_memos[key] = memo
        return memo

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
        """BM25 with named-tool rescue and fallback, then the name re-rank.

        The re-rank runs over the FULL merged list and the slice comes last:
        slicing to max_results first cut tools that belonged in the answer
        before the name scorer ever saw them, worst on the fallback path where
        the ranking is the only signal left.
        """
        # A scope reference is scored out before anything else, because it is
        # the one phrase every task in this API contains and the one that most
        # often points the ranking at the wrong tool.
        catalog = self._catalog_words(tools)
        stripped = _strip_scope_mentions(query, catalog.scope)
        # Only the scoring query widens; the name-inclusion below stays on the
        # caller's own words, which is what "named" has always meant. Shared
        # scope words are never widened - they name every tool at once.
        scored = self._augment_query(
            stripped,
            _variant_words(stripped, catalog.raw_names, exclude=catalog.name_shared | catalog.vocab_shared),
        )
        results = list(await super()._search(tools, scored) or [])
        # A tool whose own name carries the caller's word is never left out of
        # the answer. BM25 ranks prose, so which prose wins changes when a
        # description gets longer: publishing the full docstrings once moved
        # the `doh` match off the tool named for it, and that tool dropped out
        # of the result set entirely.
        words = _words(stripped)
        named = _named_tools(
            words,
            catalog.names,
            catalog.vocabulary,
            catalog.name_shared,
            catalog.vocab_shared,
            catalog.leading,
            catalog.generic_verbs,
        )
        if not results:
            # Nothing matched at all: the caller's words are the only signal
            # left, so answer with the catalog and let the name order sort it.
            results = list(tools)
        merged = _merge_named_results(results, tools, named)
        shared = (catalog.name_shared, catalog.vocab_shared)
        ranked = _rank_by_name_overlap(merged, stripped, catalog.names, catalog.vocabulary, shared)
        return ranked[: self._max_results]

    def _make_search_tool(self) -> Tool:
        """FastMCP's search tool, with this server's discovery contract on it.

        Under disclosure these two synthetic tools are the only entries in
        ``tools/list``, so their descriptions are the always-visible place to
        state how the server is meant to be used. FastMCP's own wording is a
        one-liner that never says the catalog is hidden or that guessing fails.
        FastMCP rebuilds a Tool on every listing; these two are constants of
        this transform, so the first build is the one kept.
        """
        tool = self._synthetic_tools.get("search")
        if tool is None:
            tool = super()._make_search_tool()
            tool.description = _SEARCH_TOOL_DESCRIPTION
            self._synthetic_tools["search"] = tool
        return tool

    def _make_call_tool(self) -> Tool:
        """FastMCP's call_tool proxy, with this server's contract on it (cached)."""
        tool = self._synthetic_tools.get("call")
        if tool is None:
            tool = super()._make_call_tool()
            tool.description = _CALL_TOOL_DESCRIPTION
            self._synthetic_tools["call"] = tool
        return tool

    async def _render_results(self, tools: Sequence[Tool]) -> Any:
        """The search answer, rendered from the cached compaction (see _serialize_results)."""
        return self._serialize_results(tools)

    def _serialize_results(self, tools: Sequence[Tool]) -> list[dict[str, Any]]:
        """FastMCP's search shape, from the cache, under the whole-answer budget.

        Each kept entry is a shallow copy: the cached dict is never handed to a
        caller, so nothing downstream can mutate the cache through an answer.
        """
        return [dict(dumped) for dumped in _budget_results([self._cached_compaction(tool) for tool in tools])]

    def _cached_compaction(self, tool: Tool) -> tuple[dict[str, Any], int]:
        """One tool's compacted payload and byte cost, cached per Tool instance.

        Keyed on the instance's id, with a strong reference to the Tool kept in
        the entry so an id cannot be recycled under it. The catalog is
        registered once at startup; a re-registered tool is a fresh instance
        with a fresh entry, and the old entry - never hit again - lives until
        the process does. Same static-catalog assumption as the word memo.
        """
        cached = self._compacted_cache.get(id(tool))
        if cached is None:
            dumped, cost = _compact_entry(tool)
            cached = (tool, dumped, cost)
            self._compacted_cache[id(tool)] = cached
        return cached[1], cached[2]


def _partition_named(results: Sequence[Tool], named: set[str]) -> tuple[list[Tool], list[Tool]]:
    """Split ranked results into the ones the caller named and the ones it didn't."""
    picked: list[Tool] = []
    rest: list[Tool] = []
    for tool in results:
        (picked if tool.name in named else rest).append(tool)
    return picked, rest


def _merge_named_results(results: Sequence[Tool], tools: Sequence[Tool], named: set[str]) -> list[Tool]:
    """Put every tool the caller named into the answer, keeping BM25's order.

    A tool the caller named is never left out, but one BM25 already ranked keeps
    the place BM25 gave it: when two tools tie on the words they carry, the tie
    should fall back to how well their descriptions matched, not to the order
    the tools happen to be registered in. So: named-and-ranked first, then
    named-but-unranked, then everything else in ranked order.
    """
    picked, rest = _partition_named(results, named)
    ranked_names = {tool.name for tool in results}
    missing = [tool for tool in tools if tool.name in named and tool.name not in ranked_names]
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
the highest-ranked tool in it, because the answer is in rank order; a
multi-result answer whose highest-ranked tool is a write is refused rather than
executed, and the refusal names the candidates so the call can be re-sent with
one name. Anything search_tools did not return is not a tool of this server. A
failed call returns a JSON payload with an error `code` and, when available, a
`hint`.
"""


def _truncate_description(dumped: dict[str, Any]) -> None:
    """Cut one dumped tool definition's description to the per-result budget.

    The cut lands on a line boundary, never mid-sentence; a description with no
    usable early line boundary is cut at the budget itself. The input schema,
    which decides whether a call is valid, is never touched.
    """
    description = str(dumped.get("description") or "")
    if len(description) <= SEARCH_DESCRIPTION_CHARS:
        return
    cut = description.rfind("\n", 0, SEARCH_DESCRIPTION_CHARS)
    if cut < SEARCH_DESCRIPTION_CHARS // 2:
        cut = SEARCH_DESCRIPTION_CHARS
    dumped["description"] = description[:cut].rstrip() + "\n[description truncated]"


def _compact_entry(tool: Tool) -> tuple[dict[str, Any], int]:
    """FastMCP's dump of one tool, description cut, with its whole-answer byte cost.

    The cost is measured on UTF-8 BYTES, not characters: the budget bounds what
    crosses the wire, and a non-ASCII description costs more bytes than the
    ``len(json.dumps(...))`` character count reported.
    """
    dumped = tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
    _truncate_description(dumped)
    return dumped, len(json.dumps(dumped, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _budget_results(entries: Sequence[tuple[dict[str, Any], int]]) -> list[dict[str, Any]]:
    """Assemble a search answer under SEARCH_ANSWER_MAX_BYTES.

    The accounting is skip-and-continue, not break: a result that does not fit
    is skipped and the scan goes on, so one oversized early result cannot
    suppress every smaller one after it (breaking at the first overflow was
    what it did, and the smaller later candidates were the ones the caller
    still needed). The FIRST result is always kept, even when it alone
    overflows the budget - an empty answer is worse than a big one. Framing is
    counted too: every result after the first rides in the JSON array behind a
    comma, so it carries that ``len(",")`` byte of overhead.
    """
    results: list[dict[str, Any]] = []
    spent = 0
    for dumped, cost in entries:
        framing = len(",") if results else 0
        if results and spent + framing + cost > SEARCH_ANSWER_MAX_BYTES:
            continue
        spent += framing + cost
        results.append(dumped)
    return results


def _compact_search_results(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    """FastMCP's search-result shape, with each description cut to a budget.

    A tool's own description is its full contract, and under disclosure the
    search answer is the only place a caller reads it. But the answer is also
    the biggest thing the model has to hold per discovery: with the full text
    and four results it measured 10-18 KB for a one-line task, and the run that
    did that stalled 27 times across 29 steps. So the search payload carries a
    bounded excerpt - cut at a line boundary, never mid-sentence - and the
    input schema, which is the part that decides whether the call is valid, is
    never cut. The transform's cached serializer (see _cached_compaction) is
    the mounted one; this module-level form is the uncached reference version.
    """
    return _budget_results([_compact_entry(tool) for tool in tools])


def _closest_tools(name: str, tools: Sequence[Tool], limit: int = 3) -> list[Tool]:
    """The tools a typed name's words come closest to, ranked as search_tools ranks them.

    A caller that types ``denylists.updateDenylistEntry`` or "generate analytics
    plot" has described a task in this catalog's vocabulary without naming a
    tool. Ranking those words with the rule search_tools already uses turns a
    dead end into a correction the caller can act on.
    """
    if not tools:
        return []
    scorer = _word_scorer(_words(name), _name_words(tools), _vocabulary_words(tools))
    ordered = sorted(tools, key=lambda tool: -scorer(tool.name))
    return [tool for tool in ordered[:limit] if scorer(tool.name) > 0]


def _required_arguments(tool: Tool) -> str:
    """The arguments a tool cannot be called without, read from its own schema."""
    required = schema_required(tool.parameters)
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


def _flat_equal_matches(name: str, names: Sequence[str]) -> list[str]:
    """Catalog entries equal to the typed name once separators and case are gone."""
    flat = _WORD_SPLIT.sub("", name.casefold())
    return [candidate for candidate in names if _WORD_SPLIT.sub("", candidate.casefold()) == flat]


def _word_equal_matches(name: str, names: Sequence[str]) -> list[str]:
    """Catalog entries whose word set equals the typed name's word set."""
    words = _words(name)
    return [candidate for candidate in names if _words(candidate) == words]


def _match_tool_name(name: str, names: Sequence[str]) -> str:
    """The catalog entry a mistyped name unambiguously means, or ''.

    Two readings only, both exact. The same name with separators and case
    removed: ``get_denylist`` and ``getdenylist`` are getDenylist. The
    same words in another order: ``analytics/plot`` is plotAnalytics, because
    the caller used that tool's own words and nothing else. A near miss that is
    neither is left alone - the caller gets the diagnosis, not a guess.

    A reading is dropped when two catalog entries satisfy it, so an ambiguous
    name is never resolved.
    """
    if not name or name in names:
        return ""
    flat_matches = _flat_equal_matches(name, names)
    if len(flat_matches) == 1:
        return flat_matches[0]
    word_matches = _word_equal_matches(name, names)
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
    return result


def _passthrough_resolution(resolved: str, by_name: dict[str, Tool]) -> tuple[str | None, Tool | None] | None:
    """The answer when there is nothing to match: no name given, or no catalog.

    An empty catalog - no transform, or one that could not be read - leaves the
    name exactly as the caller sent it rather than refusing it.
    """
    if not resolved or not by_name:
        return resolved, by_name.get(resolved) if resolved else None
    return None


def _resolved_entry(resolved: str, by_name: dict[str, Tool]) -> tuple[str, Tool] | None:
    """The (name, entry) the supplied name refers to in the catalog, or None if it matches nothing.

    A name that is itself a tool is used as given; one that normalises to exactly
    one entry is corrected to it.
    """
    if resolved in by_name:
        return resolved, by_name[resolved]
    matched = _match_tool_name(resolved, list(by_name))
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
    "write" a fact rather than a name-shape heuristic.
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
        write. A name the map does not carry - the two local operations,
        plotAnalytics and dohLookup - reads.
        """
        self._call_tool_name = call_tool_name
        self._transform = transform
        self._endpoints = endpoints

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        message = getattr(context, "message", None)
        arguments = getattr(message, "arguments", None)
        if getattr(message, "name", "") != self._call_tool_name or not isinstance(arguments, dict):
            return await call_next(context)

        tools = await self._catalog(context)
        supplied = arguments.get("name")
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
        self._refuse_ambiguous_write(supplied)

        by_name = {tool.name: tool for tool in tools}
        passthrough = _passthrough_resolution(resolved, by_name)
        if passthrough is not None:
            return passthrough
        entry = _resolved_entry(resolved, by_name)
        if entry is None:
            raise ToolError(_unknown_tool_message(resolved, tools))
        return entry

    def _refuse_ambiguous_write(self, supplied: Any) -> None:
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
        if len(candidates) > 1 and is_write_operation(self._endpoint_method(candidates[0])):
            raise ToolError(_ambiguous_write_message(candidates))

    def _endpoint_method(self, name: str) -> str:
        """The HTTP method the named tool executes; names off the map read."""
        endpoint = (self._endpoints or {}).get(name)
        return endpoint[0] if endpoint else "GET"

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
        except Exception:
            # A shape check must never break the call: no catalog read, no
            # repair; the call proceeds with the name exactly as sent.
            logger.debug("could not read the tool catalog to shape a call_tool call", exc_info=True)
            return ()


def build_search_transform(mode: str) -> BM25SearchTransform | None:
    """Return the configured search transform for ``mode`` (``off`` -> None).

    Any other mode raises: ``config`` validates NEXTDNS_TOOL_SEARCH against
    its own accepted list, and the factory must not turn a mode added there
    into silent "no disclosure" - this raise is the seam where the two lists
    meet, so a mode this module has not implemented fails loudly at startup
    instead of quietly disabling discovery.
    """
    if mode == "bm25":
        return FallbackBM25SearchTransform(max_results=CATALOG_MAX_RESULTS)
    if mode == "off":
        return None
    raise ConfigurationError(f"Unknown tool-search mode {mode!r}; expected 'bm25' or 'off'.")
