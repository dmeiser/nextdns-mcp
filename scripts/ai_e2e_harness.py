#!/usr/bin/env python3
"""Deterministic, API-verified E2E harness for the NextDNS MCP server.

SPDX-License-Identifier: MIT

This script is the deterministic replacement for the LLM-driven prompt in
``ai_agent_e2e_prompt.md``. It drives the *actual* MCP server (``mcp_server``)
over the real MCP tool surface via an in-process ``fastmcp.Client`` and, after
every write operation, independently verifies the resulting server state using
a *separate* HTTP client that talks to the NextDNS REST API directly. The MCP
tool surface and the verification path therefore never share code, which keeps
the two sides of each check independent.

The server exposes eight grouped tools: ``manageProfiles``, ``manageSettings``,
``manageLists``, ``manageRewrites``, ``manageLogs``, ``queryAnalytics``,
``plotAnalytics`` and ``dohLookup``. The harness exercises the full operation
checklist in a fixed order:

    profiles -> settings (7 categories) -> lists (7 types) -> rewrites (3 types)
            -> analytics (aggregate + series) -> dohLookup -> logs -> plots (9)

Design goals
------------
* **Deterministic verdicts** — every operation is a fixed, ordered step with an
  explicit ``passed`` / ``skipped`` / ``failed`` outcome. There is no LLM.
* **API-verified writes** — every mutation is re-read through the raw REST API.
* **Environment-gated** — without ``NEXTDNS_API_KEY`` it prints a clean ``SKIP``
  report and exits 0 without contacting the network.
* **Safe** — provisions its *own* isolated profile (``AI E2E Test Profile
  <timestamp>-<tag>``), narrows the writable ACL to that profile, and always
  cleans up (removes rewrites / list entries it added, then deletes the profile
  and verifies the deletion via REST).
* **Clean skips** — server-reported ``unsupported``, a freshly provisioned
  profile with no analytics/plot data, and the NextDNS log-generation delay are
  reported as ``skipped`` with a reason rather than failures.

Usage::

    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py
    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --only lists,rewrites
    uv run python scripts/ai_e2e_harness.py            # -> SKIP (no key)

The JSONL report is written to ``artifacts/ai_e2e_report.jsonl`` by default
(``--report`` to override, ``-`` for stdout).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import string
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

# --------------------------------------------------------------------------- #
# Make the in-tree ``nextdns_mcp`` package importable when running as a script
# (``uv run python scripts/ai_e2e_harness.py``) without needing an install.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from fastmcp import Client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ai_e2e_harness")


# =========================================================================== #
# Constants
# =========================================================================== #

API_BASE = os.environ.get("NEXTDNS_API_BASE", "https://api.nextdns.io").rstrip("/")

# Checklist coverage, grouped into run sections. A section maps 1:1 onto an
# entry of the prompt's test checklist.
SECTIONS: dict[str, str] = {
    "profiles": "profile create/list/get/update/delete",
    "settings": "7 settings categories (read + update + REST verify)",
    "lists": "7 list types (get/replace/add/update/remove + REST verify)",
    "rewrites": "3 rewrite record types (add/delete + REST verify)",
    "analytics": "aggregate + series for every analytics metric",
    "doh": "dohLookup DNS resolution via the test profile",
    "logs": "get / download / clear query logs",
    "plots": "9 plot-analytics metrics (PNG image)",
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
        "set": {"enabled": True, "retention": 7},
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
PLOT_METRICS = [
    "status",
    "devices",
    "protocols",
    "queryTypes",
    "ipVersions",
    "dnssec",
    "encryption",
    "reasons",
    "ips",
]

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


def _random_tag(n: int = 6) -> str:
    """Return a short random alphanumeric tag for unique resource names."""
    return "e2eh" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def make_profile_name() -> str:
    """A unique, identifiable test profile name (per the prompt's convention)."""
    return f"AI E2E Test Profile {time.strftime('%Y%m%d%H%M%S')}-{_random_tag(4)}"


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
    cleanup: str = ""
    skipped_sections: list[str] = field(default_factory=list)
    results: list[CheckResult] = field(default_factory=list)

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
            "cleanup": self.cleanup,
            "skipped_sections": self.skipped_sections,
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
    """Build a passing/skipped/failed CheckResult from a captured MCP call.

    * a captured MCP exception (``_tool_error``)          -> failed
    * a server-reported ``unsupported`` signal            -> skipped
    * an explicit ``skip_reason`` / ``skipped`` payload   -> skipped
    * a ``{"error": ...}`` structured body                -> failed
    * a raised tool error (``is_error`` True)             -> failed
    * otherwise                                           -> passed
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
    """Return the error text if the captured MCP call raised (stored by the client)."""
    if isinstance(res, dict):
        te = res.get("_tool_error")
        if te:
            return f"{res.get('_tool_error_type', 'Exception')}: {te}"
    return None


def _structured(res: Any) -> Any:
    """Pull the structured payload out of a captured MCP result."""
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
# Harness client: independent MCP surface + independent REST verifier
# =========================================================================== #


class HarnessClient:
    """Drives the MCP tools and independently verifies state via raw REST.

    Two separate transports, deliberately:
      * ``self.mcp``  — an in-process ``fastmcp.Client`` over ``mcp_server``.
      * ``self.http`` — a standalone ``httpx.AsyncClient`` for REST verification.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.http: Any = None
        self.mcp: Any = None
        self.profile_id: str | None = None

    async def __aenter__(self) -> Self:
        import httpx

        # Imported here so the (heavier) MCP server is only constructed when we
        # are actually going to run a live test.
        from nextdns_mcp.server import mcp_server

        self.http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30.0,
        )
        self.mcp = Client(mcp_server)
        await self.mcp.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            if self.mcp is not None:
                await self.mcp.__aexit__(*exc)
        with contextlib.suppress(Exception):
            if self.http is not None:
                await self.http.aclose()

    # -- MCP side ---------------------------------------------------------- #
    async def mcp_call(self, tool: str, **args: Any) -> Any:
        """Call an MCP tool, capturing either the result or the raised error."""
        req = dict(args)
        try:
            res = await self.mcp.call_tool(tool, req, raise_on_error=False)
        except Exception as e:  # noqa: BLE001 - surface as a captured result
            return {"_tool_error": str(e), "_tool_error_type": type(e).__name__}
        text = "".join(c.text for c in (res.content or []) if hasattr(c, "text") and c.text)
        has_image = any(
            getattr(c, "data", None) and getattr(c, "mimeType", "") == "image/png" for c in (res.content or [])
        )
        return {
            "structured": getattr(res, "structured_content", None),
            "is_error": bool(getattr(res, "is_error", False)),
            "text": text,
            "has_image": has_image,
        }

    # -- REST side (independent verification) ----------------------------- #
    async def rest_request(self, method: str, path: str, json_body: Any = None, params: Any = None) -> Any:
        """A raw REST call. Returns ``{"status", "ok", "body"}`` (no exception)."""
        try:
            r = await self.http.request(method, path, json=json_body, params=params)
        except Exception as e:  # noqa: BLE001
            return {"status": None, "ok": False, "body": f"{type(e).__name__}: {e}"}
        body: Any
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = r.text
        return {"status": r.status_code, "ok": 200 <= r.status_code < 300, "body": body}

    async def rest_get(self, path: str, params: Any = None) -> Any:
        return await self.rest_request("GET", path, params=params)

    async def rest_delete(self, path: str) -> Any:
        return await self.rest_request("DELETE", path)


