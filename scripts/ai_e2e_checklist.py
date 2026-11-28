"""The step checklist for the NextDNS MCP E2E harness: model, catalog, selection.

SPDX-License-Identifier: MIT

The ``Step`` model and the expectation builders that fill it, the catalog data
the checklist is generated from (settings operations, list specs, analytics
tools, per-tool endpoints), the call-log readers that turn the actor's own MCP
calls into evidence for a step, and targeted-run selection over the generated
checklist. Building the catalog and judging a step's REST verdict are delegated
to ``ai_e2e_steps`` and ``ai_e2e_verify``. Split out of ``ai_e2e_harness.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass
class Step:
    """One checklist operation: an actor prompt, an expected server call, and a
    REST verifier."""

    id: str
    section: str
    tool: str  # the published tool this step is about (a report label; the actor finds it itself)
    description: str
    prompt: str  # instruction to the actor ("{profile_id}" rendered at run time)
    verify: str  # verifier key in _verify_step
    vdata: dict[str, Any] = field(default_factory=dict)
    # The call this step must produce, matched against the server's own call
    # log (nextdns_mcp.call_log): either {"endpoints": ["METHOD /path/template"]}
    # for a proxied operation, or {"tool": name} for one the server performs
    # itself - plus "args" and "result" (substrings that must appear) and
    # "data" (the call answered with a payload). "{profile_id}" inside a
    # substring is rendered at run time.
    expect: dict[str, Any] = field(default_factory=dict)


def _expect(
    endpoint: str,
    args: tuple[str, ...] = (),
    result: tuple[str, ...] = (),
    data: bool = True,
    profile: str = "",
) -> dict[str, Any]:
    """The endpoint call this step must produce (see Step.expect).

    Every published tool is bound to one endpoint, so naming the endpoint names
    the API call the step is about no matter which tool the actor found to make
    it. ``{profile_id}`` stays literal: the log records the spec's own path
    template, so a step pointed at a different profile (the plot profile, the
    log-download profile) still matches its endpoint. ``profile`` scopes which
    profile the call's recorded ``profile_id`` must name (see ANY_PROFILE and
    PLOT_PROFILE); unset means the test profile under test.
    """
    expect = {"endpoints": [endpoint], "args": list(args), "result": list(result), "data": data}
    if profile:
        expect["profile"] = profile
    return expect


def _expect_local(
    tool: str,
    args: tuple[str, ...] = (),
    result: tuple[str, ...] = (),
    data: bool = True,
    profile: str = "",
) -> dict[str, Any]:
    """A call to an operation the server performs itself: no endpoint, its own record.

    ``plotAnalytics``, ``dohLookup``, and ``overviewDashboard`` answer with what no
    endpoint returns - a Prefab chart or dashboard, a DNS answer - so they are recorded by tool name, and the
    payload they answered with is the proof the operation ran.
    """
    expect = {"tool": tool, "args": list(args), "result": list(result), "data": data}
    if profile:
        expect["profile"] = profile
    return expect


# The profile an expected call must name (Step.expect["profile"]):
# ANY_PROFILE for operations that take no profile (create, list), PLOT_PROFILE
# for the read-only data profile the plots and log-download steps run against.
# Anything else must name the test profile itself. Without this scope, an
# actor working on some other readable profile while the harness verified the
# intended one by REST could pass a step on the wrong profile's call.
ANY_PROFILE = "*"
PLOT_PROFILE = "{plot_profile}"


# ---------------------------------------------------------------------------
# The checklist's API surface: the tools and endpoints each family of steps
# drives, taken from the shipped OpenAPI document
# (src/nextdns_mcp/nextdns-openapi.yaml).
# ---------------------------------------------------------------------------

# category -> the (read, write) tools for that settings endpoint. The seven
# settings categories are seven separate endpoints, each with its own getter and
# writer; "general" is /settings itself, which is where the web3 toggle lives.
_SETTINGS_OPS: dict[str, tuple[str, str]] = {
    "general": ("getSettings", "updateSettings"),
    "logs": ("getLogsSettings", "updateLogsSettings"),
    "blockpage": ("getBlockPageSettings", "updateBlockPageSettings"),
    "performance": ("getPerformanceSettings", "updatePerformanceSettings"),
    "security": ("getSecuritySettings", "updateSecuritySettings"),
    "privacy": ("getPrivacySettings", "updatePrivacySettings"),
    "parental": ("getParentalControlSettings", "updateParentalControlSettings"),
}

# list type -> the tool that performs each operation on that collection. A
# missing key means the checklist does not perform that operation there (only
# the free-form domain lists accept a synthetic "added" entry). The endpoint
# each tool executes is resolved from the document, so the entry parameter's
# spelling - `id` for parental control, `entry_id` elsewhere - is never
# re-typed here.
_LIST_TOOLS: dict[str, dict[str, str]] = {
    "allowlist": {
        "get": "getAllowlist",
        "replace": "replaceAllowlist",
        "add": "addToAllowlist",
        "update": "updateAllowlistEntry",
        "remove": "removeFromAllowlist",
    },
    "denylist": {
        "get": "getDenylist",
        "replace": "replaceDenylist",
        "add": "addToDenylist",
        "update": "updateDenylistEntry",
        "remove": "removeFromDenylist",
    },
    "privacy_blocklists": {
        "get": "getPrivacyBlocklists",
        "replace": "replacePrivacyBlocklists",
        "remove": "removePrivacyBlocklist",
    },
    "privacy_natives": {
        "get": "getPrivacyNatives",
        "replace": "replacePrivacyNatives",
        "remove": "removePrivacyNative",
    },
    "security_tlds": {
        "get": "getSecurityTLDs",
        "replace": "replaceSecurityTLDs",
        "remove": "removeSecurityTLD",
    },
    "parental_categories": {
        "get": "getParentalControlCategories",
        "replace": "replaceParentalControlCategories",
        "update": "updateParentalControlCategoryEntry",
        "remove": "removeFromParentalControlCategories",
    },
    "parental_services": {
        "get": "getParentalControlServices",
        "replace": "replaceParentalControlServices",
        "update": "updateParentalControlServiceEntry",
        "remove": "removeFromParentalControlServices",
    },
}

# metric -> the tool answering that aggregate. The series tool is the same name
# with a "Series" suffix (there is no series endpoint for domains, and the
# checklist does not ask for one).
_ANALYTICS_TOOLS: dict[str, str] = {
    "status": "getAnalyticsStatus",
    "domains": "getAnalyticsDomains",
    "queryTypes": "getAnalyticsQueryTypes",
    "reasons": "getAnalyticsReasons",
    "ips": "getAnalyticsIPs",
    "dnssec": "getAnalyticsDNSSEC",
    "encryption": "getAnalyticsEncryption",
    "ipVersions": "getAnalyticsIPVersions",
    "protocols": "getAnalyticsProtocols",
    "devices": "getAnalyticsDevices",
    "destinations": "getAnalyticsDestinations",
}


@lru_cache(maxsize=1)
def _spec_endpoints() -> dict[str, str]:
    """operationId -> ``"METHOD /path template"``, read from the shipped document.

    This is how a step's expectation is written: the step names the tool that
    performs it, and this resolves the endpoint the server will record for that
    call. An operation the document renames or drops fails at build time with a
    KeyError naming it, rather than quietly matching no call in a live run.
    """
    from nextdns_mcp.spec_tools import spec_routes

    return {r.operation_id: f"{r.method} {r.path}" for r in spec_routes() if r.operation_id}


def _endpoint(tool: str) -> str:
    """The endpoint a published tool executes, as the server's call log records it."""
    endpoints = _spec_endpoints()
    if tool not in endpoints:
        raise KeyError(f"the checklist names {tool!r}, which is not an operation of the shipped OpenAPI document")
    return endpoints[tool]


