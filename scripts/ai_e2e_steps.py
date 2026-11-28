"""The step catalog builder for the AI E2E harness.

The responsibilities of ``build_steps`` (``scripts/ai_e2e_harness.py``) live
here as one small function per family of the checklist — profiles, settings,
lists, rewrites, analytics, DoH, plots, dashboard and logs — with the profile
deletion step as its own helper. ``build_steps`` concatenates them in checklist
order; each helper returns the steps for its family only. The harness module
keeps the thin delegator and the shared primitives (``Step``, ``_expect``,
``_endpoint``, ...), so the catalog is still defined against the same objects.
"""

from __future__ import annotations

import json
from typing import Any

import ai_e2e_common as common
import ai_e2e_harness as harness


def _profile_steps(targets: harness.TaskTargets) -> list[harness.Step]:
    """profiles.create / list / get / update — the bracket around the run."""
    pn = targets.profile_name
    rn = targets.renamed_name
    steps: list[harness.Step] = []
    steps.append(
        harness.Step(
            id="profiles.create",
            section="profiles",
            tool="createProfile",
            description=f"create the test profile {pn!r}",
            prompt=harness._step_prompt(f"Create a new NextDNS profile named exactly '{pn}'."),
            verify="profile_exists",
            # createProfile names no profile: nothing exists yet, so the call
            # cannot be scoped to one.
            expect=harness._expect(harness._endpoint("createProfile"), args=(pn,), profile=harness.ANY_PROFILE),
        )
    )
    steps.append(
        harness.Step(
            id="profiles.list",
            section="profiles",
            tool="listProfiles",
            description="list all profiles and find the new one",
            prompt=harness._step_prompt(f"List all profiles and confirm '{pn}' is present."),
            verify="profile_exists",
            # The answer has to name the profile: a list call that never mentions it
            # did not find it. listProfiles itself carries no profile_id.
            expect=harness._expect(harness._endpoint("listProfiles"), result=(pn,), profile=harness.ANY_PROFILE),
        )
    )
    steps.append(
        harness.Step(
            id="profiles.get",
            section="profiles",
            tool="getProfile",
            description="get the test profile by id",
            prompt=harness._step_prompt("Get the profile '{profile_id}' and report its details."),
            verify="profile_exists",
            expect=harness._expect(harness._endpoint("getProfile"), args=("{profile_id}",), result=("{profile_id}",)),
        )
    )
    steps.append(
        harness.Step(
            id="profiles.update",
            section="profiles",
            tool="updateProfile",
            description=f"rename the test profile to {rn!r}",
            prompt=harness._step_prompt(f"Rename the profile '{{profile_id}}' to exactly '{rn}'."),
            verify="profile_renamed",
            expect=harness._expect(harness._endpoint("updateProfile"), args=(rn,)),
        )
    )
    return steps


def _settings_steps(targets: harness.TaskTargets) -> list[harness.Step]:
    """settings: read + update per category."""
    steps: list[harness.Step] = []
    for cat, spec in harness.SETTINGS_CATEGORIES.items():
        read_tool, write_tool = harness._SETTINGS_OPS[cat]
        steps.append(
            harness.Step(
                id=f"settings.{cat}.read",
                section="settings",
                tool=read_tool,
                description=f"{cat}: read current settings",
                prompt=harness._step_prompt(
                    f"Read the current {cat} settings for profile '{{profile_id}}' and report them."
                ),
                verify="settings_read",
                vdata={"cat": cat, "spec": spec},
                expect=harness._expect(harness._endpoint(read_tool)),
            )
        )
        steps.append(
            harness.Step(
                id=f"settings.{cat}.update",
                section="settings",
                tool=write_tool,
                description=f"{cat}: update to {json.dumps(spec['set'])}",
                prompt=harness._step_prompt(
                    f"Update the {cat} settings for profile '{{profile_id}}' to {json.dumps(spec['set'])}."
                ),
                verify="settings_update",
                vdata={"cat": cat, "spec": spec},
                # Pin the payload, not just the endpoint: the step is about these fields.
                expect=harness._expect(harness._endpoint(write_tool), args=tuple(spec["set"].keys())),
            )
        )
    return steps


