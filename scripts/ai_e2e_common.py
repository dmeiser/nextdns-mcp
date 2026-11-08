"""Shared, pure (network-free) helpers for the NextDNS MCP E2E harness.

SPDX-License-Identifier: MIT

This module holds the deterministic building blocks used by the E2E harness
(``ai_e2e_harness.py``) and its unit tests:

* the result model (``CheckResult`` / ``Report``) and result classification
  (``check_from_call`` and the ``_is_*`` / ``_*_reason`` detectors);
* the constant tables describing the server surface (the 7 settings
  categories, the 7 list types, the analytics/plot metrics, the run sections);
* the REST read-back helpers the *measuring* half uses to independently verify
  server state via the raw NextDNS REST API (never via the MCP tool response).

Nothing in this module opens a network connection, builds the MCP server, or
starts a subprocess. The only package it imports from the server is
``nextdns_mcp.tools.metrics``, the typed literal the plot tool validates its
metric against — the one table here that would otherwise be a copy of something
the server already owns. It is importable both as a sibling of
``ai_e2e_harness.py`` (the harness inserts ``scripts/`` onto ``sys.path``) and
directly by file path from the unit tests.
"""

from __future__ import annotations

import os
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from nextdns_mcp.tools.metrics import PlotMetric

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_API_BASE = "https://api.nextdns.io"


def api_base() -> str:
    """The NextDNS API base, read when it is used rather than when it is imported.

    The harness loads its ``.env`` (or ``--env-file``) *after* importing this
    module, so a snapshot taken here at import time would keep ``NEXTDNS_API_BASE``
    out of both the MCP server's environment and the measuring client — the two
    halves would then talk to different hosts and every contract check would come
    back looking like a server fault rather than an env fault.

    Raises:
        ValueError: If the configured base is not ``https://``. This function is
            the choke point every path that sends ``X-Api-Key`` passes through
            (the profile-protection probe, the measuring client, the MCP server's
            env default), so refusing here keeps the key off a plaintext wire:
            an ``http`` base fails before any request is constructed.
    """
    base = os.environ.get("NEXTDNS_API_BASE", DEFAULT_API_BASE).rstrip("/")
    if not base.lower().startswith("https://"):
        raise ValueError(f"NEXTDNS_API_BASE must use https:// — refusing to send the API key to {base!r}")
    return base


# Checklist coverage, grouped into run sections. A section maps 1:1 onto an
# entry of the validation checklist.
SECTIONS: dict[str, str] = {
    "profiles": "profile create/list/get/update/delete",
    "settings": "7 settings categories (read + update + REST verify)",
    "lists": "7 list types (get/replace/add/update/remove + REST verify)",
    "rewrites": "3 rewrite record types (add/delete + REST verify)",
    "analytics": "aggregate + series for every analytics metric",
    "doh": "dohLookup DNS resolution via the test profile",
    "logs": "get / download / clear query logs",
    "plots": "9 plot-analytics metrics (interactive chart)",
}

# Settings categories -> REST path, "set" payload, expected read-back, "restore"
# payload, and expected read-back after restore. The read-back assertion is
# tolerant: it passes if every listed field is present (nested or top-level)
# with the expected value.
SETTINGS_CATEGORIES: dict[str, dict[str, Any]] = {
    "general": {
        "path": "/settings",
        "set": {"web3": True},
        "assert": {"web3": True},
        "restore": {"web3": False},
        "assert_restore": {"web3": False},
    },
    "privacy": {
        "path": "/privacy",
        "set": {"disguisedTrackers": True, "allowAffiliate": False},
        "assert": {"disguisedTrackers": True, "allowAffiliate": False},
        "restore": {"disguisedTrackers": False},
        "assert_restore": {"disguisedTrackers": False},
    },
    "security": {
        "path": "/security",
        "set": {"threatIntelligenceFeeds": True, "googleSafeBrowsing": True},
        "assert": {"threatIntelligenceFeeds": True, "googleSafeBrowsing": True},
        "restore": {"threatIntelligenceFeeds": False, "googleSafeBrowsing": False},
        "assert_restore": {"threatIntelligenceFeeds": False, "googleSafeBrowsing": False},
    },
    "parental": {
        "path": "/parentalControl",
        "set": {"safeSearch": True, "youtubeRestrictedMode": True},
        "assert": {"safeSearch": True, "youtubeRestrictedMode": True},
        "restore": {"safeSearch": False, "youtubeRestrictedMode": False},
        "assert_restore": {"safeSearch": False, "youtubeRestrictedMode": False},
    },
    "performance": {
        "path": "/settings/performance",
        "set": {"ecs": True, "cacheBoost": True},
        "assert": {"ecs": True, "cacheBoost": True},
        "restore": {"ecs": False, "cacheBoost": False},
        "assert_restore": {"ecs": False, "cacheBoost": False},
    },
    "logs": {
        "path": "/settings/logs",
        "set": {"enabled": True, "retention": 604800},
        "assert": {"enabled": True},
        "restore": {"enabled": False},
        "assert_restore": {"enabled": False},
    },
    "blockpage": {
        "path": "/settings/blockPage",
        "set": {"enabled": False},
        "assert": {"enabled": False},
        "restore": {"enabled": True},  # restore the default (block page on)
        "assert_restore": {"enabled": True},
    },
}

