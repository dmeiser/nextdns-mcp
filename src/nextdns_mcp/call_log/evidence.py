"""The identity facts a verifier matches on, lifted from the FULL result.

SPDX-License-Identifier: MIT

The lossy ``result`` column can cut anywhere, so the ``evidence`` column is
the fix: the id a write created, the (id, name) pairs a list answer carried,
and the component types of a Prefab UI document. Reads the full payload
*before* any bounding, bounded by construction.
"""

from typing import Any

# Dedicated bounded evidence (see ``_result_evidence``): how many id/name pairs
# one row may carry, and how long each field may be. An id or name over the cap
# is an anomaly; 50 pairs covers every list this server publishes a budget for.
EVIDENCE_MAX_ITEMS = 50
EVIDENCE_MAX_ID_CHARS = 120

# The walk over a Prefab UI view tree stops here; documents are iteratively
# traversed so a deeply nested view cannot exhaust the stack.
COMPONENT_MAX_DEPTH = 64


def _identity_entry(node: Any) -> tuple[str, dict[str, str]] | None:
    """(full id, bounded entry) when this object carries an id; None otherwise."""
    if not isinstance(node, dict):
        return None
    identifier = node.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    entry = {"id": identifier[:EVIDENCE_MAX_ID_CHARS]}
    if isinstance(node.get("name"), str):
        entry["name"] = node["name"][:EVIDENCE_MAX_ID_CHARS]
    return identifier, entry


def _walk_children(node: Any, out: list[dict[str, str]], seen: set[str]) -> None:
    """Descend one container level looking for id-carrying objects.

    The loop breaks once the evidence bound is reached: the recursive guard in
    ``_walk_identities`` would stop any further append, but without the break a
    container holding thousands of id-less siblings keeps the walk iterating
    (and descending into each) long after the bound was hit.
    """
    if isinstance(node, dict):
        children: Any = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return
    for value in children:
        if len(out) >= EVIDENCE_MAX_ITEMS:
            break
        _walk_identities(value, out, seen)


def _walk_identities(node: Any, out: list[dict[str, str]], seen: set[str]) -> None:
    """Collect (id, name) objects from a payload, bounded, without replacement."""
    if len(out) >= EVIDENCE_MAX_ITEMS:
        return
    entry = _identity_entry(node)
    if entry is not None and entry[0] not in seen:
        seen.add(entry[0])
        out.append(entry[1])
    _walk_children(node, out, seen)


def _walk_component_types(node: Any, types: set[str]) -> None:
    """Collect all component `type` values in a Prefab UI view tree.

    Iterative (no RecursionError on deeply nested documents) and restricted
    to UI child containers (``view`` / ``children``) so data rows and chart
    datasets are never traversed. Bounded by ``COMPONENT_MAX_DEPTH``.
    """
    stack: list[tuple[Any, int]] = [(node, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > COMPONENT_MAX_DEPTH:
            continue
        _walk_component_node(current, depth, types, stack)


def _walk_component_node(current: Any, depth: int, types: set[str], stack: list[tuple[Any, int]]) -> None:
    """One node of the component walk: lists fan out, dicts name and push children."""
    if isinstance(current, list):
        for item in current:
            stack.append((item, depth + 1))
        return
    if not isinstance(current, dict):
        return
    if isinstance(current.get("type"), str):
        types.add(current["type"])
    _push_component_children(current, depth, stack)


def _push_component_children(current: dict[Any, Any], depth: int, stack: list[tuple[Any, int]]) -> None:
    """UI child containers only: ``view`` and ``children``, never ``data`` / ``rows``."""
    for key in ("view", "children"):
        child = current.get(key)
        if isinstance(child, (dict, list)):
            stack.append((child, depth + 1))


def _created_id_of(data: Any) -> str:
    """The id a create answer carries, bounded; empty when it does not."""
    if isinstance(data, dict) and isinstance(data.get("id"), str):
        return data["id"][:EVIDENCE_MAX_ID_CHARS]
    return ""


def _prefab_evidence(structured: dict[str, Any]) -> dict[str, Any]:
    """Component facts lifted out of a Prefab UI document."""
    if "$prefab" not in structured:
        return {}
    types: set[str] = set()
    _walk_component_types(structured.get("view"), types)
    return {"components": sorted(types)} if types else {}


def _result_evidence(result: Any, write: bool) -> dict[str, Any]:
    """Identity facts lifted out of the FULL result, before any bounding.

    Bounded by construction, so a harness matching created-ids or id/name
    pairs through a cut ``result`` column keeps what the projection drops.
    """
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        return {}
    evidence = _prefab_evidence(structured)
    data = structured.get("data")
    if write:
        created = _created_id_of(data)
        if created:
            evidence["created_id"] = created
    ids: list[dict[str, str]] = []
    _walk_identities(data, ids, set())
    if ids:
        evidence["ids"] = ids
    return evidence
