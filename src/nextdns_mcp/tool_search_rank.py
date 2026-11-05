"""Name- and word-aware ranking machinery for the search transforms.

Split out of ``tool_search`` so the serving side (transform overrides, answer
budgeting, call-shape middleware) and the ranking side stay separately
readable. Everything here is a pure function of the catalog's tool names,
descriptions, and parameter schemas: query word folding, morphological
variants, catalog-wide shared-word discounting, and the name-overlap re-rank
that keeps a tool whose name is literally what the caller asked for ahead of
a longer-description BM25 match. The behaviors are pinned by the ranking
regression tests in ``tests/unit/test_tool_search.py``.

SPDX-License-Identifier: MIT
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp.tools.base import Tool

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


def _tool_properties(tool: Tool) -> dict[str, Any]:
    """The parameters a tool declares, as a name -> schema mapping (empty if none)."""
    return (tool.parameters or {}).get("properties") or {}