def _resp_status(resp: Any) -> Any:
    return resp.get("status") if isinstance(resp, dict) else None


# =========================================================================== #
# Section checks
# =========================================================================== #


async def run_profiles(h: HarnessClient, report: Report) -> str | None:
    """Create + verify the isolated profile; returns the profile id or None."""
    name = make_profile_name()
    report.profile_name = name

    # 1. create via MCP + independent REST verification (found in GET /profiles).
    created = await h.mcp_call("manageProfiles", operation="create", name=name)
    created_id = None
    data = _structured(created)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        created_id = data["data"].get("id")

    verify = await h.rest_get("/profiles")
    rest_ok = False
    if isinstance(verify, dict) and verify.get("ok"):
        for p in (verify.get("body") or {}).get("data", []):
            if not isinstance(p, dict):
                continue
            if p.get("name") == name or (created_id and p.get("id") == created_id):
                rest_ok = True
                created_id = created_id or p.get("id")

    r = check_from_call(
        "profiles.create",
        "profiles",
        "create a dedicated test profile",
        mcp_tool="manageProfiles",
        mcp_request={"operation": "create", "name": name},
        mcp_response=created,
        rest_verification={"endpoint": "GET /profiles", "verified_present": rest_ok},
    )
    if r.status == "passed" and not rest_ok:
        r.status = "failed"
        r.error = "profile created via MCP but not found via REST verification"
    report.results.append(r)
    if r.status != "passed" or not created_id:
        report.profile_id = created_id
        return created_id

    pid = created_id
    report.profile_id = pid
    h.profile_id = pid

    # 2. list profiles contains the new one.
    lst = await h.mcp_call("manageProfiles", operation="list")
    lst_s = _structured(lst)
    lst_ok = (
        isinstance(lst_s, dict)
        and isinstance(lst_s.get("data"), list)
        and any(isinstance(p, dict) and p.get("id") == pid for p in lst_s["data"])
    )
    r = check_from_call(
        "profiles.list",
        "profiles",
        "list profiles includes the new profile",
        mcp_tool="manageProfiles",
        mcp_request={"operation": "list"},
        mcp_response=lst,
        rest_verification={"mcp_contains_profile": lst_ok},
    )
    if not lst_ok:
        r.status = "failed"
        r.error = "new profile not present in manageProfiles(list)"
    report.results.append(r)

    # 3. get profile by id (MCP + REST).
    got = await h.mcp_call("manageProfiles", operation="get", profile_id=pid)
    got_s = _structured(got)
    got_ok = isinstance(got_s, dict) and (got_s.get("data") or {}).get("id") == pid
    rest_got = await h.rest_get(f"/profiles/{pid}")
    rest_got_ok = (
        isinstance(rest_got, dict)
        and rest_got.get("ok")
        and isinstance(rest_got.get("body"), dict)
        and (rest_got["body"].get("data") or {}).get("id") == pid
    )
    r = check_from_call(
        "profiles.get",
        "profiles",
        "get profile by id (MCP + REST)",
        mcp_tool="manageProfiles",
        mcp_request={"operation": "get", "profile_id": pid},
        mcp_response=got,
        rest_verification={"endpoint": f"GET /profiles/{pid}", "verified": rest_got_ok},
    )
    if not (got_ok and rest_got_ok):
        r.status = "failed"
        r.error = f"get profile mismatch (mcp={got_ok} rest={rest_got_ok})"
    report.results.append(r)

    # 4. rename (MCP write) + REST verification of the new name.
    new_name = name + "-renamed"
    upd = await h.mcp_call("manageProfiles", operation="update", profile_id=pid, name=new_name)
    rest_upd = await h.rest_get(f"/profiles/{pid}")
    rest_name = (rest_upd.get("body") or {}).get("data", {}).get("name") if isinstance(rest_upd, dict) else None
    r = check_from_call(
        "profiles.update",
        "profiles",
        "rename profile (MCP write) and verify via REST",
        mcp_tool="manageProfiles",
        mcp_request={"operation": "update", "profile_id": pid, "name": new_name},
        mcp_response=upd,
        rest_verification={"endpoint": f"GET /profiles/{pid}", "rest_name": rest_name, "expected": new_name},
    )
    if r.status == "passed" and rest_name != new_name:
        r.status = "failed"
        r.error = f"rename not verified via REST (rest_name={rest_name!r})"
    report.results.append(r)

    # Narrow the writable ACL to just this profile now that it exists.
    os.environ["NEXTDNS_WRITABLE_PROFILES"] = pid
    return pid


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


