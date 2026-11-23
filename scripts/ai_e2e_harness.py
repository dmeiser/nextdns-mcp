#!/usr/bin/env python3
"""MCP-native, API-verified E2E harness for the NextDNS MCP server.

SPDX-License-Identifier: MIT

This script is the E2E validation harness for the NextDNS MCP server. It has a
deliberate two-halves shape:

* **The actor is an MCP-native LLM harness.** The script sends a task prompt
  into an AI coding harness that connects to the NextDNS MCP server *natively
  over stdio* (the repo's own ``nextdns_mcp.server``) and does the tool calling
  itself: it decides which tool to call, with which arguments, and in
  which order. The default harness is ``opencode`` (proven, built-in MCP
  support); the harness is a **configurable command template**, so any harness
  that can mount the MCP server may substitute it. The script does **not**
  drive the MCP tools itself, and it does **not** act through a REST
  side-channel — the MCP path is the only actor path.
* **The script measures.** After the harness finishes, the script verifies what
  *actually happened* by talking to the NextDNS REST API directly with the API
  key — independently of the MCP path. It checks the resulting server state
  (profile provisioned + renamed, settings values, list entries, rewrite
  record) against the fixed targets embedded in the task, then owns the
  cleanup: it deletes the test profile via REST and verifies the deletion
  (404 read-back). The verdict is a PASS/FAIL/SKIP report in JSONL.

How the actor gets the tools
-----------------------------
The default ``opencode`` harness reads ``opencode.json`` from its working
directory. The harness generates that file for each run with an ``mcp.servers``
entry that launches the in-tree MCP server over stdio
(``<python> -m nextdns_mcp.server`` with ``NEXTDNS_API_KEY`` in its
environment). The actor process therefore connects to the MCP server the same
way any MCP client does — the harness is an ordinary MCP client, not a special
case.

Configuration
-------------
The harness command is a template with placeholders, explicit and overridable:

* ``--config PATH`` — a JSON file describing the run. It names ``command``
  (the harness argv template, default ``opencode``), ``model``, ``prompt``
  (optional override of the built-in task), ``workdir``, ``timeout_s``, and
  ``server`` (the MCP server argv + env).
* ``--harness-cmd "opencode run --standalone ... --model ollama-cloud/x"`` —
  shorthand to override just the harness command line.
* ``--model NAME`` — shorthand for the ``{model}`` placeholder.
* Environment: ``NEXTDNS_API_KEY`` (required for a live run),
  ``NEXTDNS_API_BASE``, ``NEXTDNS_E2E_HARNESS_CMD``, ``NEXTDNS_E2E_MODEL``,
  ``NEXTDNS_MCP_PYTHON`` / ``NEXTDNS_MCP_ARGS`` / ``NEXTDNS_MCP_CWD`` (how the
  MCP server is launched; defaults to the in-tree package via the running
  interpreter plus a ``PYTHONPATH`` at ``<repo>/src``).

Placeholders available in the command template: ``{prompt}`` (the task prompt),
``{model}`` (empty by default; a ``--flag`` whose value renders empty is
dropped), ``{workdir}`` (the directory the harness is launched in),
``{config_file}`` (the generated ``opencode.json`` path), ``{server_command}``
(the MCP server argv as a single string), ``{server_env}`` (the MCP server env
as a JSON object).

Any substituted harness must mount the MCP server itself (that is what makes it
MCP-native). The default template relies on opencode reading the generated
``opencode.json`` from the working directory. A second harness (Claude Code,
which takes its MCP servers from a ``--mcp-config`` JSON file in the
``mcpServers`` schema) is documented in ``scripts/README.md``.

Usage::

    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py
    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --config my_run.json
    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --model ollama-cloud/kimi-k2.7-code
    uv run python scripts/ai_e2e_harness.py            # -> SKIP (no key)

The JSONL report defaults to ``artifacts/ai_e2e_report.jsonl`` (``--report`` to
override, ``-`` for stdout).

Client mode
-----------
``--codemode on|off`` (default ``off``) controls whether opencode mounts this
server through Code Mode. Code Mode is opencode's own default: the model's only
native tool is ``execute`` and this server's tools are JavaScript bindings inside
it, which puts a client-side JS evaluation between the model and the contract —
a body written ``async () => { ... }`` evaluates to a function, calls nothing,
and the client reports it as a successful call returning ``null``. With it off,
the tools register natively as ``nextdns_<tool>``; under progressive disclosure
that is ``nextdns_search_tools`` and ``nextdns_call_tool``, so the same contract
is measured without that layer.

The tool contract
-----------------
The server publishes one tool per operation of the shipped OpenAPI document
(``addToDenylist``, ``getAnalyticsDevicesSeries``, …), each taking that
endpoint's own arguments, plus the three composite operations it performs itself —
``plotAnalytics``, ``dohLookup``, and ``overviewDashboard`` — which no endpoint describes. Progressive
disclosure is what the run configures with ``--tool-search``: with it on,
``tools/list`` exposes only ``search_tools`` and ``call_tool``, and the catalog
behind them is the full operation set.

Condition 1 below is therefore matched on the API endpoint the call executed
(``POST /profiles/{profile_id}/denylist``): each published tool is bound to
one, and the server's call log records it. ``plotAnalytics`` and ``dohLookup``
have no endpoint; their pass rule is their own record instead — the call ran,
answered, and (for the chart) came back as structured Prefab chart output.

Targeted runs
-------------
``--print-steps`` lists the checklist (ids plus 1-based positions) on stdout;
``--steps`` then runs a comma-separated selection of it — step ids, positions,
or inclusive ``A-B`` ranges::

    uv run python scripts/ai_e2e_harness.py --print-steps
    uv run python scripts/ai_e2e_harness.py --steps 20-21,lists.privacy_natives.replace-36,profiles.delete

``--start-step``/``--end-step`` are the single-range shorthand for the same
selection. Progress keeps the full-run numbering (``[31/89]``), so a targeted
report can be compared with a full one step by step. A selection that leaves out
``profiles.create`` or ``profiles.delete`` makes the harness do that part of the
profile lifecycle itself, through the MCP ``createProfile``/``deleteProfile``
tools (so the server's ACLs apply); everything else is the caller's
responsibility — if the selection tests "remove X", include the step that adds
X.

How each step is judged
-----------------------
Three conditions, all of which must hold:

1. The MCP server recorded a call to the **endpoint this step verifies** — and
   that call succeeded and answered with a payload. Each published tool is
   bound to one endpoint, so this names the API call the step is about no
   matter which tool the actor found; the three composite operations are named by
   their own record (see ``_expect``). The server writes what it executed to a
   JSONL call log (``NEXTDNS_CALL_LOG``, see ``nextdns_mcp.call_log``); the
   harness reads back the lines its own attempt appended. ``--print-steps``
   prints each step's expectation.
2. **No state-changing call followed it** in the same attempt. A confirming
   read afterwards is normal; a later write can undo the step's own work.
3. The **REST end state** is correct, read back over the raw API.

REST alone cannot decide a step. It proves the state, not who produced it: a
step asking for ``getProfile`` passed a run whose transcript was nothing but
errors, because an earlier step had created the profile. For a read there is
nothing to read back at all — the account looks the same whether or not the
read happened — so the server's own record, including the payload it answered
with, is the only evidence there is.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv as _load_dotenv
except (ImportError, OSError):  # pragma: no cover - optional dependency
    _load_dotenv = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# Make the in-tree ``nextdns_mcp`` package importable when running as a script
# (``uv run python scripts/ai_e2e_harness.py``) without needing an install.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
# The pure helpers live in a sibling module (shared with the unit tests).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_e2e_common as common

# Re-export the pure model, constants, and helpers so existing imports and the
# README keep working against this module (they now live in ``ai_e2e_common``).
# ``api_base`` is a function on purpose: the API base has to be read after main()
# has loaded the dotenv file, not when this module was imported.
api_base = common.api_base
SECTIONS = common.SECTIONS
SETTINGS_CATEGORIES = common.SETTINGS_CATEGORIES
AGGREGATE_METRICS = common.AGGREGATE_METRICS
SERIES_METRICS = common.SERIES_METRICS
PLOT_METRICS = common.PLOT_METRICS
LIST_ENTRIES = common.LIST_ENTRIES
_LIST_SPECS = common._LIST_SPECS
CheckResult = common.CheckResult
Report = common.Report
check_from_call = common.check_from_call
# Private helpers (re-exported for the unit tests, which import by file path).
_extract_tool_error = common._extract_tool_error
_structured = common._structured
_is_error_body = common._is_error_body
_error_body_text = common._error_body_text
_is_skip = common._is_skip
_is_not_supported = common._is_not_supported
_skip_reason = common._skip_reason
_not_supported_reason = common._not_supported_reason
_is_raised_error = common._is_raised_error
_raised_error_text = common._raised_error_text
_resp_status = common._resp_status
_verify_settings_assert = common._verify_settings_assert
_list_values = common._list_values
_list_added = common._list_added
_list_contains = common._list_contains
_list_absent = common._list_absent  # (absent, verdict) — a failed read is not absence
_rewrites_absent = common._rewrites_absent
_entry_active = common._entry_active
_unwrap_id = common._unwrap_id
# The MCP content-block readers: the harness reads text out of the payloads its
# actor prints, the container runner reads it out of live tool results, and both
# go through this one. See ``ai_e2e_common`` for the shape rules.
_content_texts = common._content_texts
ABSENT = common.ABSENT
PRESENT = common.PRESENT
INDETERMINATE = common.INDETERMINATE
ENTRY_FOUND = common.ENTRY_FOUND
ENTRY_MISSING = common.ENTRY_MISSING
_entry_ids = common._entry_ids
_rewrites_contains = common._rewrites_contains
_analytics_has_data = common._analytics_has_data
_random_tag = common._random_tag
make_profile_name = common.make_profile_name


# =========================================================================== #
# The harness's responsibilities live in focused sibling modules, each importing
# ai_e2e_common (and its own collaborators) directly. This entry re-exports every
# name so existing import paths -- and the unit tests, which load this file by
# path and monkeypatch these names -- keep working unchanged, and so main's
# ai_e2e_steps / ai_e2e_verify / ai_e2e_run / ai_e2e_cli siblings keep resolving
# their primitives through ``harness.X``.
# =========================================================================== #
from ai_e2e_actor import (
    ActorRun,
    ToolCall,
    _collect_streams,
    _flatten_text,
    _kill_process_group,
    _suppress,
    _tool_event_name,
    parse_actor_events,
    run_actor,
)
from ai_e2e_checklist import (
    _ANALYTICS_TOOLS,
    _LIST_TOOLS,
    _SETTINGS_OPS,
    ANY_PROFILE,
    CALL_LOG_NAME,
    PLOT_PROFILE,
    PROFILE_CREATE_STEP,
    PROFILE_DELETE_STEP,
    Step,
    StepRangeError,
    _answered,
    _call_label,
    _endpoint,
    _entry_bounds,
    _expect,
    _expect_label,
    _expect_local,
    _expected_call_ok,
    _identifiers_from_answers,
    _read_new_calls,
    _server_calls_text,
    _spec_endpoints,
    _step_position,
    _step_prompt,
    _walk_identifiers,
    needs_harness_setup,
    needs_harness_teardown,
    render_step_catalog,
    select_steps,
)
from ai_e2e_config import (
    _BUILTIN_TOOLS,
    _ENV_REF,
    _MAX_PROFILE_PAGES,
    DEFAULT_HARNESS_COMMAND,
    DEFAULT_SERVER,
    ENV_REF_API_KEY,
    PLACEHOLDER_CONFIG_FILE,
    PLACEHOLDER_MODEL,
    PLACEHOLDER_PROMPT,
    PLACEHOLDER_SERVER_COMMAND,
    PLACEHOLDER_SERVER_ENV,
    PLACEHOLDER_WORKDIR,
    HarnessAbortError,
    HarnessConfig,
    _as_argv,
    _expand_env_refs,
    _known_tools,
    _profile_list_cursor,
    _profile_protection_probe,
    _protect_server_profile,
    _redacted_server_env,
    _validated_profile_page,
    allowed_tools,
    render_argv,
    write_mcp_config,
)
from ai_e2e_feedback import (
    _CODE_MODE_TOOL_PATH_MARKERS,
    _LAZY_REGISTRATION_MARKERS,
    _PROVIDER_OUTAGE_MARKERS,
    _PROVIDER_STREAM_MARKERS,
    PROVIDER_OUTAGE_STEPS,
    TRANSIENT_ATTEMPTS,
    _actor_read_the_instrument,
    _client_reported_errors,
    _extract_error_events,
    _feedback_block,
    _provider_outage,
    _summarize_calls,
    _transient_actor_failure,
)
from ai_e2e_measure import (
    MeasureClient,
    _all_profile_ids,
    _created_ids_from_records,
    _deletable_by_this_run,
    _ensure_plot_profile,
    _harness_delete_profile,
    _harness_provision_profile,
    _mcp_call,
    _never_delete_ids,
    _profile_gone,
    _profile_lifecycle_call,
    _progress,
    _proven_created_id,
    _self_heal_profile,
    _sweep_stray_profiles,
)
from ai_e2e_reporting import (
    _EXPECTED_CALL_MODES,
    _SCHEMA_REJECTION,
    _UPSTREAM_STATUS,
    _failure_modes,
    render_text_report,
    summarize_reports,
    write_report,
    write_skip_report,
)
from ai_e2e_task import (
    TaskTargets,
)

__all__ = [
    "ANY_PROFILE",
    "CALL_LOG_NAME",
    "DEFAULT_HARNESS_COMMAND",
    "DEFAULT_SERVER",
    "ENV_REF_API_KEY",
    "PLACEHOLDER_CONFIG_FILE",
    "PLACEHOLDER_MODEL",
    "PLACEHOLDER_PROMPT",
    "PLACEHOLDER_SERVER_COMMAND",
    "PLACEHOLDER_SERVER_ENV",
    "PLACEHOLDER_WORKDIR",
    "PLOT_PROFILE",
    "PROFILE_CREATE_STEP",
    "PROFILE_DELETE_STEP",
    "PROVIDER_OUTAGE_STEPS",
    "TRANSIENT_ATTEMPTS",
    "_ANALYTICS_TOOLS",
    "_BUILTIN_TOOLS",
    "_CODE_MODE_TOOL_PATH_MARKERS",
    "_ENV_REF",
    "_EXPECTED_CALL_MODES",
    "_LAZY_REGISTRATION_MARKERS",
    "_LIST_TOOLS",
    "_MAX_PROFILE_PAGES",
    "_PROVIDER_OUTAGE_MARKERS",
    "_PROVIDER_STREAM_MARKERS",
    "_SCHEMA_REJECTION",
    "_SETTINGS_OPS",
    "_UPSTREAM_STATUS",
    "ActorRun",
    "HarnessAbortError",
    "HarnessConfig",
    "MeasureClient",
    "Step",
    "StepRangeError",
    "TaskTargets",
    "ToolCall",
    "_actor_read_the_instrument",
    "_all_profile_ids",
    "_answered",
    "_as_argv",
    "_call_label",
    "_client_reported_errors",
    "_collect_streams",
    "_created_ids_from_records",
    "_deletable_by_this_run",
    "_endpoint",
    "_ensure_plot_profile",
    "_entry_bounds",
    "_expand_env_refs",
    "_expect",
    "_expect_label",
    "_expect_local",
    "_expected_call_ok",
    "_extract_error_events",
    "_failure_modes",
    "_feedback_block",
    "_flatten_text",
    "_harness_delete_profile",
    "_harness_provision_profile",
    "_identifiers_from_answers",
    "_kill_process_group",
    "_known_tools",
    "_mcp_call",
    "_never_delete_ids",
    "_profile_gone",
    "_profile_lifecycle_call",
    "_profile_list_cursor",
    "_profile_protection_probe",
    "_progress",
    "_protect_server_profile",
    "_proven_created_id",
    "_provider_outage",
    "_read_new_calls",
    "_redacted_server_env",
    "_self_heal_profile",
    "_server_calls_text",
    "_spec_endpoints",
    "_step_position",
    "_step_prompt",
    "_summarize_calls",
    "_suppress",
    "_sweep_stray_profiles",
    "_tool_event_name",
    "_transient_actor_failure",
    "_validated_profile_page",
    "_walk_identifiers",
    "allowed_tools",
    "needs_harness_setup",
    "needs_harness_teardown",
    "parse_actor_events",
    "render_argv",
    "render_step_catalog",
    "render_text_report",
    "run_actor",
    "select_steps",
    "summarize_reports",
    "write_mcp_config",
    "write_report",
    "write_skip_report",
]


def build_steps(targets: TaskTargets) -> list[Step]:
    """Thin delegator: the step catalog lives in ``ai_e2e_steps``."""
    return _steps.build_steps(targets)


async def _verify_step(
    m: MeasureClient, targets: TaskTargets, pid: str, step: Step, run: ActorRun
) -> tuple[str, str, str, dict[str, Any]]:
    """Thin delegator: the REST verifiers live in ``ai_e2e_verify``."""
    return await _verify.verify_step(m, targets, pid, step, run)


def _prepare_scratch_dir() -> Path:
    """Give the run a TMPDIR off the tmpfs quota, removed when the process ends.

    Set before any actor (and therefore every per-step MCP server, which the
    actor spawns and hands its environment to) can create a temp file: the
    downloadLogs CSV lands here, on the root filesystem, instead of filling
    the /tmp user quota. atexit removes the directory so runs do not
    accumulate.

    This used to also sweep every ``nextdns_logs_*`` directory in the system
    temp at startup. That was removed: a downloadLogs in flight from ANOTHER
    server instance (the product keeps the CSV by design, and hands the path
    to its caller) had its directory deleted mid-download. The sweep would
    have destroyed files the harness never created, to reclaim space a
    per-run scratch dir already reclaims at exit.
    """
    scratch = Path.home() / ".cache" / "nextdns-mcp-e2e" / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(scratch)
    # tempfile resolves and caches its directory at the first use. Priming
    # the cache here keeps the workdir's mkdtemp inside the scratch even
    # when an earlier tempfile call in this process already pinned the
    # ambient temp dir.
    tempfile.tempdir = str(scratch)
    atexit.register(partial(shutil.rmtree, scratch, ignore_errors=True))
    return scratch


# Serializes run_harness invocations within one process. The run mutates
# process globals (TMPDIR, tempfile.tempdir) for its whole lifetime; two
# concurrent in-process runs raced on them, and one run's tempfile calls
# landed in the other run's scratch, which that run then deleted. An asyncio
# lock: the realistic in-process concurrency is tasks on one event loop, and
# a threading lock cannot serialize that (its ownership is per-thread, so a
# second task on the same thread would sail through). A run on a DIFFERENT
# loop/thread is refused loudly rather than silently interleaving.
_RUN_HARNESS_LOCK = asyncio.Lock()


async def run_harness(api_key: str, config: HarnessConfig) -> Report:
    """Run the harness; in-process runs serialize on the scratch-dir globals."""
    async with _RUN_HARNESS_LOCK:
        return await _run_harness_scoped(api_key, config)


async def _run_harness_scoped(api_key: str, config: HarnessConfig) -> Report:
    """Thin delegator: the run orchestration lives in ``ai_e2e_run``."""
    return await _run.run_harness_scoped(api_key, config)


def main(argv: list[str] | None = None) -> int:
    """Thin delegator: the CLI lives in ``ai_e2e_cli``."""
    return _cli.main(argv)


# The extracted responsibilities of the four long functions (build_steps,
# _verify_step, _run_harness_scoped, main) live in sibling modules; the harness
# module keeps the thin delegators above and the shared primitives they name.
# Importing them here — after the shared names are defined — avoids a circular
# import: each module imports the harness's primitives from it.
# Run as a script, this module is `__main__`, so those siblings would import a
# SECOND copy of this 2,600-line file under its real name — two copies of every
# constant, table and cache, and monkeypatches aimed at `ai_e2e_harness` that
# the siblings would never see. Register this copy under its real name first.
if __name__ == "__main__":
    sys.modules.setdefault("ai_e2e_harness", sys.modules[__name__])
import ai_e2e_cli as _cli
import ai_e2e_run as _run
import ai_e2e_steps as _steps
import ai_e2e_verify as _verify

if __name__ == "__main__":
    raise SystemExit(main())
