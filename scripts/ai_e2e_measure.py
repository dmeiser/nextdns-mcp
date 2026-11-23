"""Measuring half of the NextDNS MCP E2E harness: independent REST verification.

SPDX-License-Identifier: MIT

After the actor exits, the harness verifies what *actually* happened by
talking to the NextDNS REST API directly with the API key — independently of
the MCP path. This module holds the REST client (:class:`MeasureClient`), the
state checks built on it (profile gone/self-heal, stray sweep, created-id
proofs), and the harness-side profile lifecycle calls through a throwaway MCP
server (``_harness_provision_profile`` / ``_harness_delete_profile``). Split
out of ``ai_e2e_harness.py``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Self

import ai_e2e_common as common
from ai_e2e_checklist import _endpoint
from ai_e2e_common import (
    ABSENT,
    INDETERMINATE,
    PRESENT,
    _content_texts,
    _unwrap_id,
)
from ai_e2e_config import HarnessConfig, _expand_env_refs, _profile_list_cursor
from ai_e2e_task import TaskTargets


class MeasureClient:
    """Talks to the NextDNS REST API directly to measure what the actor did.

    This is deliberately a *separate* transport from the actor's MCP path — the
    harness never re-uses the actor's tool responses to judge the outcome.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.http: Any = None

    async def __aenter__(self) -> Self:
        import httpx

        self.http = httpx.AsyncClient(
            base_url=common.api_base(),
            headers={"X-Api-Key": self.api_key},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            if self.http is not None:
                await self.http.aclose()

    async def rest_request(self, method: str, path: str, json_body: Any = None, params: Any = None) -> Any:
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

    async def rest_post(self, path: str, json_body: Any = None) -> Any:
        return await self.rest_request("POST", path, json_body=json_body)

    async def rest_delete(self, path: str) -> Any:
        return await self.rest_request("DELETE", path)

    async def find_profile(self, name: str) -> dict[str, Any] | None:
        resp = await self.rest_get("/profiles")
        if isinstance(resp, dict) and resp.get("ok"):
            for p in (resp.get("body") or {}).get("data", []):
                if isinstance(p, dict) and p.get("name") == name:
                    return p
        return None


# =========================================================================== #
# Measurement checks (each produces a CheckResult)
# =========================================================================== #


async def _profile_gone(m: MeasureClient, pid: str, targets: TaskTargets) -> tuple[bool, str]:
    """Is the profile confirmed gone? Returns ``(gone, verdict)``.

    Absence needs proof, not a missing answer: only a 404 on the id, plus a
    readable listing that names neither the original nor the renamed name,
    says the profile is gone. A failed lookup (5xx, transport error) answers
    nothing about whether the profile exists, so it returns an indeterminate
    verdict instead of the ``True`` a failed read used to produce — which made
    a 503 read as a verified deletion.
    """
    by_id = await m.rest_get(f"/profiles/{pid}")
    if isinstance(by_id, dict) and by_id.get("ok"):
        body = by_id.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and data.get("id"):
            return False, PRESENT
    elif not (isinstance(by_id, dict) and by_id.get("status") == 404):
        status = by_id.get("status") if isinstance(by_id, dict) else by_id
        return False, f"{INDETERMINATE}: GET /profiles/{pid} answered HTTP {status!r}, which proves nothing"
    listing = await m.rest_get("/profiles")
    rows = common._readable_rows(listing)
    if rows is None:
        return False, (
            f"{INDETERMINATE}: the profile listing answered HTTP "
            f"{common._resp_status(listing)!r} without a list, so neither name could be checked"
        )
    names = {str(p.get("name", "")) for p in rows if isinstance(p, dict)}
    if targets.profile_name in names or targets.renamed_name in names:
        return False, PRESENT
    return True, ABSENT


async def _all_profile_ids(m: MeasureClient) -> dict[str, str] | None:
    """Map every profile id currently on the account to its name.

    ``None`` when the listing failed: an empty mapping from a failed read used
    to look like an empty account, which handed the stray sweep every profile on
    it. Callers must treat ``None`` as "account state uncertain".
    """
    # Same pagination contract the protection probe holds: one GET is only
    # the first page, and a page-truncated map under-protects and under-deletes.
    cursor: str | None = None
    seen_cursors: set[str] = set()
    rows: list[Any] = []
    while True:
        page = await m.rest_get("/profiles", params={"cursor": cursor} if cursor else None)
        page_rows = common._readable_rows(page)
        if page_rows is None:
            return None
        rows.extend(page_rows)
        body = page.get("body") if isinstance(page, dict) else None
        cursor = _profile_list_cursor(body)
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    out: dict[str, str] = {}
    for p in rows:
        if isinstance(p, dict) and p.get("id"):
            out[str(p["id"])] = str(p.get("name", ""))
    return out


def _never_delete_ids() -> set[str]:
    """Profile ids the stray sweep must never remove (protected + plot).

    Normalized exactly the way the server's ACL layer normalizes its
    ``NEXTDNS_PROTECTED_PROFILES`` (``config._normalized_profile_ids``:
    strip, lowercase, drop blanks) so an env var written with upper-case
    characters still matches the lower-case ids the profile listing and the
    sweep compare against - two normalizations would drift.
    """
    from nextdns_mcp.config import _normalized_profile_ids

    ids: set[str] = set()
    for var in ("NEXTDNS_PROTECTED_PROFILES", "NEXTDNS_PLOT_PROFILE"):
        ids |= _normalized_profile_ids(os.environ.get(var, ""))
    return ids


async def _ensure_plot_profile(m: MeasureClient, pid: str) -> str:
    """Resolve the profile the read-only plot/log steps target; returns a note.

    With NEXTDNS_PLOT_PROFILE unset those steps target the freshly created test
    profile, which by definition has no query history: every plot step skips as
    data-starved and the run silently measures nothing. So resolve the first
    readable profile that has data — the listing and probes go through the
    server, whose ACLs apply — and record it in the env every reader consults
    (prompt routing, REST evidence, `_never_delete_ids`). The test profile and
    the protected ids are never candidates: one is the write target of the run
    and the others are explicitly off-limits. Returns "" when there is nothing
    to resolve (already configured) or when resolution changed nothing, and a
    note in every other case so the report says what the plot steps read.
    """
    if os.environ.get("NEXTDNS_PLOT_PROFILE", "").strip():
        return ""
    listing = await _all_profile_ids(m)
    if listing is None:
        return (
            "NEXTDNS_PLOT_PROFILE is unset and the profile listing failed, so no fallback "
            "could be resolved; plot/log steps fall back to the empty test profile"
        )
    never = {pid.lower()} | _never_delete_ids()
    for candidate in listing:
        if candidate.lower() in never:
            continue
        probe = await m.rest_get(f"/profiles/{candidate}/analytics/status")
        if common._analytics_has_data(probe):
            os.environ["NEXTDNS_PLOT_PROFILE"] = candidate
            return (
                f"NEXTDNS_PLOT_PROFILE was unset; plot/log steps use profile {candidate} "
                f"({listing[candidate] or 'unnamed'}), the first readable profile with query data"
            )
    return (
        "NEXTDNS_PLOT_PROFILE is unset and no readable profile other than the test profile "
        "has query data; plot steps will skip as data-starved"
    )


def _proven_created_id(found: dict[str, Any] | None, created_ids: set[str]) -> str:
    """Use a found profile only if this run holds a create receipt for that id.

    A name lookup answers "which profile answers to this name", never "which
    profile did this run create". The run-unique suffix narrows collisions but
    closes nothing: an operator pinning NEXTDNS_E2E_PROFILE_NAME, or the actor
    itself calling createProfile with the run's name, both produce a profile
    that answers the lookup with no receipt behind it. Only membership in
    created_ids - ids taken from createProfile answers this run performed -
    is proof, so an id that is not a member is refused here and never becomes
    the run's pid, never gets written to, and never gets deleted.
    """
    if not found:
        return ""
    pid = str(found.get("id", ""))
    return pid if pid in created_ids else ""


def _deletable_by_this_run(pid: str, created_ids: set[str]) -> bool:
    """Whether teardown may delete ``pid``: only ids with a this-run receipt."""
    return bool(pid) and pid in created_ids


def _created_ids_from_records(records: list[dict[str, Any]]) -> set[str]:
    """Profile ids this run's server records PROVE were created here.

    A successful ``createProfile`` (POST /profiles) row carries the new id in
    its answer — the server's own receipt, not a guess from a before/after
    diff of the account. A diff-based sweep deleted anything that appeared
    during the run, which made a concurrent user's or another run's profile a
    deletion target; a receipt only exists for a create this run performed.
    """
    created_endpoint = _endpoint("createProfile")
    ids: set[str] = set()
    for record in records:
        if str(record.get("endpoint", "")) != created_endpoint or not record.get("ok"):
            continue
        # The dedicated evidence column first: it is lifted from the full
        # payload before any bounding, so a create receipt survives even when
        # the result projection was truncated. The result parse is the
        # fallback for rows written by older builds.
        evidence = record.get("evidence")
        if isinstance(evidence, dict) and evidence.get("created_id"):
            ids.add(str(evidence["created_id"]))
            continue
        try:
            payload = json.loads(str(record.get("result") or ""))
        except (ValueError, TypeError):
            continue
        if pid := _unwrap_id(payload):
            ids.add(pid)
    return ids


async def _sweep_stray_profiles(m: MeasureClient, created: set[str], keep: set[str]) -> str:
    """Delete profiles this run created beyond the test profile; returns a note.

    A model asked to create a profile sometimes creates several (a failed
    run left a stray 'new-profile' behind), and strays accumulate in a real
    account across runs. Only ids with a createProfile receipt from THIS run
    are candidates — never anything that merely appeared during it, which an
    unrelated user or a parallel run could have created — and the sweep reads
    the account first: if the listing itself fails, account state is uncertain
    and nothing is deleted, with the reason surfaced in the report.
    """
    protected = _never_delete_ids()
    keep_lower = {k.lower() for k in keep}
    doomed = sorted(pid for pid in created if pid and pid.lower() not in keep_lower and pid.lower() not in protected)
    if not doomed:
        return ""
    current = await _all_profile_ids(m)
    if current is None:
        return (
            "stray sweep skipped: the profile listing failed, so account state is uncertain and "
            f"nothing was deleted (run-created profiles still present: {', '.join(doomed)})"
        )
    for stray in doomed:
        if stray not in current:
            continue  # already gone (deleted mid-run, or by the run's own teardown)
        _progress(f"stray sweep: removing profile {stray} ({current[stray]!r}) created during the run")
        await m.rest_delete(f"/profiles/{stray}")
    return ""


async def _self_heal_profile(m: MeasureClient, pid: str, targets: TaskTargets) -> str:
    """Recreate the test profile via REST if the actor deleted it mid-run.

    A step prompt like "remove this entry from the denylist" can be misread as
    "delete the profile", and deleteProfile really does delete it —
    every later step then fails on 404s and the report measures a cascade, not
    the model's capability. The harness owns the profile lifecycle (the actor
    is told cleanup is harness-owned), so recreating it here is test
    scheduling, not an environment change: the prompts and the MCP server stay
    exactly as the actor sees them. The profile is recreated under its
    RENAMED name because every post-rename step verifies against that name.

    Returns the (possibly new) profile id, or "" if recreation failed.

    Recreation requires CONFIRMED deletion: a 404 on the profile read. A
    failed read that does not say 404 - a 5xx, a transport failure, a
    malformed answer - is indeterminate: the profile may well exist, and
    POSTing a duplicate on that guess would both pollute the account and
    hand the run a second profile it must not delete. Indeterminate reads
    leave the run on the original id and let later steps report honestly.
    """
    if not pid:
        return pid
    resp = await m.rest_get(f"/profiles/{pid}")
    if isinstance(resp, dict) and resp.get("ok"):
        return pid
    if not (isinstance(resp, dict) and resp.get("status") == 404):
        _progress(f"self-heal: read of test profile {pid} was indeterminate ({resp!r}); not recreating on a guess")
        return pid
    created = await m.rest_post("/profiles", {"name": targets.renamed_name})
    new_id = ""
    if isinstance(created, dict) and created.get("ok"):
        body = created.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and data.get("id"):
            new_id = str(data["id"])
    # No adopt-by-name fallback: a create whose response carried no id has no
    # proof of what (if anything) it produced, and a profile answering to the
    # run's name could belong to anything. Failing the self-heal honestly is
    # the fail-closed answer; the run reports the 404 cascade rather than
    # writing to - and later deleting - a profile it never proved it made.
    if new_id:
        _progress(f"self-heal: test profile {pid} was deleted mid-run; recreated as {new_id}")
        return new_id
    _progress(f"self-heal FAILED: could not recreate test profile {pid} ({created!r})")
    return ""


# =========================================================================== #
# Harness-owned profile lifecycle (targeted runs)
# =========================================================================== #


async def _mcp_call(server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one tool on the configured MCP server as an ordinary MCP client.

    Only used for harness-owned lifecycle work: provisioning and tearing down
    the test profile of a targeted run, which the actor is never asked to do.
    Going through the MCP tool rather than the REST side-channel keeps the
    server's own ACLs, argument coercion and error contract in the loop, so the
    harness creates exactly the profile the actor could create. The server is
    launched from the same ``server`` spec the actor's ``opencode.json`` gets,
    so the ACL and tool-search environment is identical.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(server["command"]),
        args=[str(a) for a in server.get("args", [])],
        # The config's env carries `{env:...}` references (SEC-4); this
        # harness-launched server has no client config loader in front of it,
        # so the references are expanded here, against this process's env.
        env=_expand_env_refs(server.get("env") or {}),
        cwd=str(server["cwd"]) if server.get("cwd") else None,
    )
    async with Client(transport) as client:
        res = await client.call_tool(tool, arguments)
    data = getattr(res, "data", None)
    if data is None:
        data = getattr(res, "structured_content", None)
    text = "".join(_content_texts(res))
    error = ""
    if getattr(res, "is_error", False):
        error = text or "tool call failed"
    if isinstance(data, dict) and data.get("error"):
        error = str(data["error"])
    return {"ok": not error, "data": data, "error": error}


def _profile_lifecycle_call(action: str, pid: str = "", name: str = "") -> tuple[str, dict[str, Any], str]:
    """The call the harness makes for a profile action it owns.

    Creating and deleting the test profile goes through the MCP server, not
    straight to the API, so the server's read/write ACLs apply to the harness's
    own bookkeeping too. The action names the endpoint tool that performs it:
    ``createProfile`` and ``deleteProfile``. Returns (tool, arguments, label).
    """
    if action == "create":
        return "createProfile", {"name": name}, "createProfile"
    return "deleteProfile", {"profile_id": pid}, "deleteProfile"


async def _harness_provision_profile(config: HarnessConfig, targets: TaskTargets) -> tuple[str, str]:
    """Create the test profile through the MCP tool; returns (profile_id, note).

    A targeted run that starts after ``profiles.create`` still needs a profile
    for every later step to act on, and the harness owns that profile either
    way, so it creates it the way the server exposes it.
    """
    tool, arguments, label = _profile_lifecycle_call("create", name=targets.profile_name)
    res = await _mcp_call(config.server, tool, arguments)
    data = res.get("data")
    body = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    pid = str(body.get("id", "")) if isinstance(body, dict) else ""
    if not pid:
        return "", f"FAILED: {label} returned {res.get('error') or data!r}"
    return pid, f"test profile {targets.profile_name!r} provisioned by the harness via {label}"


async def _harness_delete_profile(config: HarnessConfig, pid: str) -> str:
    """Delete the test profile through the MCP tool; returns an error string."""
    tool, arguments, _label = _profile_lifecycle_call("delete", pid=pid)
    res = await _mcp_call(config.server, tool, arguments)
    return str(res.get("error") or "")


# =========================================================================== #
# Orchestration
# =========================================================================== #


def _progress(msg: str) -> None:
    """Live progress feedback on stderr (never pollutes the JSONL report)."""
    print(f"[e2e] {msg}", file=sys.stderr, flush=True)
