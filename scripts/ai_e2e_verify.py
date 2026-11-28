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
from typing import Any

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


def _outcome(ok: bool, reason: str, rest: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    """The (status, error, reason, rest_verification) tuple every read-back answers."""
    return ("passed" if ok else "failed", "" if ok else reason, "", rest)


def _rest_ok(rest: Any) -> bool:
    """Whether a REST read answered successfully."""
    return bool(isinstance(rest, dict) and rest.get("ok"))


def _profile_base(pid: str) -> str | None:
    return f"/profiles/{pid}" if pid else None


def _rest_endpoint(base_profile: str | None, path: str) -> str:
    return f"GET {base_profile}{path}"


async def _find_profile_or_rename(m: Any, targets: Any) -> Any:
    """The profile under its original name, or under the name it was renamed to."""
    prof = await m.find_profile(targets.profile_name)
    if prof is None:
        prof = await m.find_profile(targets.renamed_name)
    return prof


async def _profile_exists_check(m: Any, targets: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    prof = await _find_profile_or_rename(m, targets)
    ok = prof is not None
    return _outcome(
        ok,
        f"profile {targets.profile_name!r} not found via REST",
        {"endpoint": "GET /profiles", "verified_present": ok},
    )


async def _profile_renamed_check(m: Any, targets: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    prof = await m.find_profile(targets.renamed_name)
    ok = prof is not None and (not pid or str(prof.get("id")) == pid)
    return _outcome(
        ok,
        f"profile not found under renamed name {targets.renamed_name!r}",
        {"endpoint": "GET /profiles", "verified_renamed": ok},
    )


def _profile_gone_without_id() -> tuple[str, str, str, dict[str, Any]]:
    # An empty pid means this run never established a profile id, and absence
    # can only be proven FOR an id: the old ``(True, ABSENT)`` shortcut turned
    # a failed profiles.create into a verified deletion pass.
    return (
        "failed",
        "no profile id was established for this run, so deletion cannot be confirmed",
        "",
        {"endpoint": "GET /profiles/{id}", "verified_gone": False, "verdict": common.INDETERMINATE},
    )


async def _profile_gone_check(m: Any, targets: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    if not pid:
        return _profile_gone_without_id()
    gone, verdict = await harness._profile_gone(m, pid, targets)
    return _outcome(
        gone,
        f"profile {pid} not confirmed deleted ({verdict})",
        {"endpoint": f"GET /profiles/{pid}", "verified_gone": gone, "verdict": verdict},
    )


_PROFILE_CHECKS = {
    "profile_exists": _profile_exists_check,
    "profile_renamed": _profile_renamed_check,
    "profile_gone": _profile_gone_check,
}


async def _verify_profile_steps(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """profile_exists / profile_renamed / profile_gone."""
    if step.verify not in _PROFILE_VERIFIERS:
        return _unknown_verifier(step.verify)
    return await _PROFILE_CHECKS[step.verify](m, targets, pid)


async def _settings_read_check(m: Any, pid: str, vd: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    base_profile = _profile_base(pid)
    rest = await _get(m, f"{base_profile}{vd['spec']['path']}")
    ok = _rest_ok(rest)
    return _outcome(
        ok,
        f"REST GET settings/{vd['cat']} not ok",
        {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "rest_status": common._resp_status(rest)},
    )


async def _settings_update_check(m: Any, pid: str, vd: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    base_profile = _profile_base(pid)
    rest = await _get(m, f"{base_profile}{vd['spec']['path']}")
    ok = common._verify_settings_assert(rest, vd["spec"]["assert"])
    return _outcome(
        ok,
        f"settings/{vd['cat']} read-back does not match {vd['spec']['assert']}",
        {"endpoint": _rest_endpoint(base_profile, vd["spec"]["path"]), "verified": ok},
    )


_SETTINGS_CHECKS = {
    "settings_read": _settings_read_check,
    "settings_update": _settings_update_check,
}


async def _verify_settings_steps(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """settings_read / settings_update."""
    if step.verify not in _SETTINGS_VERIFIERS:
        return _unknown_verifier(step.verify)
    return await _SETTINGS_CHECKS[step.verify](m, pid, vd)


def _list_read_check(rest: Any, vd: dict[str, Any], endpoint: str) -> tuple[str, str, str, dict[str, Any]]:
    ok = _rest_ok(rest)
    return _outcome(
        ok,
        f"REST GET list {vd['name']} not ok",
        {"endpoint": endpoint, "rest_status": common._resp_status(rest)},
    )


def _list_replace_check(rest: Any, vd: dict[str, Any], endpoint: str) -> tuple[str, str, str, dict[str, Any]]:
    ok = common._list_contains(rest, vd["values"])
    return _outcome(
        ok,
        f"list {vd['name']} does not contain {vd['values']}",
        {"endpoint": endpoint, "verified": ok},
    )


def _list_add_check(rest: Any, vd: dict[str, Any], endpoint: str) -> tuple[str, str, str, dict[str, Any]]:
    # Only this step's own effect: the entry it asked to add. Checking the
    # earlier step's entries as well let a failed `replace` cascade into the
    # `add` verdict and blame the wrong entry.
    ok = common._list_contains(rest, [vd["added"]])
    return _outcome(
        ok,
        f"list {vd['name']} does not contain the entry this step asked to add: {vd['added']!r}",
        {"endpoint": endpoint, "verified": ok},
    )


def _list_update_check(rest: Any, vd: dict[str, Any], endpoint: str) -> tuple[str, str, str, dict[str, Any]]:
    # Read the exact entry this step asked to change, not just that the
    # list still holds something: an update that set active=true (or
    # touched another entry) used to pass this check.
    flag, verdict = common._entry_active(rest, vd["values"][0])
    ok = verdict == common.ENTRY_FOUND and flag is False
    return _outcome(
        ok,
        f"list {vd['name']} entry {vd['values'][0]!r} did not read back inactive ({verdict}: active={flag!r})",
        {
            "endpoint": endpoint,
            "verified_inactive": ok,
            "verdict": verdict,
            "active": flag,
        },
    )


def _list_remove_check(rest: Any, vd: dict[str, Any], endpoint: str) -> tuple[str, str, str, dict[str, Any]]:
    absent, verdict = common._list_absent(rest, vd["values"])
    return _outcome(
        absent,
        f"list {vd['name']} removal of {vd['values']} not confirmed ({verdict})",
        {
            "endpoint": endpoint,
            "verified_absent": absent,
            "verdict": verdict,
        },
    )


def _list_remove_added_check(rest: Any, vd: dict[str, Any], endpoint: str) -> tuple[str, str, str, dict[str, Any]]:
    absent, verdict = common._list_absent(rest, [vd["added"]])
    return _outcome(
        absent,
        f"list {vd['name']} removal of added entry {vd['added']!r} not confirmed ({verdict})",
        {
            "endpoint": endpoint,
            "verified_absent": absent,
            "verdict": verdict,
        },
    )


_LIST_CHECKS = {
    "list_read": _list_read_check,
    "list_replace": _list_replace_check,
    "list_add": _list_add_check,
    "list_update": _list_update_check,
    "list_remove": _list_remove_check,
    "list_remove_added": _list_remove_added_check,
}


async def _verify_list_steps(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """list_read / list_replace / list_add / list_update / list_remove / list_remove_added."""
    if step.verify not in _LIST_VERIFIERS:
        return _unknown_verifier(step.verify)
    endpoint = _rest_endpoint(_profile_base(pid), vd["spec"]["path"])
    rest = await _get(m, f"{_profile_base(pid)}{vd['spec']['path']}")
    return _LIST_CHECKS[step.verify](rest, vd, endpoint)


async def _rewrite_list_check(m: Any, endpoint: str, vd: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    rest = await _get(m, endpoint)
    ok = _rest_ok(rest)
    return _outcome(
        ok, "REST GET rewrites not ok", {"endpoint": f"GET {endpoint}", "rest_status": common._resp_status(rest)}
    )


async def _rewrite_add_check(m: Any, endpoint: str, vd: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    rest = await _get(m, endpoint)
    ok = common._rewrites_contains(rest, [vd["record"]["name"]])
    return _outcome(
        ok,
        f"rewrite {vd['record']['name']!r} not found via REST",
        {"endpoint": f"GET {endpoint}", "verified": ok},
    )


async def _rewrite_delete_check(m: Any, endpoint: str, vd: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    rest = await _get(m, endpoint)
    # `not _rewrites_contains(...)` used to make a failed or malformed read
    # of the rewrites a proof the record was gone; only a 404 or a readable
    # list without it says so.
    absent, verdict = common._rewrites_absent(rest, [vd["record"]["name"]])
    return _outcome(
        absent,
        f"rewrite {vd['record']['name']!r} deletion not confirmed ({verdict})",
        {"endpoint": f"GET {endpoint}", "verified_absent": absent, "verdict": verdict},
    )


_REWRITE_CHECKS = {
    "rewrite_list": _rewrite_list_check,
    "rewrite_add": _rewrite_add_check,
    "rewrite_delete": _rewrite_delete_check,
}


async def _verify_rewrite_steps(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """rewrite_list / rewrite_add / rewrite_delete."""
    if step.verify not in _REWRITE_VERIFIERS:
        return _unknown_verifier(step.verify)
    return await _REWRITE_CHECKS[step.verify](m, f"{_profile_base(pid)}/rewrites", vd)


def _analytics_params(metric: str, series: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"from": "-1d", "to": "now"}
    if series:
        params["interval"] = 3600
    if metric == "destinations":
        params["type"] = "countries"
    return params


async def _verify_analytics_step(
    m: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """analytics (aggregate + series)."""
    base_profile = _profile_base(pid)
    metric = vd["metric"]
    suffix = ";series" if vd.get("series") else ""
    params = _analytics_params(metric, bool(vd.get("series")))
    rest = await _get(m, f"{base_profile}/analytics/{metric}{suffix}", params=params)
    ok = _rest_ok(rest)
    return _outcome(
        ok,
        f"REST analytics {metric}{suffix} not ok",
        {
            "endpoint": f"GET {base_profile}/analytics/{metric}{suffix}",
            "params": params,
            "rest_status": common._resp_status(rest),
        },
    )


async def _verify_plot_step(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
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


async def _logs_get_check(m: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    base_profile = _profile_base(pid)
    rest = await _get(m, f"{base_profile}/logs", params={"limit": 10})
    ok = _rest_ok(rest)
    return _outcome(
        ok, "REST GET logs not ok", {"endpoint": f"GET {base_profile}/logs", "rest_status": common._resp_status(rest)}
    )


async def _logs_download_check(m: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    log_pid = os.environ.get("NEXTDNS_PLOT_PROFILE") or pid
    rest = await _get(m, f"/profiles/{log_pid}/logs", params={"limit": 10})
    # Log entries are mostly strings (domain, status, timestamps), so an
    # analytics-style numeric-data check would wrongly report "no logs".
    body = rest.get("body") if isinstance(rest, dict) else None
    if not (isinstance(body, dict) and body.get("data")):
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


async def _logs_clear_check(m: Any, pid: str) -> tuple[str, str, str, dict[str, Any]]:
    base_profile = _profile_base(pid)
    rest = await m.rest_delete(f"{base_profile}/logs")
    ok = _rest_ok(rest)
    return _outcome(
        ok,
        "REST DELETE logs not ok",
        {"endpoint": f"DELETE {base_profile}/logs", "rest_status": common._resp_status(rest)},
    )


_LOG_CHECKS = {
    "logs_get": _logs_get_check,
    "logs_download": _logs_download_check,
    "logs_clear": _logs_clear_check,
}


async def _verify_log_steps(
    m: Any, targets: Any, pid: str, step: Any, vd: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """logs_get / logs_download / logs_clear."""
    if step.verify not in _LOG_VERIFIERS:
        return _unknown_verifier(step.verify)
    return await _LOG_CHECKS[step.verify](m, pid)


# The verifier families and their handlers, in dispatch order. Names are matched
# exactly, so a typo reaches the unknown-verifier answer instead of a
# neighbouring branch.
_VERIFIER_FAMILIES = (
    (_PROFILE_VERIFIERS, _verify_profile_steps),
    (_SETTINGS_VERIFIERS, _verify_settings_steps),
    (_LIST_VERIFIERS, _verify_list_steps),
    (_REWRITE_VERIFIERS, _verify_rewrite_steps),
    (_PLOT_VERIFIERS, _verify_plot_step),
    (_LOG_VERIFIERS, _verify_log_steps),
)


async def verify_step(m: Any, targets: Any, pid: str, step: Any, run: Any) -> tuple[str, str, str, dict[str, Any]]:
    """REST-verify one step. Returns (status, error, reason, rest_verification)."""
    vd = step.vdata
    if step.verify == "analytics":
        return await _verify_analytics_step(m, pid, step, vd)
    if step.verify == "tool_ok":
        # DoH resolution leaves no account state to read back, so REST cannot
        # decide this step at all. The server's call log does (see
        # _expected_call_ok): the lookup ran and answered.
        return ("passed", "", "", {"note": "read-only DoH lookup; verdict from the server call log"})
    for names, handler in _VERIFIER_FAMILIES:
        if step.verify in names:
            return await handler(m, targets, pid, step, vd)
    return _unknown_verifier(step.verify)