def _list_steps(targets: harness.TaskTargets) -> list[harness.Step]:
    """lists: read/replace/add/update/remove per type."""
    tag = targets.tag
    entries = targets.list_entries
    steps: list[harness.Step] = []
    for lt in harness._LIST_SPECS:
        name = lt["name"]
        tools = harness._LIST_TOOLS[name]
        values = [entries[name]]
        added = common._list_added(name, tag)
        steps.append(
            harness.Step(
                id=f"lists.{name}.read",
                section="lists",
                tool=tools["get"],
                description=f"{name}: read current list",
                prompt=harness._step_prompt(f"Read the current {name} for profile '{{profile_id}}'."),
                verify="list_read",
                vdata={"name": name, "spec": lt},
                expect=harness._expect(harness._endpoint(tools["get"])),
            )
        )
        steps.append(
            harness.Step(
                id=f"lists.{name}.replace",
                section="lists",
                tool=tools["replace"],
                description=f"{name}: replace with {values}",
                prompt=harness._step_prompt(
                    f"Replace the {name} for profile '{{profile_id}}' with exactly {json.dumps(values)}."
                ),
                verify="list_replace",
                vdata={"name": name, "spec": lt, "values": values},
                expect=harness._expect(harness._endpoint(tools["replace"]), args=(values[0],)),
            )
        )
        if added is not None:
            steps.append(
                harness.Step(
                    id=f"lists.{name}.add",
                    section="lists",
                    tool=tools["add"],
                    description=f"{name}: add {added!r}",
                    prompt=harness._step_prompt(f"Add '{added}' to the {name} for profile '{{profile_id}}'."),
                    verify="list_add",
                    vdata={"name": name, "spec": lt, "values": values, "added": added},
                    expect=harness._expect(harness._endpoint(tools["add"]), args=(added,)),
                )
            )
        if lt["updatable"]:
            steps.append(
                harness.Step(
                    id=f"lists.{name}.update",
                    section="lists",
                    tool=tools["update"],
                    description=f"{name}: update {values[0]!r} (active=false)",
                    prompt=harness._step_prompt(
                        f"Update the entry '{values[0]}' in the {name} for profile '{{profile_id}}' "
                        f"with {json.dumps({'active': False})}."
                    ),
                    verify="list_update",
                    vdata={"name": name, "spec": lt, "values": values},
                    # Pin the payload, not just the endpoint: the step is about
                    # this entry going inactive. `entry_id` is only knowable
                    # from a prior read (and equals the entry value for these
                    # lists), and the recorded args are the coerced JSON the
                    # server sent upstream, so the false value is matched in
                    # its serialized spelling.
                    expect=harness._expect(
                        harness._endpoint(tools["update"]),
                        args=(values[0], '"active": false'),
                    ),
                )
            )
        steps.append(
            harness.Step(
                id=f"lists.{name}.remove",
                section="lists",
                tool=tools["remove"],
                description=f"{name}: remove {values[0]!r}",
                prompt=harness._step_prompt(f"Remove '{values[0]}' from the {name} for profile '{{profile_id}}'."),
                verify="list_remove",
                vdata={"name": name, "spec": lt, "values": values},
                expect=harness._expect(harness._endpoint(tools["remove"])),
            )
        )
        if added is not None:
            steps.append(
                harness.Step(
                    id=f"lists.{name}.remove_added",
                    section="lists",
                    tool=tools["remove"],
                    description=f"{name}: remove the added entry {added!r}",
                    prompt=harness._step_prompt(f"Remove '{added}' from the {name} for profile '{{profile_id}}'."),
                    verify="list_remove_added",
                    vdata={"name": name, "spec": lt, "values": values, "added": added},
                    expect=harness._expect(harness._endpoint(tools["remove"])),
                )
            )
    return steps