# Written by the MCP server (nextdns_mcp.call_log) into the run's workdir; the
# harness reads it back per step.
CALL_LOG_NAME = "mcp_calls.jsonl"


def _read_new_calls(path: Path, offset: int) -> list[dict[str, Any]]:
    """Call records the server appended since ``offset`` bytes into the log.

    The server's own row reader (nextdns_mcp.call_log.read_call_log_rows):
    one definition of what a valid row is, shared with the server's
    get_call_log, instead of a second parser free to drift from it.
    """
    if not path.exists():
        return []
    from nextdns_mcp.call_log import read_call_log_rows

    return read_call_log_rows(str(path), offset)


def _dict_answered(payload: dict[str, Any]) -> bool:
    if payload.get("success") is True:
        return True
    if "data" in payload:
        return payload["data"] is not None
    # Anything else the tool answered with - a chart's content blocks, a
    # DNS answer, a status document - is the payload for that tool.
    return bool(payload)


def _answered(result_text: str) -> bool:
    """Did the call answer with a payload?

    Effectiveness, not emptiness: a fresh test profile really does have an empty
    allowlist, so ``{"data": []}`` is a correct answer to "read the allowlist",
    and the write tools answer ``{"success": true}`` rather than an object. Each
    published tool answers with its own document - a chart's content blocks, a
    DNS answer, a settings object - so any non-empty payload counts; the ok flag
    on the record has already excluded the ones that answered with an error.
    """
    if not result_text:
        return False
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return bool(result_text.strip())
    if isinstance(payload, dict):
        return _dict_answered(payload)
    return bool(payload)