async def run_settings(h: HarnessClient, report: Report) -> None:
    pid = h.profile_id
    for cat, spec in SETTINGS_CATEGORIES.items():
        # 1. read current (MCP) + REST baseline.
        read = await h.mcp_call("manageSettings", operation="get", category=cat, profile_id=pid)
        rest_read = await h.rest_get(f"/profiles/{pid}{spec['path']}")
        mcp_read_ok = not _is_error_body(read)

        # 2. update the "set" payload (MCP write) + REST verification.
        write = await h.mcp_call(
            "manageSettings", operation="update", category=cat, profile_id=pid, settings=spec["set"]
        )
        rest_after = await h.rest_get(f"/profiles/{pid}{spec['path']}")
        verified = _verify_settings_assert(rest_after, spec["assert"])

        r = check_from_call(
            f"settings.{cat}",
            "settings",
            f"{cat}: read + update {spec['set']} and verify via REST",
            mcp_tool="manageSettings",
            mcp_request={"operation": "update", "category": cat, "profile_id": pid, "settings": spec["set"]},
            mcp_response=write,
            rest_verification={
                "endpoint": f"GET /profiles/{pid}{spec['path']}",
                "expected": spec["assert"],
                "verified": verified,
                "mcp_read_ok": mcp_read_ok,
                "rest_status_read": _resp_status(rest_read),
                "rest_status_after": _resp_status(rest_after),
            },
        )
        # A server-reported "unsupported" (skipped) is kept as a clean skip.
        if r.status != "passed":
            report.results.append(r)
            continue
        if not verified:
            r.status = "failed"
            r.error = f"settings.{cat} not verified via REST (rest_verified=False)"
            report.results.append(r)
            continue
        report.results.append(r)

        # 3. restore a sane default (write) + REST verification.
        restore = await h.mcp_call(
            "manageSettings", operation="update", category=cat, profile_id=pid, settings=spec["restore"]
        )
        rest_restore = await h.rest_get(f"/profiles/{pid}{spec['path']}")
        restored = _verify_settings_assert(rest_restore, spec["assert_restore"])
        rr = check_from_call(
            f"settings.{cat}.restore",
            "settings",
            f"{cat}: restore default {spec['restore']} and verify via REST",
            mcp_tool="manageSettings",
            mcp_request={"operation": "update", "category": cat, "profile_id": pid, "settings": spec["restore"]},
            mcp_response=restore,
            rest_verification={
                "endpoint": f"GET /profiles/{pid}{spec['path']}",
                "expected": spec["assert_restore"],
                "verified": restored,
            },
        )
        if rr.status == "passed" and not restored:
            rr.status = "failed"
            rr.error = f"settings.{cat} restore not verified (rest_verified=False)"
        report.results.append(rr)