def _rewrite_steps(targets: harness.TaskTargets) -> list[harness.Step]:
    """rewrites: list, add each of 3, delete each."""
    tag = targets.tag
    steps: list[harness.Step] = []
    steps.append(
        harness.Step(
            id="rewrites.list",
            section="rewrites",
            tool="listRewrites",
            description="list existing rewrites",
            prompt=harness._step_prompt("List the DNS rewrite records for profile '{profile_id}'."),
            verify="rewrite_list",
            expect=harness._expect(harness._endpoint("listRewrites")),
        )
    )
    rewrite_records = [
        {"name": targets.rewrite_name, "content": targets.rewrite_content},
        {"name": f"ai-e2e-aaaa-{tag}.example.com", "content": "2001:db8::dead"},
        {"name": f"ai-e2e-cname-{tag}.example.com", "content": f"ai-e2e-target-{tag}.example.com"},
    ]
    for rec in rewrite_records:
        steps.append(
            harness.Step(
                id=f"rewrites.add.{rec['name'].split('-')[2]}",
                section="rewrites",
                tool="addRewrite",
                description=f"add rewrite {rec['name']!r} -> {rec['content']!r}",
                # Phrased as a mapping, not "A -> B": the small model read the arrow
                # as a sequence of two actions and reported having "added and then
                # deleted" the record - it undid its own successful write.
                prompt=harness._step_prompt(
                    f"Add a DNS rewrite record for profile '{{profile_id}}' so that the hostname "
                    f"'{rec['name']}' resolves to '{rec['content']}'."
                ),
                verify="rewrite_add",
                vdata={"record": rec},
                expect=harness._expect(harness._endpoint("addRewrite"), args=(rec["name"], rec["content"])),
            )
        )
    for rec in rewrite_records:
        steps.append(
            harness.Step(
                id=f"rewrites.delete.{rec['name'].split('-')[2]}",
                section="rewrites",
                tool="deleteRewrite",
                description=f"delete rewrite {rec['name']!r}",
                prompt=harness._step_prompt(
                    f"Delete the DNS rewrite record '{rec['name']}' from profile '{{profile_id}}'."
                ),
                verify="rewrite_delete",
                vdata={"record": rec},
                # The entry id is only knowable from a prior list, so the step
                # pins the record by name; the id the server answered with for
                # that name counts as the same identifier (_identifiers_from_answers).
                expect=harness._expect(harness._endpoint("deleteRewrite"), args=(rec["name"],)),
            )
        )
    return steps


def _analytics_steps() -> list[harness.Step]:
    """analytics: aggregate + series per metric."""
    steps: list[harness.Step] = []
    for m in harness.AGGREGATE_METRICS:
        vdata: dict[str, Any] = {"metric": m}
        instr = f"Query aggregate analytics for profile '{{profile_id}}' for metric '{m}' over the last day"
        args: tuple[str, ...] = ()
        if m == "destinations":
            instr += ", restricted to type 'countries'"
            args = ("countries",)
        steps.append(
            harness.Step(
                id=f"analytics.aggregate.{m}",
                section="analytics",
                tool=harness._ANALYTICS_TOOLS[m],
                description=f"aggregate analytics for metric {m!r}",
                prompt=harness._step_prompt(instr + "."),
                verify="analytics",
                vdata=vdata,
                expect=harness._expect(harness._endpoint(harness._ANALYTICS_TOOLS[m]), args=args),
            )
        )
    for m in harness.SERIES_METRICS:
        series_tool = harness._ANALYTICS_TOOLS[m] + "Series"
        vdata_s: dict[str, Any] = {"metric": m, "series": True}
        instr = (
            f"Query the time series for profile '{{profile_id}}' for metric '{m}' "
            "over the last day (from -1d, interval 3600)"
        )
        args_s: tuple[str, ...] = ()
        if m == "destinations":
            instr += ", restricted to type 'countries'"
            args_s = ("countries",)
        steps.append(
            harness.Step(
                id=f"analytics.series.{m}",
                section="analytics",
                tool=series_tool,
                description=f"time-series analytics for metric {m!r}",
                prompt=harness._step_prompt(instr + "."),
                verify="analytics",
                vdata=vdata_s,
                expect=harness._expect(harness._endpoint(series_tool), args=args_s),
            )
        )
    return steps