# Analytics metrics (mirrors queryAnalytics' literal).
AGGREGATE_METRICS = [
    "status",
    "domains",
    "queryTypes",
    "reasons",
    "ips",
    "dnssec",
    "encryption",
    "ipVersions",
    "protocols",
    "devices",
    "destinations",
]
SERIES_METRICS = [m for m in AGGREGATE_METRICS if m != "domains"]  # no series for domains
# The nine chartable metrics, read from the server's own typed literal — the very
# set ``plotAnalytics`` validates its argument against — so the checklist cannot
# drift from the surface it exercises. ``get_args`` keeps the literal's declaration
# order, which is what pins the step numbering a targeted ``--steps`` selection
# refers to; iterating the frozenset beside it would reshuffle the checklist
# between runs.
PLOT_METRICS: list[str] = list(get_args(PlotMetric))

# List types: REST path segment + per-entry update support + optional post-run
# "restore" entry (so a fresh profile is left in a sane state).
_LIST_SPECS: list[dict[str, Any]] = [
    {"name": "allowlist", "path": "/allowlist", "updatable": True},
    {"name": "denylist", "path": "/denylist", "updatable": True},
    {"name": "privacy_blocklists", "path": "/privacy/blocklists", "updatable": False, "restore": "nextdns-recommended"},
    {"name": "privacy_natives", "path": "/privacy/natives", "updatable": False},
    {"name": "security_tlds", "path": "/security/tlds", "updatable": False},
    {"name": "parental_categories", "path": "/parentalControl/categories", "updatable": True},
    {"name": "parental_services", "path": "/parentalControl/services", "updatable": True},
]


# The entry each list step leaves behind, in one table. The prompt the actor is
# given and the REST read-back that judges it name the same entry because both read
# this; the two copies that used to exist (the harness's ``TaskTargets.list_entries``
# and the ``_list_values`` switch below) drifted the moment one was edited.
# ``{tag}`` marks the free-form domain lists, whose entries are invented per run;
# the rest are closed enums the API only accepts by their own ids.
LIST_ENTRIES: dict[str, str] = {
    "allowlist": "ai-e2e-allow-{tag}.example.com",
    "denylist": "ai-e2e-deny-{tag}.example.com",
    "privacy_blocklists": "nextdns-recommended",
    "privacy_natives": "alexa",
    "security_tlds": "zip",
    "parental_categories": "gambling",
    "parental_services": "tiktok",
}


def _random_tag(n: int = 6) -> str:
    """Return a short random alphanumeric tag for unique resource names."""
    return "e2eh" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# One run suffix per process: two resolutions of the name inside one run must
# agree, while two runs on the same day must not.
_RUN_SUFFIX = f"{random.randrange(0x10000):04x}"


def _resolve_profile_name(template: str | None = None) -> str:
    """Resolve a profile-name template into a concrete name.

    Supports the ``{date}`` placeholder (``YYYYMMDD``), ``{timestamp}``
    (``YYYYMMDDHHMMSS``) and ``{run}`` (a per-process random suffix). The
    default carries ``{run}`` on purpose: the harness may only delete what
    this run proved it created, and a name unique to the run is what turns
    "found by this exact name" into such proof. An operator who pins the
    name via ``NEXTDNS_E2E_PROFILE_NAME`` reclaims the name - and with it
    the responsibility for what shares it.
    """
    if template is None:
        template = os.environ.get("NEXTDNS_E2E_PROFILE_NAME", "TEST_PROFILE_{date}_{run}")
    return template.format(
        date=time.strftime("%Y%m%d"),
        timestamp=time.strftime("%Y%m%d%H%M%S"),
        run=_RUN_SUFFIX,
    )


def make_profile_name(template: str | None = None) -> str:
    """The test profile name: date-scoped and run-scoped (overridable)."""
    return _resolve_profile_name(template)