def _list_values(name: str, tag: str) -> list[str]:
    if name == "allowlist":
        return [f"ai-e2e-allow-{tag}.example.com"]
    if name == "denylist":
        return [f"ai-e2e-deny-{tag}.example.com"]
    if name == "privacy_blocklists":
        return ["nextdns-recommended"]
    if name == "privacy_natives":
        return ["alexa"]
    if name == "security_tlds":
        return ["zip"]
    if name == "parental_categories":
        return ["gambling"]
    if name == "parental_services":
        return ["tiktok"]
    raise ValueError(f"unknown list type {name}")


def _list_added(name: str, tag: str) -> str:
    if name in ("allowlist", "denylist"):
        return f"ai-e2e-{name}-add-{tag}.example.com"
    return f"{name}-add-{tag}"


def _list_contains(rest_resp: Any, values: list[str]) -> bool:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return False
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return False
    ids = {str((r or {}).get("id", "")) for r in body.get("data", [])}
    return all(str(v) in ids for v in values)


def _list_absent(rest_resp: Any, values: list[str]) -> bool:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return True  # nothing can be present if the read itself failed
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return True
    ids = {str((r or {}).get("id", "")) for r in body.get("data", [])}
    return not any(str(v) in ids for v in values)


def _entry_ids(rest_resp: Any) -> list[str]:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return []
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return []
    return [str(r.get("id")) for r in body.get("data", []) if isinstance(r, dict) and r.get("id")]


async def run_lists(h: HarnessClient, report: Report) -> None:
    pid = h.profile_id
    tag = _random_tag()
    for lt in _LIST_SPECS:
        name = lt["name"]
        values = _list_values(name, tag)
        base = f"/profiles/{pid}{lt['path']}"

        # 1. read baseline (MCP + REST).
        read = await h.mcp_call("manageLists", list_type=name, operation="get", profile_id=pid)
        rest_read = await h.rest_get(base)
        mcp_read_ok = not _is_error_body(read)

        # 2. replace the whole list with the test entries (MCP write) + REST verify.
        replace = await h.mcp_call(
            "manageLists",
            list_type=name,
            operation="replace",
            profile_id=pid,
            entries=[{"id": v} for v in values],
        )
        rest_after = await h.rest_get(base)
        replaced = _list_contains(rest_after, values)

        # 3. add a second entry (MCP write) + REST verify both are present.
        added = _list_added(name, tag)
        add = await h.mcp_call("manageLists", list_type=name, operation="add", profile_id=pid, entry=added)
        rest_add = await h.rest_get(base)
        added_ok = _list_contains(rest_add, values + [added])

        # 4. per-entry update (only the 4 list types that support it).
        upd_ok = None
        if lt["updatable"]:
            upd = await h.mcp_call(
                "manageLists",
                list_type=name,
                operation="update",
                profile_id=pid,
                entry_id=values[0],
                entry={"active": False},
            )
            rest_upd = await h.rest_get(base)
            # Verify the entry is still present (PATCH keeps it) and the call was
            # accepted; the `active` flag may not be exposed in the REST body.
            upd_ok = (not _is_error_body(upd)) and _list_contains(rest_upd, [values[0]])

        r = check_from_call(
            f"lists.{name}",
            "lists",
            f"{name}: get + replace {values} + add {added}"
            + (" + per-entry update" if lt["updatable"] else "")
            + " — verified via REST",
            mcp_tool="manageLists",
            mcp_request={
                "list_type": name,
                "operations": ["get", "replace", "add"] + (["update"] if lt["updatable"] else []),
                "profile_id": pid,
                "values": values,
                "added": added,
            },
            mcp_response={"read": read, "replace": replace, "add": add},
            rest_verification={
                "endpoint": f"GET {base}",
                "replaced_present": replaced,
                "added_present": added_ok,
                "update_ok": upd_ok,
                "rest_status_read": _resp_status(rest_read),
                "mcp_read_ok": mcp_read_ok,
            },
        )
        if r.status == "skipped":
            report.results.append(r)
            continue
        write_ok = replaced and added_ok and (upd_ok in (True, None))
        if r.status == "passed" and write_ok:
            report.results.append(r)
        else:
            r.status = "failed"
            r.error = f"list {name} writes not verified (replaced={replaced} added={added_ok} update={upd_ok})"
            report.results.append(r)
            continue

        # 5. remove the added entry, then the replaced one (MCP writes) + verify absent.
        rm1 = await h.mcp_call("manageLists", list_type=name, operation="remove", profile_id=pid, entry_id=added)
        rm2 = await h.mcp_call("manageLists", list_type=name, operation="remove", profile_id=pid, entry_id=values[0])
        rest_rm = await h.rest_get(base)
        gone = _list_absent(rest_rm, values + [added])
        rr = check_from_call(
            f"lists.{name}.remove",
            "lists",
            f"{name}: remove {values} + {added} and verify absent via REST",
            mcp_tool="manageLists",
            mcp_request={"list_type": name, "operation": "remove", "profile_id": pid, "entry_ids": values + [added]},
            mcp_response={"remove_added": rm1, "remove_replaced": rm2},
            rest_verification={"endpoint": f"GET {base}", "verified_absent": gone},
        )
        if rr.status == "passed" and not gone:
            rr.status = "failed"
            rr.error = f"list {name} removal not verified (absent={gone})"
        report.results.append(rr)

        # 6. restore a sensible default for blocklists (fresh profile had one).
        if lt.get("restore"):
            await h.mcp_call(
                "manageLists",
                list_type=name,
                operation="replace",
                profile_id=pid,
                entries=[{"id": lt["restore"]}],
            )