def _doh_step() -> list[harness.Step]:
    """doh: the single DoH lookup step."""
    return [
        harness.Step(
            id="doh.lookup",
            section="doh",
            tool="dohLookup",
            description="dohLookup example.com through the test profile",
            prompt=harness._step_prompt(
                "Perform a DNS-over-HTTPS lookup for example.com (A record) through profile '{profile_id}'."
            ),
            verify="tool_ok",
            # No account state to read back; the server's own record that the
            # lookup ran and answered with example.com is the whole verdict.
            expect=harness._expect_local("dohLookup", args=("example.com",), result=("example.com",)),
        )
    ]


def _plot_steps() -> list[harness.Step]:
    """plots: every metric."""
    steps: list[harness.Step] = []
    for m in harness.PLOT_METRICS:
        steps.append(
            harness.Step(
                id=f"plots.{m}",
                section="plots",
                tool="plotAnalytics",
                description=f"plotAnalytics {m} (chart)",
                prompt=harness._step_prompt(
                    f"Generate an analytics plot for profile '{{profile_id}}' for metric '{m}' over the last day (interactive chart)."
                ),
                verify="plots",
                vdata={"metric": m},
                # The chart is the payload: the record has to say it answered
                # with chart content, not just that the tool ran. The step runs
                # against the read-only data profile (falls back to the test
                # profile when no plot profile is configured).
                expect=harness._expect_local(
                    "plotAnalytics", args=(m,), result=("AreaChart",), profile=harness.PLOT_PROFILE
                ),
            )
        )
    return steps


# The prompt table for the dashboard steps: (step id, section, task prompt).
# "all" is an aggregate section exercised via the individual widgets; no
# dedicated prompt step is required for it.
_DASHBOARD_SPECS = (
    (
        "dashboard.overview",
        "overview",
        "Show me an overview dashboard for profile '{profile_id}'.",
    ),
    (
        "dashboard.kpi",
        "kpi",
        "Show me the top KPI metrics dashboard widget (queries, blocked queries, block rate) for profile '{profile_id}'.",
    ),
    (
        "dashboard.domains",
        "domains",
        "Show me the top resolved domains dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.blocked_domains",
        "blocked_domains",
        "Show me the top blocked domains dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.reasons",
        "reasons",
        "Show me the block reasons dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.devices",
        "devices",
        "Show me the device analytics dashboard widget (device models and traffic share) for profile '{profile_id}'.",
    ),
    (
        "dashboard.ips",
        "ips",
        "Show me the client IP addresses and ISP details dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.root_domains",
        "root_domains",
        "Show me the top root domains dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.countries",
        "countries",
        "Show me the country traffic dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.gafam",
        "gafam",
        "Show me the GAFAM traffic dominance dashboard widget for profile '{profile_id}'.",
    ),
    (
        "dashboard.security",
        "security",
        "Show me the security analytics dashboard widget (Encrypted DNS and DNSSEC rates) for profile '{profile_id}'.",
    ),
)
_DASHBOARD_EXCLUDED = {"all"}


