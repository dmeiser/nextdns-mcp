"""The REST verifiers for the AI E2E harness.

The responsibilities of ``_verify_step`` (``scripts/ai_e2e_harness.py``) live
here as one small async function per family of checks — profiles, settings,
lists, rewrites, analytics, DoH, plots/dashboard and logs — plus the
dispatcher that maps ``step.verify`` onto them. Every verifier returns the
same ``(status, error, reason, rest_verification)`` tuple the harness produced
before the split. The harness module keeps the thin delegator; this module is
the one that asks REST what actually happened.

Dispatch is by EXACT verifier name, and every family handler ends with an
unknown-name guard, so a verifier name nothing implements makes no REST call
at all and fails with ``unknown verifier <name>``. A prefix test here would
turn a typo such as ``logs_bogus`` into the ``logs_clear`` branch — a live
``DELETE /profiles/<id>/logs`` against the account, reported as a pass.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts import ai_e2e_common as common
    from scripts import ai_e2e_harness as harness
else:
    import ai_e2e_common as common
    import ai_e2e_harness as harness

# The verifier names each family below implements. The dispatcher matches these
# exactly: an unknown name must reach the unknown-verifier answer, never a
# neighbouring branch — one of which (logs_clear) is a destructive DELETE.
_PROFILE_VERIFIERS = ("profile_exists", "profile_renamed", "profile_gone")
_SETTINGS_VERIFIERS = ("settings_read", "settings_update")
_LIST_VERIFIERS = (
    "list_read",
    "list_replace",
    "list_add",
    "list_update",
    "list_remove",
    "list_remove_added",
)
_REWRITE_VERIFIERS = ("rewrite_list", "rewrite_add", "rewrite_delete")
_PLOT_VERIFIERS = ("plots", "dashboard")
_LOG_VERIFIERS = ("logs_get", "logs_download", "logs_clear")


def _unknown_verifier(verify: Any) -> tuple[str, str, str, dict[str, Any]]:
    """A name no verifier implements: failed, and no request was made."""
    return ("failed", f"unknown verifier {verify!r}", "", {})


async def _get(m: Any, path: str, **kw: Any) -> Any:
    # Read-only by construction: an earlier `_req(method, ...)` helper took a
    # method and DELETEd for anything that was not GET — a future call site
    # passing a write method would have issued a destructive read-back.
    return await m.rest_get(path, **kw)


def _rest_endpoint(base_profile: str | None, path: str) -> str:
    return f"GET {base_profile}{path}"


async def _verify_profile_steps(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """profile_exists / profile_renamed / profile_gone."""
    if step.verify not in _PROFILE_VERIFIERS:
        return _unknown_verifier(step.verify)
    if step.verify == "profile_exists":
        prof = await m.find_profile(targets.profile_name)
        if prof is None:
            prof = await m.find_profile(targets.renamed_name)
        ok = prof is not None
        return (
            "passed" if ok else "failed",
            "" if ok else f"profile {targets.profile_name!r} not found via REST",
            "",
            {"endpoint": "GET /profiles", "verified_present": ok},
        )
    if step.verify == "profile_renamed":
        prof = await m.find_profile(targets.renamed_name)
        ok = prof is not None and (not pid or str(prof.get("id")) == pid)
        return (
            "passed" if ok else "failed",
            "" if ok else f"profile not found under renamed name {targets.renamed_name!r}",
            "",
            {"endpoint": "GET /profiles", "verified_renamed": ok},
        )
    # profile_gone. An empty pid means this run never established a profile
    # id, and absence can only be proven FOR an id: the old ``(True, ABSENT)``
    # shortcut turned a failed profiles.create into a verified deletion pass.
    if not pid:
        return (
            "failed",
            "no profile id was established for this run, so deletion cannot be confirmed",
            "",
            {"endpoint": "GET /profiles/{id}", "verified_gone": False, "verdict": common.INDETERMINATE},
        )
    gone, verdict = await harness._profile_gone(m, pid, targets)
    return (
        "passed" if gone else "failed",
        "" if gone else f"profile {pid} not confirmed deleted ({verdict})",
        "",
        {"endpoint": f"GET /profiles/{pid}", "verified_gone": gone, "verdict": verdict},
    )


async def _verify_settings_steps(
    m: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """settings_read / settings_update."""
    if step.verify not in _SETTINGS_VERIFIERS:
        return _unknown_verifier(step.verify)
    base_profile = f"/profiles/{pid}" if pid else None
    if step.verify == "settings_read":
        rest = await _get(m, f"{base_profile}{vd['spec']['path']}")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else f"REST GET settings/{vd['cat']} not ok",
            "",
            {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "rest_status": common._resp_status(rest)},
        )
    # settings_update
    rest = await _get(m, f"{base_profile}{vd['spec']['path']}")
    ok = common._verify_settings_assert(rest, vd["spec"]["assert"])
    return (
        "passed" if ok else "failed",
        "" if ok else f"settings/{vd['cat']} read-back does not match {vd['spec']['assert']}",
        "",
        {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "verified": ok},
    )


async def _verify_list_steps(m: Any, pid: str, step: Any, vd: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    """list_read / list_replace / list_add / list_update / list_remove / list_remove_added."""
    if step.verify not in _LIST_VERIFIERS:
        return _unknown_verifier(step.verify)
    base_profile = f"/profiles/{pid}" if pid else None
    endpoint = f"{base_profile}{vd['spec']['path']}"
    if step.verify == "list_read":
        rest = await _get(m, endpoint)
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else f"REST GET list {vd['name']} not ok",
            "",
            {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "rest_status": common._resp_status(rest)},
        )
    if step.verify == "list_replace":
        rest = await _get(m, endpoint)
        ok = common._list_contains(rest, vd["values"])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} does not contain {vd['values']}",
            "",
            {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "verified": ok},
        )
    if step.verify == "list_add":
        rest = await _get(m, endpoint)
        # Only this step's own effect: the entry it asked to add. Checking the
        # earlier step's entries as well let a failed `replace` cascade into the
        # `add` verdict and blame the wrong entry.
        ok = common._list_contains(rest, [vd["added"]])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} does not contain the entry this step asked to add: {vd['added']!r}",
            "",
            {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "verified": ok},
        )
    if step.verify == "list_update":
        rest = await _get(m, endpoint)
        # Read the exact entry this step asked to change, not just that the
        # list still holds something: an update that set active=true (or
        # touched another entry) used to pass this check.
        flag, verdict = common._entry_active(rest, vd["values"][0])
        ok = verdict == common.ENTRY_FOUND and flag is False
        return (
            "passed" if ok else "failed",
            ""
            if ok
            else f"list {vd['name']} entry {vd['values'][0]!r} did not read back inactive ({verdict}: active={flag!r})",
            "",
            {
                "endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]),
                "verified_inactive": ok,
                "verdict": verdict,
                "active": flag,
            },
        )
    if step.verify == "list_remove":
        rest = await _get(m, endpoint)
        absent, verdict = common._list_absent(rest, vd["values"])
        return (
            "passed" if absent else "failed",
            "" if absent else f"list {vd['name']} removal of {vd['values']} not confirmed ({verdict})",
            "",
            {
                "endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]),
                "verified_absent": absent,
                "verdict": verdict,
            },
        )
    # list_remove_added
    rest = await _get(m, endpoint)
    absent, verdict = common._list_absent(rest, [vd["added"]])
    return (
        "passed" if absent else "failed",
        "" if absent else f"list {vd['name']} removal of added entry {vd['added']!r} not confirmed ({verdict})",
        "",
        {
            "endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]),
            "verified_absent": absent,
            "verdict": verdict,
        },
    )


async def _verify_rewrite_steps(
    m: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """rewrite_list / rewrite_add / rewrite_delete."""
    if step.verify not in _REWRITE_VERIFIERS:
        return _unknown_verifier(step.verify)
    base_profile = f"/profiles/{pid}" if pid else None
    endpoint = f"{base_profile}/rewrites"
    if step.verify == "rewrite_list":
        rest = await _get(m, endpoint)
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else "REST GET rewrites not ok",
            "",
            {"endpoint": f"GET {endpoint}", "rest_status": common._resp_status(rest)},
        )
    rec = vd["record"]
    if step.verify == "rewrite_add":
        rest = await _get(m, endpoint)
        ok = common._rewrites_contains(rest, [rec["name"]])
        return (
            "passed" if ok else "failed",
            "" if ok else f"rewrite {rec['name']!r} not found via REST",
            "",
            {"endpoint": f"GET {endpoint}", "verified": ok},
        )
    # rewrite_delete
    rest = await _get(m, endpoint)
    # `not _rewrites_contains(...)` used to make a failed or malformed read
    # of the rewrites a proof the record was gone; only a 404 or a readable
    # list without it says so.
    absent, verdict = common._rewrites_absent(rest, [rec["name"]])
    return (
        "passed" if absent else "failed",
        "" if absent else f"rewrite {rec['name']!r} deletion not confirmed ({verdict})",
        "",
        {"endpoint": f"GET {endpoint}", "verified_absent": absent, "verdict": verdict},
    )


async def _verify_analytics_step(
    m: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """analytics (aggregate + series)."""
    base_profile = f"/profiles/{pid}" if pid else None
    metric = vd["metric"]
    series = bool(vd.get("series"))
    suffix = ";series" if series else ""
    params: dict[str, Any] = {"from": "-1d", "to": "now"}
    if series:
        params["interval"] = 3600
    if metric == "destinations":
        params["type"] = "countries"
    rest = await _get(m, f"{base_profile}/analytics/{metric}{suffix}", params=params)
    ok = bool(isinstance(rest, dict) and rest.get("ok"))
    return (
        "passed" if ok else "failed",
        "" if ok else f"REST analytics {metric}{suffix} not ok",
        "",
        {
            "endpoint": f"GET {base_profile}/analytics/{metric}{suffix}",
            "params": params,
            "rest_status": common._resp_status(rest),
        },
    )


async def _verify_plot_step(m: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    """plots / dashboard — the chart/widget is the payload; REST checks data."""
    plot_pid = os.environ.get("NEXTDNS_PLOT_PROFILE") or pid
    rest = await _get(m, f"/profiles/{plot_pid}/analytics/status")
    has_data = common._analytics_has_data(rest)
    if not has_data:
        return (
            "skipped",
            "",
            "no query history on the fresh profile; plot checks need data",
            # The path actually read: the data profile, not the test profile.
            {"endpoint": f"GET /profiles/{plot_pid}/analytics/status", "has_data": False},
        )
    return (
        "passed",
        "",
        "",
        {
            "has_data": True,
            "endpoint": f"GET /profiles/{plot_pid}/analytics/status",
            "note": "view generation verified by the server call log",
        },
    )


async def _verify_log_steps(m: Any, pid: str, step: Any) -> tuple[str, str, str, dict[str, Any]]:
    """logs_get / logs_download / logs_clear."""
    if step.verify not in _LOG_VERIFIERS:
        return _unknown_verifier(step.verify)
    base_profile = f"/profiles/{pid}" if pid else None
    if step.verify == "logs_get":
        rest = await _get(m, f"{base_profile}/logs", params={"limit": 10})
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else "REST GET logs not ok",
            "",
            {"endpoint": f"GET {base_profile}/logs", "rest_status": common._resp_status(rest)},
        )
    if step.verify == "logs_download":
        log_pid = os.environ.get("NEXTDNS_PLOT_PROFILE") or pid
        rest = await _get(m, f"/profiles/{log_pid}/logs", params={"limit": 10})
        # Log entries are mostly strings (domain, status, timestamps), so an
        # analytics-style numeric-data check would wrongly report "no logs".
        body = rest.get("body") if isinstance(rest, dict) else None
        has = bool(isinstance(body, dict) and body.get("data"))
        if not has:
            return (
                "skipped",
                "",
                "no retained logs yet (NextDNS log delay on a fresh profile)",
                # The path actually read: the data profile, not the test profile.
                {"endpoint": f"GET /profiles/{log_pid}/logs"},
            )
        return (
            "passed",
            "",
            "",
            {
                "has_logs": True,
                "endpoint": f"GET /profiles/{log_pid}/logs",
                "note": "CSV download verified by the server call log",
            },
        )
    # logs_clear
    rest = await m.rest_delete(f"{base_profile}/logs")
    ok = bool(isinstance(rest, dict) and rest.get("ok"))
    return (
        "passed" if ok else "failed",
        "" if ok else "REST DELETE logs not ok",
        "",
        {"endpoint": f"DELETE {base_profile}/logs", "rest_status": common._resp_status(rest)},
    )


async def verify_step(m: Any, targets: Any, pid: str, step: Any, run: Any) -> tuple[str, str, str, dict[str, Any]]:
    """REST-verify one step. Returns (status, error, reason, rest_verification)."""
    vd = step.vdata
    if step.verify in _PROFILE_VERIFIERS:
        return await _verify_profile_steps(m, targets, pid, step, vd)
    if step.verify in _SETTINGS_VERIFIERS:
        return await _verify_settings_steps(m, pid, step, vd)
    if step.verify in _LIST_VERIFIERS:
        return await _verify_list_steps(m, pid, step, vd)
    if step.verify in _REWRITE_VERIFIERS:
        return await _verify_rewrite_steps(m, pid, step, vd)
    if step.verify == "analytics":
        return await _verify_analytics_step(m, pid, step, vd)
    if step.verify == "tool_ok":
        # DoH resolution leaves no account state to read back, so REST cannot
        # decide this step at all. The server's call log does (see
        # _expected_call_ok): the lookup ran and answered.
        return ("passed", "", "", {"note": "read-only DoH lookup; verdict from the server call log"})
    if step.verify in _PLOT_VERIFIERS:
        return await _verify_plot_step(m, pid)
    if step.verify in _LOG_VERIFIERS:
        return await _verify_log_steps(m, pid, step)
    return _unknown_verifier(step.verify)