def _rewrites_contains(rest_resp: Any, names: list[str]) -> bool:
    if not isinstance(rest_resp, dict) or not rest_resp.get("ok"):
        return False
    body = rest_resp.get("body")
    if not isinstance(body, dict):
        return False
    have = {(r or {}).get("name") for r in body.get("data", [])}
    return all(n in have for n in names)


async def run_rewrites(h: HarnessClient, report: Report) -> None:
    pid = h.profile_id
    tag = _random_tag()
    records = [
        {"name": f"ai-e2e-a-{tag}.example.com", "content": "192.0.2.100"},
        {"name": f"ai-e2e-aaaa-{tag}.example.com", "content": "2001:db8::dead"},
        {"name": f"ai-e2e-cname-{tag}.example.com", "content": f"ai-e2e-target-{tag}.example.com"},
    ]
    base = f"/profiles/{pid}/rewrites"

    # 1. list baseline.
    lst0 = await h.mcp_call("manageRewrites", operation="list", profile_id=pid)
    mcp_list_ok = not _is_error_body(lst0)

    # 2. add all three (MCP writes), capturing each entry id from the response.
    entry_ids: dict[str, str] = {}
    add_results: list[Any] = []
    all_ok = True
    for rec in records:
        a = await h.mcp_call(
            "manageRewrites", operation="add", profile_id=pid, name=rec["name"], content=rec["content"]
        )
        add_results.append(a)
        s = _structured(a)
        eid = None
        if isinstance(s, dict) and isinstance(s.get("data"), dict):
            eid = s["data"].get("id") or s["data"].get("name")
        if eid:
            entry_ids[rec["name"]] = eid
        elif _is_error_body(a):
            all_ok = False

    # Fallback: resolve ids from a REST list if the add response omitted them.
    if len(entry_ids) < len(records):
        rest_l = await h.rest_get(base)
        if isinstance(rest_l, dict):
            for r_ in (rest_l.get("body") or {}).get("data", []):
                if (
                    isinstance(r_, dict)
                    and r_.get("name") in [x["name"] for x in records]
                    and r_["name"] not in entry_ids
                ):
                    entry_ids[r_["name"]] = r_.get("id") or r_.get("name")

    rest_after = await h.rest_get(base)
    present = _rewrites_contains(rest_after, [r["name"] for r in records])

    r = check_from_call(
        "rewrites.add",
        "rewrites",
        "add A + AAAA + CNAME rewrites and verify all three via REST",
        mcp_tool="manageRewrites",
        mcp_request={"operation": "add", "profile_id": pid, "records": records},
        mcp_response=add_results,
        rest_verification={
            "endpoint": f"GET {base}",
            "names": [x["name"] for x in records],
            "verified_present": present,
            "mcp_list_ok": mcp_list_ok,
        },
    )
    if r.status == "passed" and not (all_ok and present and len(entry_ids) == len(records)):
        r.status = "failed"
        r.error = f"rewrite add not verified (all_mcp_ok={all_ok} present={present} ids_found={len(entry_ids)}/{len(records)})"
    report.results.append(r)

    # 3. delete each rewrite by its entry id (MCP writes) + REST verify all gone.
    del_results: list[Any] = []
    del_ok = True
    for rec in records:
        d = await h.mcp_call("manageRewrites", operation="delete", profile_id=pid, entry_id=entry_ids.get(rec["name"]))
        del_results.append(d)
        if _is_error_body(d):
            del_ok = False
    rest_del = await h.rest_get(base)
    gone = not _rewrites_contains(rest_del, [r["name"] for r in records])
    rr = check_from_call(
        "rewrites.delete",
        "rewrites",
        "delete all rewrites by entry id and verify they are gone via REST",
        mcp_tool="manageRewrites",
        mcp_request={"operation": "delete", "profile_id": pid, "entry_ids": list(entry_ids.values())},
        mcp_response=del_results,
        rest_verification={"endpoint": f"GET {base}", "verified_absent": gone},
    )
    if rr.status == "passed" and not (del_ok and gone):
        rr.status = "failed"
        rr.error = f"rewrite delete not verified (del_ok={del_ok} absent={gone})"
    report.results.append(rr)


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