def _assert_dashboard_table_complete() -> None:
    """The prompt table must cover every dashboard section but the excluded one."""
    from nextdns_mcp.tools.dashboard import DASHBOARD_SECTIONS

    covered = {spec[1] for spec in _DASHBOARD_SPECS}
    assert covered.isdisjoint(_DASHBOARD_EXCLUDED), "excluded section also has a prompt step"
    assert covered | _DASHBOARD_EXCLUDED == set(DASHBOARD_SECTIONS), (
        "dashboard prompt table drift: "
        f"missing={set(DASHBOARD_SECTIONS) - covered - _DASHBOARD_EXCLUDED}, "
        f"extra={covered - set(DASHBOARD_SECTIONS)}"
    )


def _dashboard_steps() -> list[harness.Step]:
    """dashboard: overview and specialized widgets."""
    _assert_dashboard_table_complete()
    steps: list[harness.Step] = []
    for step_id, section, task_prompt in _DASHBOARD_SPECS:
        expect_args = () if section == "overview" else (section,)
        steps.append(
            harness.Step(
                id=step_id,
                section="dashboard",
                tool="overviewDashboard",
                description=f"overviewDashboard (section={section})",
                prompt=harness._step_prompt(task_prompt),
                verify="dashboard",
                expect=harness._expect_local(
                    "overviewDashboard",
                    args=expect_args,
                    result=("$prefab",),
                    profile=harness.PLOT_PROFILE,
                ),
            )
        )
    return steps


def _log_steps() -> list[harness.Step]:
    """logs: get, download, clear."""
    steps: list[harness.Step] = []
    steps.append(
        harness.Step(
            id="logs.get",
            section="logs",
            tool="getLogs",
            description="get recent query logs",
            prompt=harness._step_prompt("Retrieve the 10 most recent query logs for profile '{profile_id}'."),
            verify="logs_get",
            expect=harness._expect(harness._endpoint("getLogs")),
        )
    )
    steps.append(
        harness.Step(
            id="logs.download",
            section="logs",
            tool="downloadLogs",
            description="download retained logs (CSV)",
            prompt=harness._step_prompt("Download the retained query logs for profile '{profile_id}' as CSV."),
            verify="logs_download",
            # The tool saves the CSV and answers with its local path; naming
            # that key proves the download itself ran, not just the redirect.
            # The step reads the data-bearing profile, same as the plot steps.
            expect=harness._expect(
                harness._endpoint("downloadLogs"), result=("file_path",), profile=harness.PLOT_PROFILE
            ),
        )
    )
    steps.append(
        harness.Step(
            id="logs.clear",
            section="logs",
            tool="clearLogs",
            description="clear query logs",
            prompt=harness._step_prompt("Clear the query logs for profile '{profile_id}'."),
            verify="logs_clear",
            expect=harness._expect(harness._endpoint("clearLogs")),
        )
    )
    return steps


def _delete_step(targets: harness.TaskTargets) -> list[harness.Step]:
    """profiles.delete — the model performs the delete."""
    return [
        harness.Step(
            id="profiles.delete",
            section="profiles",
            tool="deleteProfile",
            description=f"delete the test profile {targets.profile_name!r}",
            prompt=harness._step_prompt("Delete the profile '{profile_id}'."),
            verify="profile_gone",
            expect=harness._expect(harness._endpoint("deleteProfile"), args=("{profile_id}",)),
        )
    ]


def build_steps(targets: harness.TaskTargets) -> list[harness.Step]:
    """Build the full per-step checklist (~89 steps) for this run.

    Each step names the published tool that performs it and is judged on that
    tool's endpoint, resolved from the shipped OpenAPI document (see
    ``harness._endpoint``); the three composite operations the server performs itself name
    themselves through ``harness._expect_local``. The actor decides for itself which
    tool to find and call - the step states the outcome it must produce, not
    the call to make.
    """
    steps: list[harness.Step] = []
    steps += _profile_steps(targets)
    steps += _settings_steps(targets)
    steps += _list_steps(targets)
    steps += _rewrite_steps(targets)
    steps += _analytics_steps()
    steps += _doh_step()
    steps += _plot_steps()
    steps += _dashboard_steps()
    steps += _log_steps()
    steps += _delete_step(targets)
    return steps