def _add_identity(out: dict[str, list[str]], name: str, identifier: str) -> None:
    if identifier not in out.setdefault(name, []):
        out[name].append(identifier)


def _is_identity_pair(identifier: Any, name: Any) -> bool:
    # Short values (`type: "A"`) are skipped: they are labels, not identifiers,
    # and matching on them would tie every entry in the list to the same word.
    if not isinstance(identifier, str) or not isinstance(name, str):
        return False
    return len(name) >= 6 and name != identifier


def _evidence_pair(entry: Any) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    identifier, name = entry.get("id"), entry.get("name")
    if not _is_identity_pair(identifier, name):
        return None
    return str(identifier), str(name)


def _add_evidence_ids(record: dict[str, Any], out: dict[str, list[str]]) -> None:
    # Evidence pairs first: they survive result truncation (the projection
    # keeps five list items; evidence keeps fifty identities). The result
    # walk stays as the fallback and as coverage for identifiers the
    # evidence lift does not name (non-``name`` label fields).
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        return
    for entry in evidence.get("ids", []):
        pair = _evidence_pair(entry)
        if pair is None:
            continue
        _add_identity(out, pair[1], pair[0])


def _add_payload_ids(record: dict[str, Any], out: dict[str, list[str]]) -> None:
    try:
        payload = json.loads(str(record.get("result") or ""))
    except (ValueError, TypeError):
        return
    for identifier, names in _walk_identifiers(payload):
        for name in names:
            _add_identity(out, name, identifier)