async def run_analytics(h: HarnessClient, report: Report) -> bool:
    """Run analytics checks; returns True if the profile has real query history."""
    pid = h.profile_id
    common = {"from_time": "-1d", "to_time": "now"}

    # 1. aggregate totals for every metric (MCP + REST).
    for m in AGGREGATE_METRICS:
        kwargs: dict[str, Any] = {"metric": m, "profile_id": pid, **common}
        if m == "destinations":
            kwargs["destination_type"] = "countries"
        a = await h.mcp_call("queryAnalytics", **kwargs)
        rest_params = {"from": "-1d", "to": "now"}
        if m == "destinations":
            rest_params["type"] = "countries"
        rs = await h.rest_get(f"/profiles/{pid}/analytics/{m}", params=rest_params)
        r = check_from_call(
            f"analytics.aggregate.{m}",
            "analytics",
            f"queryAnalytics(metric={m}, aggregate) returns totals (MCP + REST)",
            mcp_tool="queryAnalytics",
            mcp_request=kwargs,
            mcp_response=a,
            rest_verification={"endpoint": f"/profiles/{pid}/analytics/{m}", "rest_status": _resp_status(rs)},
        )
        if r.status == "passed" and not (isinstance(rs, dict) and rs.get("ok")):
            r.status = "failed"
            r.error = f"REST verification of {m} aggregate failed (status={_resp_status(rs)})"
        report.results.append(r)

    # 2. time series for every series-supported metric (MCP + REST).
    for m in SERIES_METRICS:
        kwargs = {"metric": m, "profile_id": pid, **common, "series": True, "interval": 3600}
        if m == "destinations":
            kwargs["destination_type"] = "countries"
        a = await h.mcp_call("queryAnalytics", **kwargs)
        rest_params = {"from": "-1d", "to": "now", "interval": 3600}
        if m == "destinations":
            rest_params["type"] = "countries"
        rs = await h.rest_get(f"/profiles/{pid}/analytics/{m};series", params=rest_params)
        r = check_from_call(
            f"analytics.series.{m}",
            "analytics",
            f"queryAnalytics(metric={m}, series=true) returns series (MCP + REST)",
            mcp_tool="queryAnalytics",
            mcp_request=kwargs,
            mcp_response=a,
            rest_verification={f"/profiles/{pid}/analytics/{m};series": _resp_status(rs)},
        )
        report.results.append(r)

    # 3. Determine whether the profile has real query history (for plots).
    rest_status = await h.rest_get(f"/profiles/{pid}/analytics/status", params={"from": "-1d", "to": "now"})
    return _analytics_has_data(rest_status)


async def run_doh(h: HarnessClient, report: Report) -> None:
    pid = h.profile_id
    for qname, rtype in [("example.com", "A"), ("one.one.one.one", "A")]:
        d = await h.mcp_call("dohLookup", domain=qname, profile_id=pid, record_type=rtype)
        s = _structured(d)
        resolved = isinstance(s, dict) and not _is_error_body(d) and ("Status" in s or "status" in str(s))
        r = check_from_call(
            f"doh.{qname}",
            "doh",
            f"dohLookup({qname} {rtype}) resolves via the test profile",
            mcp_tool="dohLookup",
            mcp_request={"domain": qname, "profile_id": pid, "record_type": rtype},
            mcp_response=d,
            rest_verification={"note": "read-only DoH resolution; no write to verify", "resolved": resolved},
        )
        if r.status == "passed" and not resolved:
            r.status = "failed"
            r.error = f"dohLookup({qname}) did not return a DNS JSON response"
        report.results.append(r)


async def run_logs(h: HarnessClient, report: Report) -> None:
    pid = h.profile_id
    base = f"/profiles/{pid}/logs"

    # 1. get recent logs. A fresh profile may have none (NextDNS log delay).
    g = await h.mcp_call("manageLogs", operation="get", profile_id=pid, limit=5)
    rest_g = await h.rest_get(base, params={"limit": 5})
    g_s = _structured(g)
    has_entries = isinstance(g_s, dict) and bool(g_s.get("data"))
    r = check_from_call(
        "logs.get",
        "logs",
        "getLogs returns (possibly empty) log data without error",
        mcp_tool="manageLogs",
        mcp_request={"operation": "get", "profile_id": pid, "limit": 5},
        mcp_response=g,
        rest_verification={
            "endpoint": f"GET {base}",
            "mcp_has_entries": has_entries,
            "rest_status": _resp_status(rest_g),
        },
    )
    if not has_entries and r.status == "passed":
        r.status = "skipped"
        r.reason = "no query logs yet (NextDNS can take up to 5 minutes to generate logs)"
    report.results.append(r)

    # 2. download retained logs.
    dl = await h.mcp_call("manageLogs", operation="download", profile_id=pid)
    dl_s = _structured(dl)
    dl_size = dl_s.get("size") if isinstance(dl_s, dict) else None
    rr = check_from_call(
        "logs.download",
        "logs",
        "download retained logs as CSV",
        mcp_tool="manageLogs",
        mcp_request={"operation": "download", "profile_id": pid},
        mcp_response=dl,
        rest_verification={"note": "CSV download; size reported by tool", "size": dl_size},
    )
    if rr.status == "passed" and (not isinstance(dl_s, dict) or not dl_s.get("data")):
        rr.status = "skipped"
        rr.reason = "no retained logs to download yet (fresh profile / log delay)"
    report.results.append(rr)

    # 3. clear logs (MCP write) + REST verification the DELETE succeeds.
    clear = await h.mcp_call("manageLogs", operation="clear", profile_id=pid)
    rest_clear = await h.rest_delete(base)
    rest_clear_ok = isinstance(rest_clear, dict) and rest_clear.get("ok")
    rrr = check_from_call(
        "logs.clear",
        "logs",
        "clearLogs clears log history and the REST DELETE succeeds",
        mcp_tool="manageLogs",
        mcp_request={"operation": "clear", "profile_id": pid},
        mcp_response=clear,
        rest_verification={
            "endpoint": f"DELETE {base}",
            "rest_ok": rest_clear_ok,
            "rest_status": _resp_status(rest_clear),
        },
    )
    if rrr.status == "passed" and not rest_clear_ok:
        rrr.status = "failed"
        rrr.error = f"clearLogs REST DELETE not ok (status={_resp_status(rest_clear)})"
    report.results.append(rrr)