def write_private_text(path: Any, text: str) -> None:
    """Write a harness artifact 0600, or refuse: reports and configs quote account data.

    The run report names profile ids, entry names and call arguments; the
    generated MCP config carries the server env. Both must not be readable
    by other users of the CI machine the way a umask-default 0644 would be.
    Fail closed: the tightening ``fchmod`` covers a pre-existing permissive
    file; if it fails, nothing is written and the error propagates - an
    artifact that cannot be made private is an artifact not written.
    """
    p = Path(path)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # the creation mode only covers a brand-new file
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


# =========================================================================== #
# Result model (pure helpers are unit-tested)
# =========================================================================== #


@dataclass
class CheckResult:
    """Outcome of a single harness check."""

    id: str
    section: str
    description: str
    status: str  # "passed" | "skipped" | "failed"
    mcp_tool: str | None = None
    mcp_request: dict[str, Any] | None = None
    mcp_response: Any = None
    rest_verification: Any = None
    reason: str = ""
    error: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "description": self.description,
            "status": self.status,
            "mcp_tool": self.mcp_tool,
            "mcp_request": self.mcp_request,
            "mcp_response": self.mcp_response,
            "rest_verification": self.rest_verification,
            "reason": self.reason,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class Report:
    started_at: float = 0.0
    finished_at: float = 0.0
    api_key_present: bool = False
    profile_id: str | None = None
    profile_name: str | None = None
    # Harness-owned lifecycle work for a targeted (step-range) run: how the
    # test profile was provisioned when the range excluded profiles.create.
    setup: str = ""
    cleanup: str = ""
    skipped_sections: list[str] = field(default_factory=list)
    # Events that say the run did not measure what it claims to: a test profile
    # deleted by something outside the run, a provider outage that ended it.
    # Printed by --summarize so a contaminated number cannot be read as a score.
    notes: list[str] = field(default_factory=list)
    results: list[CheckResult] = field(default_factory=list)
    # LLM-actor metadata (the "who acted" half of the report).
    actor_kind: str = ""
    actor_command: list[str] = field(default_factory=list)
    actor_tool_calls: int = 0
    actor_tools_used: list[str] = field(default_factory=list)
    actor_final_text: str = ""
    actor_stderr: str = ""
    actor_raw_lines: list[str] = field(default_factory=list)
    actor_duration_ms: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def verdict(self) -> str:
        return "FAIL" if self.failed else "PASS"

    def summary(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "total": self.total,
            "passed": self.passed,
            "skipped": self.skipped,
            "failed": self.failed,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "setup": self.setup,
            "cleanup": self.cleanup,
            "skipped_sections": self.skipped_sections,
            "notes": self.notes,
            "actor_kind": self.actor_kind,
            "actor_tool_calls": self.actor_tool_calls,
            "actor_tools_used": self.actor_tools_used,
            "actor_duration_ms": self.actor_duration_ms,
            "duration_ms": int((self.finished_at - self.started_at) * 1000),
        }


def check_from_call(
    cid: str,
    section: str,
    description: str,
    *,
    mcp_tool: str | None = None,
    mcp_request: dict[str, Any] | None = None,
    mcp_response: Any = None,
    rest_verification: Any = None,
    duration_ms: int = 0,
) -> CheckResult:
    """Build a passing/skipped/failed CheckResult from a captured tool result.

    * a captured tool exception (``_tool_error``)        -> failed
    * a server-reported ``unsupported`` signal           -> skipped
    * an explicit ``skip_reason`` / ``skipped`` payload  -> skipped
    * a ``{"error": ...}`` structured body               -> failed
    * a raised tool error (``is_error`` True)            -> failed
    * otherwise                                          -> passed
    """
    status = "passed"
    reason = ""
    error = ""

    tool_error = _extract_tool_error(mcp_response)
    if tool_error is not None:
        status = "failed"
        error = f"MCP tool error: {tool_error}"
    elif _is_not_supported(mcp_response):
        status = "skipped"
        reason = f"unsupported by the server: {_not_supported_reason(mcp_response)}"
    elif _is_skip(mcp_response):
        status = "skipped"
        reason = _skip_reason(mcp_response)
    elif _is_error_body(mcp_response):
        status = "failed"
        error = f"MCP returned error body: {_error_body_text(mcp_response)}"
    elif _is_raised_error(mcp_response):
        status = "failed"
        error = f"MCP tool raised an error: {_raised_error_text(mcp_response)}"

    return CheckResult(
        id=cid,
        section=section,
        description=description,
        status=status,
        mcp_tool=mcp_tool,
        mcp_request=mcp_request,
        mcp_response=mcp_response,
        rest_verification=rest_verification,
        reason=reason,
        error=error,
        duration_ms=duration_ms,
    )


