"""One recursive bounder for JSON documents, with the caps supplied by the caller.

SPDX-License-Identifier: MIT

Two paths trim a JSON document down to a size a reader can carry: ``errors``
bounds an upstream document before inlining it into the tool result a model
reads, and ``call_log`` projects a tool result before serializing its row.
Both walk the same shape — cap a string, rebuild a dict from its values,
rebuild a list from its items, pass a scalar through — and differ only in the
numbers and the marks they leave behind. Kept apart, the two descents drifted
into two conventions for the same job (issue #614), so this module owns the
walk and each caller passes in the :class:`BoundSpec` it needs.
"""

from dataclasses import dataclass
from itertools import islice
from typing import Any


@dataclass(frozen=True)
class BoundSpec:
    """The caps and marks one caller applies while walking a JSON document.

    ``max_string_chars`` cuts every string, and ``string_marker`` is appended to
    a string that was cut. ``max_items`` keeps that many leading list items and
    appends ``{omitted_key: dropped_count}``; a caller that sets ``max_items``
    sets ``omitted_key`` with it, because a drop the reader cannot see is the
    failure the marker exists to prevent. ``max_keys`` keeps that many leading
    dict keys; a dict that loses keys to the cap is flagged as a cut, the same
    visible-drop rule the list marker follows. ``max_depth`` projects anything
    deeper than that level to ``None`` instead of recursing into it. A limit a
    caller does not want stays ``None``.

    ``bound_keys`` applies ``max_string_chars`` to mapping *keys* as well as
    values, which a caller that inlines a document it did not write needs: the
    document names its own fields, so a key is as much a size cost as a value
    (issue #600). Cutting is not injective, so a key the cap made equal to one
    already in the result is named ``<cut key> [duplicate N]`` rather than
    overwriting it and losing a field the document carried.
    """

    max_string_chars: int
    string_marker: str = ""
    max_items: int | None = None
    omitted_key: str | None = None
    max_keys: int | None = None
    max_depth: int | None = None
    bound_keys: bool = False


def bound_json(value: Any, spec: BoundSpec, depth: int = 0) -> tuple[Any, bool]:
    """Bound one JSON value: ``(bounded_value, content_was_dropped)``.

    The flag is the caller's truncation signal: True when a string was cut, a
    list dropped items, a dict dropped keys, or a subtree was projected away
    at the depth cap — every cut a caller flags. A depth cut flags too: the
    ``None`` left behind is indistinguishable from a null the document carried,
    so the caller that inlines a document it did not write must be told (issue
    #753). Containers are
    rebuilt rather than edited, so the document the caller still holds is
    untouched by the walk.

    Args:
        value: Any JSON-decoded value.
        spec: The caps and marks to apply at every depth.
        depth: The level of ``value`` in the document; the entry point is 0.
    """
    if _over_depth(spec, depth):
        return None, True
    if isinstance(value, str):
        return _bound_string(value, spec)
    if isinstance(value, dict):
        return _bound_dict(value, spec, depth)
    if isinstance(value, list):
        return _bound_list(value, spec, depth)
    return value, False


def _over_depth(spec: BoundSpec, depth: int) -> bool:
    """True past the caller's depth cap: the walk stops there rather than recursing."""
    return spec.max_depth is not None and depth > spec.max_depth


def _bound_string(value: str, spec: BoundSpec) -> tuple[str, bool]:
    """Cut one string at the cap, marking the cut when the caller has a marker."""
    if len(value) <= spec.max_string_chars:
        return value, False
    return value[: spec.max_string_chars] + spec.string_marker, True


def _bound_dict(value: dict[Any, Any], spec: BoundSpec, depth: int) -> tuple[dict[Any, Any], bool]:
    """Rebuild a dict with every key and value bounded; ``max_keys`` keeps the leading keys.

    ``islice`` consumes the items view lazily, so a huge mapping yields only the
    kept items to the walk instead of a materialized list of all of them.
    Keys the cap dropped flag the cut, so a caller can tell a shortened dict
    from a complete one.
    """
    items = value.items() if spec.max_keys is None else islice(value.items(), spec.max_keys)
    bounded: dict[Any, Any] = {}
    collisions: dict[str, int] = {}
    cuts: list[bool] = []
    for key, item in items:
        bounded_key, key_cut = bound_key(bounded, key, spec, collisions)
        bounded[bounded_key], value_cut = bound_json(item, spec, depth + 1)
        cuts.extend((key_cut, value_cut))
    if len(bounded) < len(value):
        cuts.append(True)
    return bounded, any(cuts)


def bound_key(bounded: dict[Any, Any], key: Any, spec: BoundSpec, collisions: dict[str, int]) -> tuple[Any, bool]:
    """Bound one key of the mapping under construction (``bounded``).

    A caller that walks a mapping itself — because it exempts some of its own
    keys from the value cap — still gets the key rule from here, so the cap and
    the collision naming cannot drift between the two paths. A key is capped by
    ``max_string_chars`` only when the spec sets ``bound_keys``; a non-string
    key cannot come from ``json``, but it must not fail the walk either, so it
    passes through untouched.

    ``collisions`` is the per-mapping counter :func:`_distinct_key` amortizes
    its suffix search over; the caller creates one fresh dict per mapping it
    walks, alongside ``bounded``.
    """
    if not spec.bound_keys or not isinstance(key, str):
        return key, False
    cut_key, truncated = _bound_string(key, spec)
    if cut_key in bounded:
        cut_key = _distinct_key(bounded, cut_key, collisions)
    return cut_key, truncated


def _distinct_key(bounded: dict[Any, Any], key: str, collisions: dict[str, int]) -> str:
    """Name a cut key that now equals a key the result already holds.

    ``collisions`` remembers the next suffix to try for each base key and never
    moves it backwards, so a candidate is formed at most once per walk: the
    search restarts from the last candidate rather than from ``[duplicate 2]``
    on every collision, and the suffix work across one mapping grows at most
    linearly with its keys instead of quadratically (issue #753). Candidates a
    document already carries under a literal ``[duplicate N]`` name are still
    skipped, just never re-probed.
    """
    numbered = collisions.get(key, 2)
    while f"{key} [duplicate {numbered}]" in bounded:
        numbered += 1
    collisions[key] = numbered + 1
    return f"{key} [duplicate {numbered}]"


def _bound_list(value: list[Any], spec: BoundSpec, depth: int) -> tuple[list[Any], bool]:
    """Rebuild a list with every item bounded; ``max_items`` keeps the leading ones.

    A list that fits the cap is returned with nothing added: the omission
    marker tells a reader that content is missing, so marking a complete list
    would make every small answer look truncated.
    """
    kept, dropped = _cap_items(value, spec.max_items)
    bounded: list[Any] = []
    truncated = False
    for item in kept:
        bounded_item, cut = bound_json(item, spec, depth + 1)
        bounded.append(bounded_item)
        truncated = truncated or cut
    if dropped:
        bounded.append({spec.omitted_key: dropped})
        truncated = True
    return bounded, truncated


def _cap_items(value: list[Any], max_items: int | None) -> tuple[list[Any], int]:
    """The items the list cap keeps, and how many it dropped."""
    if max_items is None or len(value) <= max_items:
        return value, 0
    return value[:max_items], len(value) - max_items