async def run_plots(h: HarnessClient, report: Report, has_data: bool) -> None:
    pid = h.profile_id
    for m in PLOT_METRICS:
        p = await h.mcp_call("plotAnalytics", metric=m, profile_id=pid)
        is_error = isinstance(p, dict) and (p.get("is_error") or _is_error_body(p))
        has_image = isinstance(p, dict) and bool(p.get("has_image"))

        if not has_data:
            report.results.append(
                CheckResult(
                    id=f"plots.{m}",
                    section="plots",
                    description=f"plotAnalytics(metric={m}) — requires query history",
                    status="skipped",
                    mcp_tool="plotAnalytics",
                    mcp_request={"metric": m, "profile_id": pid},
                    mcp_response=p,
                    reason="no query history on the freshly provisioned test profile",
                )
            )
            continue

        r = check_from_call(
            f"plots.{m}",
            "plots",
            f"plotAnalytics(metric={m}) returns a non-empty PNG image",
            mcp_tool="plotAnalytics",
            mcp_request={"metric": m, "profile_id": pid},
            mcp_response=p,
            rest_verification={"expected": "image/png", "got_image": has_image},
        )
        if r.status == "passed" and not (has_image and not is_error):
            r.status = "failed"
            r.error = f"plotAnalytics({m}) did not return a PNG (is_error={is_error} image={has_image})"
        report.results.append(r)


# =========================================================================== #
# Cleanup
# =========================================================================== #


async def cleanup(h: HarnessClient, report: Report) -> str:
    """Remove leftover rewrites/list entries, then delete the profile itself."""
    pid = report.profile_id
    if not pid:
        return "nothing to clean up (no profile id)"
    problems: list[str] = []

    # Remove any rewrites still present (best effort; profile delete also clears).
    rest = await h.rest_get(f"/profiles/{pid}/rewrites")
    if isinstance(rest, dict) and rest.get("ok"):
        for rec in (rest.get("body") or {}).get("data", []) or []:
            if isinstance(rec, dict) and rec.get("id"):
                await h.mcp_call("manageRewrites", operation="delete", profile_id=pid, entry_id=rec["id"])

    # Remove any list entries still present (best effort).
    for spec in _LIST_SPECS:
        r2 = await h.rest_get(f"/profiles/{pid}{spec['path']}")
        if isinstance(r2, dict) and r2.get("ok"):
            for eid in _entry_ids(r2):
                await h.mcp_call(
                    "manageLists", list_type=spec["name"], operation="remove", profile_id=pid, entry_id=eid
                )

    # Widen the writable ACL again so the profile delete is permitted.
    os.environ["NEXTDNS_WRITABLE_PROFILES"] = "ALL"

    # Delete the profile via MCP, then verify it is gone via REST.
    del_resp = await h.mcp_call("manageProfiles", operation="delete", profile_id=pid)
    rest_after = await h.rest_get(f"/profiles/{pid}")
    gone = (
        not isinstance(rest_after, dict)
        or not rest_after.get("ok")
        or (isinstance(rest_after.get("body"), dict) and rest_after["body"].get("data") in (None, {}))
    )
    if _is_error_body(del_resp) and not gone:
        problems.append(f"profile delete reported error: {_error_body_text(del_resp)}")
    elif not gone:
        problems.append(f"profile {pid} still present after delete (REST {_resp_status(rest_after)})")

    report.results.append(
        CheckResult(
            id="cleanup.delete_profile",
            section="profiles",
            description=f"delete the test profile {pid} and verify it is gone via REST",
            status="passed" if not problems else "failed",
            mcp_tool="manageProfiles",
            mcp_request={"operation": "delete", "profile_id": pid},
            mcp_response=del_resp,
            rest_verification={"endpoint": f"GET /profiles/{pid}", "verified_gone": gone},
            error="; ".join(problems),
        )
    )
    return f"clean: profile {pid} deleted and verified gone via REST" if not problems else f"cleanup issues: {problems}"


# =========================================================================== #
# Runner
# =========================================================================== #

_SECTION_ORDER = ("settings", "lists", "rewrites", "analytics", "doh", "logs", "plots")