def _extract_tool_error(res: Any) -> str | None:
    """Return the error text if a captured call raised (stored by the caller)."""
    if isinstance(res, dict):
        te = res.get("_tool_error")
        if te:
            return f"{res.get('_tool_error_type', 'Exception')}: {te}"
    return None


def _structured(res: Any) -> Any:
    """Pull the structured payload out of a captured tool result."""
    if isinstance(res, dict):
        return res.get("structured")
    return None


def _is_error_body(res: Any) -> bool:
    s = _structured(res)
    return isinstance(s, dict) and "error" in s and bool(s.get("error"))


def _error_body_text(res: Any) -> str:
    s = _structured(res)
    return str(s.get("error")) if isinstance(s, dict) else str(s)


def _is_skip(res: Any) -> bool:
    s = _structured(res)
    if isinstance(s, dict) and s.get("skip_reason"):
        return True
    return isinstance(s, dict) and s.get("skipped") is True


def _is_not_supported(res: Any) -> bool:
    """The server explicitly reports the operation is not supported / not enabled."""
    s = _structured(res)
    if isinstance(s, dict) and s.get("unsupported") is True:
        return True
    if isinstance(s, dict):
        for key in ("detail", "detailMessage", "error"):
            v = s.get(key)
            if isinstance(v, str) and v.startswith("unsupported"):
                return True
    if isinstance(res, dict) and res.get("is_error"):
        text = res.get("text", "")
        if isinstance(text, str) and text.lower().startswith("unsupported"):
            return True
    return False


def _skip_reason(res: Any) -> str:
    s = _structured(res)
    if isinstance(s, dict):
        if s.get("skip_reason"):
            return str(s["skip_reason"])
        if s.get("skipped") is True:
            return str(s.get("reason") or "skipped")
        if s.get("unsupported") is True:
            return _not_supported_reason(res)
    if isinstance(res, dict) and res.get("is_error"):
        return str(res.get("text", "skipped"))
    return "skipped"


def _not_supported_reason(res: Any) -> str:
    s = _structured(res)
    if isinstance(s, dict):
        return str(s.get("detail") or s.get("detailMessage") or "unsupported")
    if isinstance(res, dict) and res.get("is_error"):
        return str(res.get("text", "unsupported"))
    return "unsupported"


def _is_raised_error(res: Any) -> bool:
    """A tool call that raised (``is_error`` True) with an error message, i.e. the
    server hit a real (e.g. HTTP) error rather than returning a clean payload."""
    if not isinstance(res, dict) or not res.get("is_error"):
        return False
    return bool(res.get("text"))


def _raised_error_text(res: Any) -> str:
    return str(res.get("text", ""))[:500] if isinstance(res, dict) else ""


# =========================================================================== #
# REST read-back helpers (pure; used by the measuring half)
# =========================================================================== #


def _resp_status(resp: Any) -> Any:
    return resp.get("status") if isinstance(resp, dict) else None


def _verify_settings_assert(rest_resp: Any, expected: dict[str, Any]) -> bool:
    """Check every field in ``expected`` is present (nested or top-level) in the
    REST body with the expected value."""
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return False
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return False
    for key, want in expected.items():
        if key in body and body[key] == want:
            continue
        inner = body.get("data")
        if isinstance(inner, dict) and key in inner and inner[key] == want:
            continue
        return False
    return True


def _list_values(name: str, tag: str) -> list[str]:
    """The entry this run leaves in list type ``name``, from ``LIST_ENTRIES``."""
    try:
        template = LIST_ENTRIES[name]
    except KeyError:
        raise ValueError(f"unknown list type {name}") from None
    return [template.format(tag=tag)]


def _unwrap_id(response: Any) -> str:
    """The ``id`` a create/add answer carries, unwrapping its ``data`` envelope.

    The NextDNS API wraps an object answer in ``data`` and the MCP tools answer
    with whatever their endpoint returned, so the id sits at either depth
    depending on which layer the caller is reading. Every site that re-typed this
    unwrap kept its own wording for the same two lines.
    """
    if not isinstance(response, dict):
        return ""
    inner = response.get("data") if isinstance(response.get("data"), dict) else response
    return str(inner.get("id") or "") if isinstance(inner, dict) else ""