def _identifiers_from_answers(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each name a tool answer carried to the id that answer gave it.

    Built only from this attempt's successful answers, so the equivalence is the
    server's own statement, not the harness guessing which record a call meant.
    """
    out: dict[str, list[str]] = {}
    for record in records:
        if not record.get("ok"):
            continue
        _add_evidence_ids(record, out)
        _add_payload_ids(record, out)
    return out


def _is_label_value(key: str, value: Any, identifier: str) -> bool:
    if key == "id" or not isinstance(value, str):
        return False
    return len(value) >= 6 and value != identifier


def _names_for_id(node: dict[str, Any], identifier: str) -> list[str]:
    return [value for key, value in node.items() if _is_label_value(key, value, identifier)]


def _walk_dict(node: dict[str, Any]) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    identifier = node.get("id")
    if isinstance(identifier, str) and len(identifier) >= 4:
        names = _names_for_id(node, identifier)
        if names:
            found.append((identifier, names))
    for value in node.values():
        found.extend(_walk_identifiers(value))
    return found


def _walk_list(node: list[Any]) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    for value in node:
        found.extend(_walk_identifiers(value))
    return found


def _walk_identifiers(node: Any) -> list[tuple[str, list[str]]]:
    """(id, names) for every object in a payload that carries an `id` string."""
    if isinstance(node, dict):
        return _walk_dict(node)
    if isinstance(node, list):
        return _walk_list(node)
    return []


def _profile_for_expectation(expect: dict[str, Any], pid: str, alt_pid: str) -> str:
    profile_token = str(expect.get("profile", ""))
    if profile_token == ANY_PROFILE:
        return ""
    if profile_token == PLOT_PROFILE:
        return alt_pid or pid
    return pid


def _wanted_calls(expect: dict[str, Any], pid: str, alt_pid: str) -> dict[str, Any]:
    # Endpoint expectations are matched against the spec's path templates, so
    # `{profile_id}` stays literal and a step whose call targets another
    # profile (plots, log download) still matches.
    endpoints = [str(item) for item in expect.get("endpoints", [])]
    tool = str(expect.get("tool", ""))
    return {
        "tool": tool,
        "endpoints": endpoints,
        "label": " or ".join(endpoints) if endpoints else tool,
        "args": [s.replace("{profile_id}", pid) for s in expect.get("args", [])],
        "result": [s.replace("{profile_id}", pid) for s in expect.get("result", [])],
        "profile": _profile_for_expectation(expect, pid, alt_pid),
        "data": bool(expect.get("data", True)),
    }


def _matches(record: dict[str, Any], wanted: dict[str, Any]) -> bool:
    if wanted["endpoints"]:
        return str(record.get("endpoint", "")) in wanted["endpoints"]
    return record.get("tool") == wanted["tool"]


def _no_match_reason(records: list[dict[str, Any]], wanted: dict[str, Any]) -> str:
    # Why the closest candidate was rejected, so a failure names the condition
    # it failed instead of guessing from the tool name alone.
    label = wanted["label"]
    if any(_matches(r, wanted) and not r.get("ok") for r in records):
        return f"{label} reached the server but every such call failed"
    return f"server recorded no {label} call in this attempt"


def _candidates(records: list[dict[str, Any]], wanted: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """(index, record) for every successful record that could be this step's call."""
    return [(index, r) for index, r in enumerate(records) if _matches(r, wanted) and r.get("ok")]


def _profile_rejection(record: dict[str, Any], wanted: dict[str, Any]) -> str:
    want_profile = wanted["profile"]
    if not want_profile:
        return ""
    recorded = str(record.get("profile_id", ""))
    if recorded == want_profile:
        return ""
    return f"{wanted['label']} ran against profile {recorded or '(none recorded)'}, not the {want_profile} under test"


def _any_id_in(args_text: str, identifiers: Any) -> bool:
    return any(i in args_text for i in identifiers)


def _args_not_called(args_text: str, want_args: list[str], ids_by_name: dict[str, list[str]]) -> list[str]:
    return [s for s in want_args if s not in args_text and not _any_id_in(args_text, ids_by_name.get(s, ()))]


def _args_rejection(record: dict[str, Any], wanted: dict[str, Any], ids_by_name: dict[str, list[str]]) -> str:
    missing_args = _args_not_called(str(record.get("args", "")), wanted["args"], ids_by_name)
    if missing_args:
        return f"{wanted['label']} was called without {missing_args}"
    return ""


def _missing_answer(record: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    result_text = str(record.get("result", ""))
    evidence_text = json.dumps(record.get("evidence", {}))
    return [s for s in wanted["result"] if s not in result_text and s not in evidence_text]


def _answer_rejection(record: dict[str, Any], wanted: dict[str, Any]) -> str:
    missing_answer = _missing_answer(record, wanted)
    if missing_answer:
        return f"{wanted['label']} answered without {missing_answer}"
    if wanted["data"] and not _answered(str(record.get("result", ""))):
        return f"{wanted['label']} succeeded but returned no payload"
    return ""


def _record_rejection(record: dict[str, Any], wanted: dict[str, Any], ids_by_name: dict[str, list[str]]) -> str:
    """'' when this record satisfies every transcript condition; else why it does not."""
    return (
        _profile_rejection(record, wanted)
        or _args_rejection(record, wanted, ids_by_name)
        or _answer_rejection(record, wanted)
    )


def _later_successful_write(records: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    # Only a write that SUCCEEDED can undo the step. A failed duplicate
    # (observed: addRewrite of an existing record, rejected upstream)
    # changed nothing, so it must not fail the step.
    later = [r for r in records[index + 1 :] if r.get("write") and r.get("ok")]
    return later[0] if later else None


def _expected_call_ok(
    records: list[dict[str, Any]],
    expect: dict[str, Any],
    pid: str,
    alt_pid: str = "",
) -> tuple[bool, str]:
    """The two transcript conditions of the pass rule.

    1. the server recorded a SUCCESSFUL call matching this step's expected call,
       and the call answered with a payload — the API cannot show that a read
       happened. The expectation names the API endpoint the call executed
       (``POST /profiles/{profile_id}/denylist``), which the server knows
       because every generated tool is bound to one; the three composite operations
       the server performs itself are named by their tool instead (see
       ``_expect_local``). The call's recorded ``profile_id`` must name the
       profile the step targets: matching the endpoint template alone would
       score a call against ANY readable profile as this step's answer, while
       REST verification of the intended profile made the step pass around it.
    2. no state-changing call came AFTER it in the same attempt. Confirming
       reads afterwards are normal and allowed; undoing the step's own write is
       not.

    ``alt_pid`` is the profile a plot/download step runs against (the
    ``PLOT_PROFILE`` token); unset, those steps fall back to the test profile.
    Returns (ok, reason_when_not).
    """
    if not expect:
        return True, ""
    wanted = _wanted_calls(expect, pid, alt_pid)
    # An identifier the server handed back in this same attempt names the thing
    # it names. The checklist asks for the record called
    # `ai-e2e-a-…example.com`; the server's own list answer is what tells a
    # caller that its id is `c41a6xwk`, and deleting by that id is the workflow
    # the server documents and hints at. Both spellings identify the record.
    ids_by_name = _identifiers_from_answers(records)
    why = _no_match_reason(records, wanted)
    for index, record in reversed(_candidates(records, wanted)):
        rejection = _record_rejection(record, wanted, ids_by_name)
        if rejection:
            why = rejection
            continue
        undo = _later_successful_write(records, index)
        if undo:
            return False, f"{wanted['label']} succeeded but was followed by the write {_call_label(undo)}"
        return True, ""

    return False, why


def _call_label(record: dict[str, Any]) -> str:
    """How the server's own record of a call is named.

    The endpoint says what happened upstream; a call with no endpoint is one of
    the three composite operations the server performs itself, so its tool name says it.
    """
    endpoint = str(record.get("endpoint") or "")
    if endpoint:
        return endpoint
    return str(record.get("tool") or "?")


def _server_calls_text(records: list[dict[str, Any]]) -> str:
    """One-line rendering of the server's own record, for the live feedback."""
    if not records:
        return "(no tool call reached the server)"
    return "; ".join(
        _call_label(r) + ("" if r.get("ok") else f" ERR {str(r.get('error'))[:60]}") for r in records[:6]
    ) + (f"; ... {len(records) - 6} more" if len(records) > 6 else "")


def _step_prompt(task: str) -> str:
    """The actor's whole message for one step: the task, nothing else.

    No role framing, no rules, no description of the agent CLI or the MCP
    server — the harness asks for an outcome and verifies it over REST. How
    the server is to be used is the server's own instructions' job
    (``nextdns_mcp.usage.server_instructions``).
    """
    return f"Do the following in NextDNS:\n\n{task}"


# The steps that own the test profile's lifecycle. A range that leaves one out
# means the harness performs that part of the lifecycle itself (the actor is
# never asked to do it, and the run still starts and ends clean).
PROFILE_CREATE_STEP = "profiles.create"
PROFILE_DELETE_STEP = "profiles.delete"


class StepRangeError(ValueError):
    """A --steps/--start-step/--end-step value that does not identify a step."""


def _step_position(steps: list[Step], value: str, flag: str) -> int:
    """Resolve one selection bound to a 0-based index: a step id or a 1-based position."""
    raw = value.strip()
    if raw.isdigit():
        pos = int(raw)
        if 1 <= pos <= len(steps):
            return pos - 1
        raise StepRangeError(f"{flag}={value!r} is not a position in 1..{len(steps)}")
    for i, step in enumerate(steps):
        if step.id == raw:
            return i
    raise StepRangeError(f"{flag}={value!r} is not a step id or a 1-based position")


def _entry_bounds(steps: list[Step], entry: str, flag: str) -> tuple[int, int]:
    """One selection entry as (lo, hi) 0-based indices, inclusive.

    An entry is a step id, a 1-based position, or ``A-B`` — both sides resolved
    the same way. Step ids never contain ``-``, so the separator is unambiguous.
    """
    lo_raw, sep, hi_raw = entry.partition("-")
    if not sep:
        pos = _step_position(steps, entry, flag)
        return pos, pos
    return _step_position(steps, lo_raw, flag), _step_position(steps, hi_raw, flag)


def _chosen_positions(steps: list[Step], entries: list[str]) -> set[int]:
    """The 1-based positions the selection entries cover."""
    chosen: set[int] = set()
    for entry in entries:
        lo, hi = _entry_bounds(steps, entry, "--steps")
        if hi < lo:
            raise StepRangeError(
                f"--steps {entry!r}: {steps[hi].id!r} comes before {steps[lo].id!r} — a range is written low to high"
            )
        chosen.update(range(lo + 1, hi + 2))
    return chosen


def select_steps(steps: list[Step], selection: str = "") -> list[tuple[int, Step]]:
    """The selected steps, as (1-based position, step) pairs in checklist order.

    ``selection`` is a comma-separated list of step ids (``lists.allowlist.add``),
    1-based positions (``21``) and inclusive ranges (``20-21``,
    ``profiles.get-profiles.update``); overlapping entries collapse and the
    result is always de-duplicated and in checklist order. Empty means all
    steps. The positions are the ones ``--print-steps`` prints, so a targeted
    run's progress lines keep a full run's numbering and two reports can be
    compared step by step.
    """
    entries = [e.strip() for e in selection.split(",") if e.strip()]
    if not entries:
        return list(enumerate(steps, 1))
    chosen = _chosen_positions(steps, entries)
    return [(i, steps[i - 1]) for i in sorted(chosen)]


def needs_harness_setup(selected: list[tuple[int, Step]]) -> bool:
    """True unless the range itself asks the actor to create the test profile."""
    return not any(step.id == PROFILE_CREATE_STEP for _, step in selected)


def needs_harness_teardown(selected: list[tuple[int, Step]]) -> bool:
    """True unless the range itself asks the actor to delete the test profile."""
    return not any(step.id == PROFILE_DELETE_STEP for _, step in selected)


def render_step_catalog(steps: list[Step]) -> str:
    """The step catalog a --steps range selects from (stdout)."""
    id_width = max((len(s.id) for s in steps), default=0)
    header = (
        f"Step catalog ({len(steps)} steps) — --steps takes a comma-separated list of "
        "these ids or numbers, and A-B selects an inclusive range "
        "(e.g. --steps 20-21,lists.privacy_natives.replace-36,profiles.delete). "
        f"A selection that omits {PROFILE_CREATE_STEP} or {PROFILE_DELETE_STEP} makes "
        "the harness provision and/or delete the test profile itself."
    )
    lines = [header, ""]
    for i, s in enumerate(steps, 1):
        lines.append(f"  {i:>3}  {s.id:<{id_width}}  [{s.section}] {s.description}")
        lines.append(f"       {'':<{id_width}}  expects: {_expect_label(s)}")
    return "\n".join(lines) + "\n"


def _expect_bits(exp: dict[str, Any]) -> list[str]:
    bits = [f"args~{exp['args']}"] if exp.get("args") else []
    bits += [f"result~{exp['result']}"] if exp.get("result") else []
    bits += [f"profile={exp['profile']}"] if exp.get("profile") else []
    return bits


def _expect_label(step: Step) -> str:
    """The call a step must produce, as the server's call log will see it."""
    exp = step.expect
    if not exp:
        return "(no call expectation)"
    label = " or ".join(str(e) for e in exp.get("endpoints", [])) or str(exp.get("tool", ""))
    return f"{label} {' '.join(_expect_bits(exp))}".strip()