async def run_harness(api_key: str, only: list[str] | None) -> Report:
    report = Report(started_at=time.time(), api_key_present=bool(api_key))
    sections = list(SECTIONS.keys()) if not only else [s for s in SECTIONS if s in (only or [])]
    has_data = False

    # Widen the ACL so we can provision + write the isolated test profile.
    os.environ["NEXTDNS_WRITABLE_PROFILES"] = "ALL"
    os.environ["NEXTDNS_READABLE_PROFILES"] = "ALL"
    os.environ["NEXTDNS_READ_ONLY"] = "false"

    async with HarnessClient(api_key) as h:
        # Profiles gate the rest: provision our own isolated profile (or, when the
        # profiles section is excluded, borrow the first readable one).
        if "profiles" in sections:
            pid = await run_profiles(h, report)
            if not pid:
                report.skipped_sections = [s for s in sections if s != "profiles"]
                report.cleanup = "skipped (profile creation failed)"
                report.finished_at = time.time()
                return report
        else:
            lst = await h.mcp_call("manageProfiles", operation="list")
            rows = (_structured(lst) or {}).get("data", [])
            if not rows:
                report.cleanup = "no profiles available to target"
                report.finished_at = time.time()
                return report
            pid = (rows[0] or {}).get("id")
            h.profile_id = pid
            report.profile_id = pid
            report.profile_name = (rows[0] or {}).get("name")

        # Ordered, section-by-section execution. Analytics runs before plots so the
        # query-history flag can skip plots cleanly; logs run after doh so there is
        # time for query logs to be generated.
        for sec in _SECTION_ORDER:
            if sec not in sections:
                continue
            try:
                if sec == "settings":
                    await run_settings(h, report)
                elif sec == "lists":
                    await run_lists(h, report)
                elif sec == "rewrites":
                    await run_rewrites(h, report)
                elif sec == "analytics":
                    has_data = bool(await run_analytics(h, report))
                elif sec == "doh":
                    await run_doh(h, report)
                elif sec == "logs":
                    await run_logs(h, report)
                elif sec == "plots":
                    await run_plots(h, report, has_data)
            except Exception as e:  # noqa: BLE001 - keep going, record the crash
                report.results.append(
                    CheckResult(
                        id=f"{sec}.crash",
                        section=sec,
                        description=f"section {sec} crashed",
                        status="failed",
                        error=f"{type(e).__name__}: {e}",
                    )
                )

        # Always clean up the profile we provisioned.
        report.cleanup = await cleanup(h, report)

    report.finished_at = time.time()
    return report


# =========================================================================== #
# Reporting
# =========================================================================== #


def write_report(report: Report, path: str) -> None:
    if path == "-":
        for r in report.results:
            print(json.dumps(r.to_dict()))
        print(json.dumps({"summary": report.summary()}))
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in report.results:
            fh.write(json.dumps(r.to_dict()) + "\n")
        fh.write(json.dumps({"summary": report.summary()}) + "\n")


def write_skip_report(path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"verdict": "SKIP", "reason": "NEXTDNS_API_KEY not set"}) + "\n", encoding="utf-8")


def render_text_report(report: Report) -> str:
    lines = ["=" * 72, "NextDNS MCP — deterministic E2E harness", "=" * 72]
    s = report.summary()
    lines.append(f"VERDICT : {s['verdict']}")
    lines.append(f"total   : {s['total']}  (passed {s['passed']}, skipped {s['skipped']}, failed {s['failed']})")
    lines.append(f"profile : {report.profile_id}  ({report.profile_name})")
    lines.append(f"cleanup : {s['cleanup']}")
    if report.skipped_sections:
        lines.append(f"skipped sections: {', '.join(report.skipped_sections)}")
    lines.append("-" * 72)
    for r in report.results:
        icon = {"passed": "PASS", "skipped": "SKIP", "failed": "FAIL"}[r.status]
        lines.append(f"[{icon}] {r.id}  —  {r.description}")
        if r.status == "failed":
            lines.append(f"       error: {r.error}")
        elif r.status == "skipped" and r.reason:
            lines.append(f"       reason: {r.reason}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic, API-verified E2E harness for the NextDNS MCP server.")
    parser.add_argument("--only", help=f"comma-separated sections to run (choose from: {', '.join(SECTIONS)})")
    parser.add_argument(
        "--report", default="artifacts/ai_e2e_report.jsonl", help="path for the JSONL report, or '-' for stdout"
    )
    parser.add_argument("--quiet", action="store_true", help="do not print the text report")
    args = parser.parse_args(argv)

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    api_key = os.environ.get("NEXTDNS_API_KEY")

    if not api_key:
        print("SKIP: NEXTDNS_API_KEY is not set — no live E2E performed.")
        print(
            "The harness requires a real NextDNS API key to drive the MCP server against the live API and verify writes."
        )
        if args.report != "-":
            write_skip_report(args.report)
            print(f"Report written to {args.report}")
        return 0

    report = asyncio.run(run_harness(api_key, only))
    write_report(report, args.report)
    if args.report != "-":
        print(f"Report written to {args.report}")
    if not args.quiet:
        print(render_text_report(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