def _list_added(name: str, tag: str) -> str | None:
    # Only free-form (domain) lists accept a synthetic "added" entry. Closed
    # enums (natives, tlds, categories, services, blocklists) reject unknown ids.
    if name in ("allowlist", "denylist"):
        return f"ai-e2e-{name}-add-{tag}.example.com"
    return None


def _list_contains(rest_resp: Any, values: list[str]) -> bool:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return False
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return False
    ids = {str((r or {}).get("id", "")) for r in body.get("data", [])}
    return all(str(v) in ids for v in values)


# The three verdicts an absence check can reach. Only ``absent`` is a fact about
# the account; ``indeterminate`` is a fact about the read.
ABSENT = "absent"
PRESENT = "present"
INDETERMINATE = "indeterminate"
ENTRY_FOUND = "found"
ENTRY_MISSING = "missing"


def _indeterminate(reason: str) -> tuple[bool, str]:
    """A read that proves nothing: never an absence, always a named reason."""
    return False, f"{INDETERMINATE}: {reason}"


def _readable_rows(rest_resp: Any) -> list[Any] | None:
    """The rows of a successful, correctly shaped list answer.

    ``None`` when the read proves nothing — a transport error, a 5xx, or a body
    that carries no list. Callers must not read that as an empty list: the
    difference between "nothing here" and "I could not look" is the whole of an
    absence verdict.
    """
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return None
    body = rest_resp.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    return body["data"]


def _confirmed_gone(rest_resp: Any) -> bool:
    """A 404 on the collection's own path: the strongest absence the API offers."""
    return isinstance(rest_resp, dict) and rest_resp.get("status") == 404


def _absent_verdict(rest_resp: Any, wanted: list[str], key: str) -> tuple[bool, str]:
    """Shared shape of the two absence checks: 404 or a readable list without them.

    ``key`` names the field the rows are identified by (``id`` for a list entry,
    ``name`` for a rewrite), so the reason text says which read failed.
    """
    if _confirmed_gone(rest_resp):
        return True, ABSENT
    rows = _readable_rows(rest_resp)
    if rows is None:
        status = _resp_status(rest_resp)
        return _indeterminate(
            f"the {key} read answered HTTP {status!r} without a list body, which proves nothing about it"
        )
    present = {str((row or {}).get(key, "")) for row in rows if isinstance(row, dict)}
    if any(str(value) in present for value in wanted):
        return False, PRESENT
    return True, ABSENT


def _list_absent(rest_resp: Any, values: list[str]) -> tuple[bool, str]:
    """Are these entries confirmed gone? Only a 404 or a readable list says so.

    Used to return ``True`` for a failed read on the theory that nothing can be
    present if the read itself failed; that made a 503 or a malformed body a
    passing deletion, so a cleanup the harness never observed was reported as one
    it verified. Returns ``(absent, verdict)``, with the verdict naming why when
    the answer is not a plain ``absent``/``present``.
    """
    return _absent_verdict(rest_resp, values, "id")


def _rewrites_absent(rest_resp: Any, names: list[str]) -> tuple[bool, str]:
    """The same three-way verdict for a rewrite record, which is named, not id-ed."""
    return _absent_verdict(rest_resp, names, "name")


def _entry_active(rest_resp: Any, value: str) -> tuple[Any, str]:
    """The exact entry's ``active`` flag, as the API read it back.

    ``(flag, verdict)``: ``flag`` is what the entry says (possibly ``None`` when
    the answer carries no such field) and the verdict is ``found``, ``missing``, or
    an indeterminate reason. An update step has to read the entry it asked to
    change — checking only that the list still holds something passes for an update
    that never touched it.
    """
    rows = _readable_rows(rest_resp)
    if rows is None:
        return _indeterminate(f"the entry read answered HTTP {_resp_status(rest_resp)!r} without a list body")
    for row in rows:
        if isinstance(row, dict) and str(row.get("id", "")) == str(value):
            return row.get("active"), ENTRY_FOUND
    return None, ENTRY_MISSING


def _entry_ids(rest_resp: Any) -> list[str]:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return []
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return []
    return [str(r.get("id")) for r in body.get("data", []) if isinstance(r, dict) and r.get("id")]


def _rewrites_contains(rest_resp: Any, names: list[str]) -> bool:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return False
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return False
    have = {(r or {}).get("name") for r in body.get("data", [])}
    return all(n in have for n in names)


def _analytics_has_data(rest_resp: Any) -> bool:
    """Heuristic: does the profile have any real query history to chart?"""
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return False
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, (int, float)) and v > 0:
                        return True
        return False
    for key in ("queries", "blockedQueries", "total"):
        if isinstance(body.get(key), (int, float)) and body[key] > 0:
            return True
    return False
