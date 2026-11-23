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
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Self

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
# Harness (actor) configuration
# =========================================================================== #


# The tool catalog the actor is measured against: one tool per operation of the
# shipped OpenAPI document, plus the three composite operations the server performs itself.
# Read from the server rather than typed out here - the document is the
# contract, and a list kept beside it would drift the moment an operation is
# added to it.
@lru_cache(maxsize=1)
def allowed_tools() -> frozenset[str]:
    from nextdns_mcp.server import published_tool_names

    return frozenset(published_tool_names())


# Harness-side tools the actor may legitimately use (discovery, sandbox);
# attempts outside these and the catalog are flagged in verbose feedback.
_BUILTIN_TOOLS = frozenset(
    {"execute", "glob", "read", "write", "edit", "bash", "search", "websearch", "patch", "search_tools", "call_tool"}
)


def _known_tools() -> frozenset[str]:
    """Catalog plus the harness-side tool names."""
    return allowed_tools() | _BUILTIN_TOOLS


# Default way to launch the in-tree MCP server (overridden by config/env).
# Uses the running interpreter (``sys.executable``) plus a PYTHONPATH pointing
# at the in-tree package, so it works no matter how the harness itself was
# launched (``uv run python scripts/...`` from the repo root).
DEFAULT_SERVER = {
    "command": sys.executable,
    "args": ["-m", "nextdns_mcp.server"],
    "cwd": str(_REPO_ROOT),
    "env": {"PYTHONPATH": str(_REPO_ROOT / "src"), "MCP_TRANSPORT": "stdio"},
}

# The default harness command template. ``opencode`` is the documented default
# because it has native, built-in MCP support: it reads ``opencode.json`` from
# its working directory, launches the declared local MCP server over stdio, and
# exposes its tools to the model. The task prompt and (optional) model are
# substituted from the ``{prompt}`` / ``{model}`` placeholders.
DEFAULT_HARNESS_COMMAND = [
    "opencode",
    "run",
    "--standalone",
    "--format",
    "json",
    "--auto",
    "--model",
    "{model}",
    "--title",
    "nextdns-e2e",
    "{prompt}",
]

# Placeholders understood in the harness command template.
PLACEHOLDER_PROMPT = "{prompt}"
PLACEHOLDER_MODEL = "{model}"
PLACEHOLDER_WORKDIR = "{workdir}"
PLACEHOLDER_CONFIG_FILE = "{config_file}"
PLACEHOLDER_SERVER_COMMAND = "{server_command}"
PLACEHOLDER_SERVER_ENV = "{server_env}"

# How the API key reaches the MCP server: as a reference the actor's own config
# loader expands, never as the value. The key is in the actor process's environment
# already (the harness starts it from this process's environment), so inlining it
# into the server env — which is serialized into the ``{server_env}`` placeholder,
# the actor's argv, and the run report — only ever added a way for it to be read off
# ``ps`` or a logged command line.
ENV_REF_API_KEY = "{env:NEXTDNS_API_KEY}"
_ENV_REF = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(env: dict[str, Any]) -> dict[str, str]:
    """Expand ``{env:NAME}`` references against this process's environment.

    The config deliberately holds references (see ``ENV_REF_API_KEY``), but the
    lifecycle MCP server the harness launches for its own profile bookkeeping has
    no config loader in front of it to expand them, so it gets the values here.
    """
    expanded = {str(k): str(v) for k, v in (env or {}).items()}
    return {name: _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value) for name, value in expanded.items()}


def _redacted_server_env(server: dict[str, Any]) -> dict[str, str]:
    """The server's environment with the API key reduced to its ``{env:...}`` reference.

    Anything built from the server env that can leave this process — the generated
    client config, the ``{server_env}`` placeholder, the report's command line — goes
    through here, so an ``--config`` file that inlines a real key cannot smuggle it
    into the actor's argv either.
    """
    env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
    if env.get("NEXTDNS_API_KEY"):
        env["NEXTDNS_API_KEY"] = ENV_REF_API_KEY
    return env


@dataclass
class HarnessConfig:
    """Explicit, overridable description of the actor run.

    ``command`` is the harness command template; the harness is whatever MCP
    client it names. ``server`` is the MCP server the actor must mount — the
    actor's tool surface comes from this server, launched over stdio by the
    harness itself (natively), never through the measuring script.
    """

    command: list[str] = field(default_factory=lambda: list(DEFAULT_HARNESS_COMMAND))
    model: str = ""  # empty => let the harness use its configured default
    profile_name: str = ""  # empty => make_profile_name() (fixed name + date)
    prompt: str = ""  # empty => use the built-in task prompt
    workdir: str = ""  # empty => a fresh temporary directory
    server: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_SERVER)))
    timeout_s: float = 7200.0  # overall run budget (99 sequential model steps)
    step_timeout_s: float = 300.0  # per-step actor timeout
    verbose: bool = False  # per-step feedback includes failed/unknown tool attempts
    # opencode mounts MCP servers through Code Mode unless told otherwise: the
    # model's only native tool is `execute`, and this server's tools are JS
    # bindings inside it. False registers them natively as `nextdns_<tool>`.
    # Off by default: a live run lost a step to a code-mode body written as
    # `async () => { ... }`, which evaluates to a function, calls nothing, and
    # is reported by the client as a successful call returning `null`. Native
    # registration removes that layer while leaving progressive disclosure
    # (nextdns_search_tools / nextdns_call_tool) exactly what is measured.
    codemode: bool = False
    # Targeted run: comma-separated step ids, 1-based positions and inclusive
    # A-B ranges (see select_steps). start_step/end_step are the single-range
    # shorthand for the same thing; empty means "the whole checklist".
    steps: str = ""
    start_step: str = ""
    end_step: str = ""

    def step_selection(self, total: int) -> str:
        """The selection string for a checklist of ``total`` steps."""
        parts = [self.steps] if self.steps.strip() else []
        if self.start_step or self.end_step:
            parts.append(f"{self.start_step or 1}-{self.end_step or total}")
        return ",".join(parts)

    @classmethod
    def load(cls, path: str | None) -> HarnessConfig:
        """Build a HarnessConfig from a JSON file (if given) layered over env."""
        cfg = cls()
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if "command" in data:
                cfg.command = _as_argv(data["command"])
            if "model" in data:
                cfg.model = str(data["model"])
            if "prompt" in data:
                cfg.prompt = str(data["prompt"])
            if "profile_name" in data:
                cfg.profile_name = str(data["profile_name"])
            if "workdir" in data:
                cfg.workdir = str(data["workdir"])
            if "timeout_s" in data:
                cfg.timeout_s = float(data["timeout_s"])
            if "step_timeout_s" in data:
                cfg.step_timeout_s = float(data["step_timeout_s"])
            if "start_step" in data:
                cfg.start_step = str(data["start_step"])
            if "end_step" in data:
                cfg.end_step = str(data["end_step"])
            if "steps" in data:
                cfg.steps = str(data["steps"])
            if isinstance(data.get("server"), dict):
                srv = data["server"]
                if "command" in srv:
                    cfg.server["command"] = str(srv["command"])
                if "args" in srv:
                    cfg.server["args"] = [str(a) for a in srv["args"]]
                if "cwd" in srv:
                    cfg.server["cwd"] = str(srv["cwd"])
                if "env" in srv and isinstance(srv["env"], dict):
                    cfg.server["env"] = {str(k): str(v) for k, v in srv["env"].items()}
        # Environment overrides (explicit and documented).
        if cmd := os.environ.get("NEXTDNS_E2E_HARNESS_CMD"):
            cfg.command = _as_argv(cmd)
        if env_model := os.environ.get("NEXTDNS_E2E_MODEL"):
            cfg.model = env_model
        if env_profile := os.environ.get("NEXTDNS_E2E_PROFILE_NAME"):
            cfg.profile_name = env_profile
        if srv_python := os.environ.get("NEXTDNS_MCP_PYTHON"):
            cfg.server["command"] = srv_python
        if srv_args := os.environ.get("NEXTDNS_MCP_ARGS"):
            cfg.server["args"] = srv_args.split()
        if srv_cwd := os.environ.get("NEXTDNS_MCP_CWD"):
            cfg.server["cwd"] = str(srv_cwd)
        # The API key (and its base) are handed to the MCP server via its env;
        # the actor itself never needs the key (the server uses it). The key
        # goes in as a `{env:...}` reference, never the value: this env dict is
        # serialized into the actor's `{server_env}` placeholder and the run
        # report, and the actor's own config loader (and the harness's
        # `_expand_env_refs`) resolve the reference at launch time.
        cfg.server["env"] = dict(cfg.server["env"])
        if os.environ.get("NEXTDNS_API_KEY"):
            cfg.server["env"].setdefault("NEXTDNS_API_KEY", ENV_REF_API_KEY)
        cfg.server["env"].setdefault("NEXTDNS_API_BASE", common.api_base())
        # The MCP server applies its own read/write ACLs from these; without
        # them every tool call is denied ("no profiles are readable").
        for var in (
            "NEXTDNS_READABLE_PROFILES",
            "NEXTDNS_WRITABLE_PROFILES",
            "NEXTDNS_DEFAULT_PROFILE",
            "NEXTDNS_TOOL_SEARCH",
            "NEXTDNS_PROTECTED_PROFILES",
        ):
            if val := os.environ.get(var):
                cfg.server["env"].setdefault(var, val)
        # The data-bearing profile used for plots/log-download steps is only
        # ever read by the actor; protect it against hallucinated writes
        # (e.g. deleteProfile with the wrong id) even under ALL.
        if plot_pid := os.environ.get("NEXTDNS_PLOT_PROFILE"):
            _protect_server_profile(cfg, plot_pid.strip())
        # Protect every profile that already exists on the account, so a model
        # picking a wrong id during a write cannot hit a real profile. The probe
        # FAILS CLOSED: any request or JSON failure aborts the run rather than
        # continuing with an unknown pre-existing profile set (which, under
        # NEXTDNS_WRITABLE_PROFILES=ALL, would leave every real profile
        # model-writable).
        _profile_protection_probe(cfg)
        return cfg

    def render_argv(self, values: dict[str, str]) -> list[str]:
        """Expand the command template's placeholders into a concrete argv."""
        return render_argv(self.command, values)


def _as_argv(value: Any) -> list[str]:
    """Accept either a JSON array or a shell-ish string and return an argv list."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        # Split on whitespace (the harness launches via argv, not a shell, so
        # this is for convenience/documentation only).
        return value.split()
    return list(DEFAULT_HARNESS_COMMAND)


def render_argv(template: list[str], values: dict[str, str]) -> list[str]:
    """Expand placeholders in a command template into a concrete argv.

    A placeholder that renders to the empty string drops its token; a flag
    (a token starting with ``-``) whose value token rendered empty is dropped
    along with it — so e.g. ``["--model", "{model}"]`` vanishes entirely when
    no model is configured, instead of leaving a dangling ``--model``.

    One pass, never one pass per placeholder: a substituted value (the prompt
    is model-authored text) that happens to contain a placeholder token must
    not be re-scanned as a template, or a prompt spelling ``{server_env}``
    would smuggle the server environment into the rendered argv.
    """
    rendered: list[str] = []
    pattern = re.compile("|".join(re.escape(key) for key in values)) if values else None
    for tok in template:
        rendered.append(pattern.sub(lambda m: values[m.group(0)], tok) if pattern else tok)
    argv: list[str] = []
    for i, tok in enumerate(rendered):
        if tok == "":
            continue
        if tok.startswith("-") and i + 1 < len(rendered) and rendered[i + 1] == "":
            continue
        argv.append(tok)
    return argv


# No harness-side prompt of any kind. The harness sends the task and nothing
# else: it does not tell the model how to behave inside a given agent CLI, and
# it does not describe the MCP server on the server's behalf. That guidance is
# the server's to publish, through channels any MCP client sees — the MCP
# ``initialize`` instructions, the tool descriptions, and the usage
# prompt/resource (see ``nextdns_mcp.usage`` and ``tool_search``).
class HarnessAbortError(RuntimeError):
    """The run cannot be made safe, so it must not start (or must not continue).

    Raised by the profile-protection probe when the pre-existing profile set
    cannot be established. Continuing without it would hand the actor an
    ALL-writable server that real profiles are only nominally protected from,
    so the failure is fatal to the run, never a warning line.
    """


# A listing that yields this many cursors without ending is a runaway
# pagination loop (the API hands back fresh unique cursors forever), not a
# real account: unique cursors would otherwise spin the probe forever.
_MAX_PROFILE_PAGES = 10_000


def _profile_protection_probe(cfg: HarnessConfig) -> None:
    """Protect every profile that already exists on the account; FAIL CLOSED.

    The probe's answer is what keeps a wrong-id write from reaching a real
    profile. A probe that "fails open" (logs and continues) leaves the run
    holding NEXTDNS_WRITABLE_PROFILES=ALL with an incomplete protected list —
    the exact misconfiguration this step exists to prevent. So every failure
    mode is fatal: a missing httpx, a transport error, a non-2xx answer, an
    unparseable body, a malformed page (missing/null/non-list ``data``, a row
    that is not an object with an ``id``, a cursor that is present but not a
    non-empty string), a cursor that repeats, or a listing that never ends —
    each raises :class:`HarnessAbortError` and ``main`` reports the aborted
    run with a non-zero exit. A run that never starts cannot write to a
    profile it was never told to protect.
    """
    api_key = os.environ.get("NEXTDNS_API_KEY")
    if not api_key:
        return
    try:
        import httpx as _httpx
    except ImportError as exc:
        raise HarnessAbortError(
            "profile-protection probe requires httpx, which is not importable; "
            "refusing to start a key-bearing run without pre-existing profile protection"
        ) from exc
    # The ProfileList contract paginates, so one GET protects only the first
    # page and every pre-existing profile on later pages stays writable. Follow
    # the cursor to exhaustion; any page's transport/HTTP/JSON/shape failure
    # aborts, the same fail-closed contract the first page is held to.
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        for _page in range(_MAX_PROFILE_PAGES):
            # api_base() validates https before any request is built (see
            # ai_e2e_common.api_base); the ValueError it raises on a plaintext
            # base propagates unchanged — it is the same loud refusal.
            params = {"cursor": cursor} if cursor else {}
            resp = _httpx.get(
                f"{common.api_base()}/profiles", headers={"X-Api-Key": api_key}, params=params, timeout=10.0
            )
            resp.raise_for_status()
            page_rows, cursor = _validated_profile_page(resp.json())
            rows.extend(page_rows)
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise HarnessAbortError(
                    f"profile-protection probe pagination looped (cursor {cursor!r} repeated); refusing to "
                    "run with an unknown pre-existing profile set — set NEXTDNS_PROTECTED_PROFILES "
                    "explicitly or fix the API"
                )
            seen_cursors.add(cursor)
        else:
            raise HarnessAbortError(
                f"profile-protection probe did not exhaust the listing within {_MAX_PROFILE_PAGES} pages; "
                "refusing to run with an unknown pre-existing profile set — set "
                "NEXTDNS_PROTECTED_PROFILES explicitly or fix the API"
            )
    except HarnessAbortError:
        raise
    except Exception as exc:
        raise HarnessAbortError(
            "profile-protection probe failed "
            f"({type(exc).__name__}: {exc}); refusing to run with an unknown pre-existing "
            "profile set — set NEXTDNS_PROTECTED_PROFILES explicitly or fix the API reachability"
        ) from exc
    for row in rows:
        _protect_server_profile(cfg, str(row["id"]))


def _validated_profile_page(payload: object) -> tuple[list[dict[str, Any]], str | None]:
    """One ProfileList page, shape-validated; malformed pages abort the probe.

    A page whose ``data`` is missing, null, or not a list, or whose rows are
    not all objects carrying an ``id``, cannot be trusted to enumerate the
    account's pre-existing profiles: accepting it silently (skipping the odd
    row) would leave whatever it hid writable. Raises :class:`HarnessAbortError`
    on any malformed shape. Returns ``(rows, next_cursor)`` where ``next_cursor``
    is None when the listing ended.
    """
    if not isinstance(payload, dict):
        raise HarnessAbortError(
            f"profile-protection probe got a non-object ProfileList page ({type(payload).__name__}); refusing "
            "to run with an unknown pre-existing profile set — set NEXTDNS_PROTECTED_PROFILES explicitly"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise HarnessAbortError(
            "profile-protection probe got a ProfileList page whose 'data' is missing, null, or not a list; "
            "refusing to run with an unknown pre-existing profile set — set NEXTDNS_PROTECTED_PROFILES explicitly"
        )
    rows: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict) or not row.get("id"):
            raise HarnessAbortError(
                "profile-protection probe got a malformed profile row (not an object with an id); refusing "
                "to run with an unknown pre-existing profile set — set NEXTDNS_PROTECTED_PROFILES explicitly"
            )
        rows.append(row)
    return rows, _profile_list_cursor(payload)


def _profile_list_cursor(payload: dict[str, Any]) -> str | None:
    """The next-page cursor of a ProfileList body, or None when the list ended.

    A cursor that is ABSENT or null ends the listing. A cursor that is present
    but not a non-empty string is malformed: it is refused rather than
    masquerading as the end of the list (a 0 that silently ended pagination
    would leave every later page's profiles writable).
    """
    candidates: list[Any] = [payload.get("cursor")]
    meta = payload.get("meta")
    pagination = meta.get("pagination") if isinstance(meta, dict) else None
    if isinstance(pagination, dict):
        candidates.append(pagination.get("cursor"))
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, str) and value:
            return value
        raise HarnessAbortError(
            f"profile-protection probe got a malformed cursor ({value!r}); refusing to run with an unknown "
            "pre-existing profile set — set NEXTDNS_PROTECTED_PROFILES explicitly"
        )
    return None


def _protect_server_profile(config: HarnessConfig, profile_id: str) -> None:
    """Add ``profile_id`` to the child server's NEXTDNS_PROTECTED_PROFILES.

    The protected set is what stops an actor holding WRITABLE=ALL from
    deleting a profile it was only meant to read. Both paths that pick a
    data-bearing plot/log profile - the explicit env var and the resolved
    fallback - go through here, so neither can select a profile the child
    server leaves writable.
    """
    env = config.server["env"] = dict(config.server["env"])
    ids = [p for p in env.get("NEXTDNS_PROTECTED_PROFILES", "").split(",") if p.strip()]
    if profile_id.lower() not in {p.strip().lower() for p in ids}:
        ids.append(profile_id)
    env["NEXTDNS_PROTECTED_PROFILES"] = ",".join(ids)


def write_mcp_config(config: HarnessConfig, dest_dir: str | Path) -> Path:
    """Write the generated MCP client config (``opencode.json``) into workdir.

    The file declares the in-tree MCP server as a *local* stdio server under
    the name ``nextdns``, which is how the default opencode harness (and any
    harness that reads opencode's config format) mounts it natively. It is
    client plumbing only: nothing in it is model-facing text. The harness
    injects no instructions of its own — how the server is to be used is the
    server's business, and it publishes that through the MCP ``initialize``
    instructions, its tool descriptions, and its usage prompt/resource.
    Returns the written path (exposed to templates as ``{config_file}``).
    """
    srv = config.server
    # Never write the API key into the file. A live run showed the model can
    # browse the workdir with opencode's `read` tool, and this file is what it
    # reads first; an inlined key would then reach the model endpoint. The
    # harness launches the actor with NEXTDNS_API_KEY in its environment, and
    # opencode expands `{env:...}` when it loads the config, so the server
    # still receives the key while the file carries only a reference. The
    # redaction also covers a `--config` file that inlined a real key: it is
    # reduced to the reference before it reaches this document too.
    srv_env = _redacted_server_env(srv)
    # The server records here every tool call it actually executes. A read
    # leaves no trace in the NextDNS API, so this is the only evidence that a
    # step's operation really happened — the harness reads it back per step.
    srv_env.setdefault("NEXTDNS_CALL_LOG", str(Path(dest_dir) / CALL_LOG_NAME))
    doc: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        # Side-channel tools are disabled so the ONLY way the model can act is
        # through the nextdns MCP tools — one tool call per step, as designed.
        "tools": {
            "nextdns*": True,
            "shell": False,
            "bash": False,
            "search": False,
            "websearch": False,
            "webfetch": False,
            "write": False,
            "edit": False,
            "patch": False,
            # Code Mode's `execute` survived opencode 2.2.5's per-server
            # `codemode: false` and reached `require('fs')` anyway: every
            # failed step of the first target-model run opened by browsing
            # the workdir in JS before (instead of) calling the contract.
            "execute": False,
            # Filesystem browsing was live-observed (a live run read this very
            # config back into model context, and a step failed on a model
            # that poked the workdir instead of calling the contract): with the
            # key now only env-referenced the read is inert, but a browsing
            # model wastes its one session on it, so the tools are off too.
            "read": False,
            "glob": False,
            "grep": False,
            "list": False,
            # Observed in a live run: the model delegated a step to a subagent,
            # which is a second actor with its own context and tool access.
            "subagent": False,
            "task": False,
            # Nemotron live-run forensics: the remaining escape hatches were
            # opencode's own meta-tools — opencode_list_mcp_resources fired
            # before the model gave up on a step, experimental_image_generate
            # fabricated a plot PNG (google/gemini-nano-banana) instead of
            # calling plotAnalytics, and question asked the user instead of
            # calling. The wildcards close the families, not just the three
            # instances seen.
            "opencode_*": False,
            "experimental_*": False,
            "question": False,
        },
        "mcp": {
            "nextdns": {
                "type": "local",
                "command": [srv["command"], *[str(a) for a in srv["args"]]],
                "cwd": srv.get("cwd"),
                "environment": srv_env,
                "enabled": True,
                # Client plumbing, not model-facing text: Code Mode is opencode's
                # default and puts a JS evaluation between the model and the
                # server's contract. Turning it off registers the tools natively.
                "codemode": config.codemode,
            }
        },
    }
    out = Path(dest_dir) / "opencode.json"
    common.write_private_text(out, json.dumps(doc, indent=2) + "\n")
    return out


# =========================================================================== #
# Task targets + prompt (sent into the actor)
# =========================================================================== #


@dataclass
class TaskTargets:
    """The fixed, verifiable outcomes the task asks the actor to reach.

    Every target is something the measuring half can re-verify through the raw
    REST API after the actor exits, independent of the MCP path.
    """

    profile_name: str
    tag: str

    @property
    def renamed_name(self) -> str:
        return f"{self.profile_name}-renamed"

    @property
    def list_entries(self) -> dict[str, str]:
        """The entry the actor must leave present in each list type.

        Derived from ``common.LIST_ENTRIES`` — the same table the REST
        read-back resolves — so the prompt the actor is given and the check
        that judges it cannot name different entries after either is edited.
        """
        return {name: template.format(tag=self.tag) for name, template in common.LIST_ENTRIES.items()}

    @property
    def rewrite_name(self) -> str:
        return f"ai-e2e-a-{self.tag}.example.com"

    @property
    def rewrite_content(self) -> str:
        return "192.0.2.100"


# =========================================================================== #
# Actor result model + harness event parsing (pure helpers, unit-tested)
# =========================================================================== #


@dataclass
class ToolCall:
    """A tool the actor used, as observed in the harness's event stream."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    result_text: str = ""

    @property
    def ok(self) -> bool:
        return not self.is_error


@dataclass
class ActorRun:
    """Outcome of driving the actor harness once."""

    command: list[str] = field(default_factory=list)
    ok: bool = False
    exit_code: int | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    stderr: str = ""
    raw_lines: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def tools_used(self) -> list[str]:
        seen: list[str] = []
        for c in self.tool_calls:
            if c.name not in seen:
                seen.append(c.name)
        return seen


def _flatten_text(res: Any) -> str:
    """Flatten a tool result into readable text (bounded)."""
    if isinstance(res, dict):
        content = res.get("content")
        if isinstance(content, list):
            return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("text"))[:5000]
    if isinstance(res, str):
        return res[:5000]
    if res is None:
        return ""
    return str(res)[:5000]


def _tool_event_name(part: dict[str, Any]) -> str:
    """Best-effort tool name from a harness tool event.

    Harnesses differ: opencode exposes each MCP tool as ``nextdns_<tool>`` and
    may additionally route calls through a generic ``execute`` tool, whose
    ``input`` code references the underlying ``nextdns_<tool>``. We normalise
    both so coverage (an *observational*, non-verdicting signal) is useful.
    """
    tool = str(part.get("tool") or "")
    name = tool
    if tool in ("execute", "bash", "run", ""):
        code = (
            str((part.get("state") or {}).get("input", {}).get("code", ""))
            if isinstance(part.get("state"), dict)
            else ""
        )
        # opencode invokes MCP tools as tools.nextdns["<tool>"](...), while
        # other harnesses use a nextdns_<tool> prefix or a generic
        # tools.mcp.call_tool({name: "<tool>", ...}) wrapper. Try each form.
        # The wrapper is tried FIRST so a progressive-disclosure call
        # (tools.nextdns.call_tool({name: "addToDenylist", ...})) resolves to the
        # proxied tool rather than to the synthetic "call_tool" itself.
        m = re.search(r'call_tool\(\s*\{[^}]*?name["\']?\s*:\s*["\']([A-Za-z0-9_]+)', code)
        if not m:
            m = re.search(r'nextdns["\']?\s*\[\s*["\']([A-Za-z0-9_]+)', code)
        if not m:
            m = re.search(r"nextdns[._]([A-Za-z_][A-Za-z0-9_]*)", code)
        if not m:
            m = re.search(r'["\']nextdns_([A-Za-z][A-Za-z0-9_]*)["\']', code)
        if m:
            name = m.group(1)
        else:
            name = tool
    return name.removeprefix("nextdns_").removeprefix("tools.nextdns.")


def parse_actor_events(lines: list[str]) -> tuple[list[ToolCall], str]:
    """Parse a harness ``--format json``-style event stream.

    Recognises the two shapes we support out of the box:

    * opencode: ``tool_use`` events with ``part.tool`` (+ ``part.state``),
      ``text`` events, and an ``error`` event.
    * Claude Code ``stream-json``: assistant ``message`` events with
      ``tool_use`` content blocks, and a final ``result`` event.

    This is the *measuring* side reading what the *actor* did. It is best
    effort: the verdict never depends on it.
    """
    calls: list[ToolCall] = []
    final_text = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        if etype == "tool_use":
            part = ev.get("part") or {}
            state = part.get("state") or {}
            inp = state.get("input") or {}
            output = _flatten_text(state.get("output"))
            if not output and state.get("error"):
                # opencode reports a failed call's reason in state.error. Without
                # it, a provider-side drop ("OpenAI Chat stream ended without
                # finish_reason") is invisible to the verdict, and the step gets
                # judged as though the model chose to emit an empty call.
                output = _flatten_text(state["error"])
            calls.append(
                ToolCall(
                    name=_tool_event_name(part),
                    args=inp if isinstance(inp, dict) else {},
                    is_error=str(state.get("status", "completed"))
                    not in (
                        "completed",
                        "success",
                        "succeeded",
                    ),
                    result_text=output,
                )
            )

        elif etype in ("message", "assistant"):
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        inp = block.get("input")
                        calls.append(
                            ToolCall(
                                name=str(block.get("name") or ""),
                                args=inp if isinstance(inp, dict) else {},
                                is_error=False,
                            )
                        )
                    elif btype == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                        final_text = block["text"]

        elif etype == "result":
            text = ev.get("result")
            if isinstance(text, str) and text.strip():
                final_text = text

        elif etype == "text":
            part = ev.get("part") or {}
            if isinstance(part.get("text"), str) and part["text"].strip():
                final_text = part["text"]

    return calls, final_text


# =========================================================================== #
# Driving the actor (subprocess)
# =========================================================================== #


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the actor's whole process group, falling back to the direct child.

    The actor is its own session (``start_new_session=True``), so the group is
    the actor plus everything it spawned — the MCP server included. Without
    this, a timed-out or crashed actor left its MCP server alive, and that
    server kept writing state and call records that later steps attributed to
    themselves. Best effort throughout: an already-dead group is success.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    with _suppress():
        proc.kill()


async def _collect_streams(proc: asyncio.subprocess.Process, timeout_s: float) -> tuple[list[bytes], list[bytes], bool]:
    """Drain the actor's stdout and stderr while it runs, up to ``timeout_s``.

    Returns (stdout lines, stderr chunks, timed_out). On a timeout whatever the
    actor had already written is returned with it: the event stream is what
    shows whether the model stalled before its first tool call or in the middle
    Every abnormal exit — timeout, task cancellation (Ctrl-C arrives as
    :class:`asyncio.CancelledError` inside the loop), or a
    :class:`KeyboardInterrupt` — kills the actor's whole process group before
    propagating, so neither the actor nor the MCP server it spawned survives
    teardown to keep writing state or call records.
    """
    out: list[bytes] = []
    err: list[bytes] = []

    async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                return
            sink.append(chunk)

    drains = [
        asyncio.create_task(_drain(proc.stdout, out)),
        asyncio.create_task(_drain(proc.stderr, err)),
    ]
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        _kill_process_group(proc)
    except (asyncio.CancelledError, KeyboardInterrupt):
        _kill_process_group(proc)
        raise
    # The pipes close when the process does, so the drains finish on their own;
    # bounded so a child that leaves a grandchild holding a pipe cannot hang the
    # harness here. Cancellation arriving DURING this drain used to be swallowed
    # by ``_suppress`` and vanish: the run kept going while the actor's process
    # group stayed alive. It now propagates - after the same group teardown as
    # any other abnormal exit.
    try:
        with _suppress():
            await asyncio.wait_for(asyncio.gather(*drains, return_exceptions=True), timeout=10)
    except (asyncio.CancelledError, KeyboardInterrupt):
        _kill_process_group(proc)
        raise
    return out, err, timed_out


async def run_actor(config: HarnessConfig, prompt: str, workdir: Path, retries: int = 1) -> ActorRun:
    """Launch the actor harness in ``workdir`` and capture its event stream.

    Retries on transient provider/transport failures (e.g. ``ECONNRESET`` from a
    flaky model endpoint) up to ``retries`` times, so a single dropped
    connection does not doom the whole run.
    """
    values = {
        PLACEHOLDER_PROMPT: prompt,
        PLACEHOLDER_MODEL: config.model,
        PLACEHOLDER_WORKDIR: str(workdir),
        PLACEHOLDER_CONFIG_FILE: str(workdir / "opencode.json"),
        PLACEHOLDER_SERVER_COMMAND: config.server["command"] + " " + " ".join(config.server["args"]),
        # Redacted, like the config file: a template that places {server_env}
        # into the actor's argv must never render the real key into it (SEC-4) —
        # the reference expands inside the child, from the child's environment.
        PLACEHOLDER_SERVER_ENV: json.dumps(_redacted_server_env(config.server)),
    }
    argv = config.render_argv(values)
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    # asyncio's cwd= sets the child's working directory but NOT the $PWD env
    # var; opencode resolves its project config from $PWD. Without this it
    # reads the harness's directory, finds no opencode.json, and never mounts
    # the MCP server. OPENCODE_CONFIG pins the generated config explicitly.
    env["PWD"] = str(workdir)
    env["OPENCODE_CONFIG"] = str(Path(workdir) / "opencode.json")

    def _is_transient(err: str) -> bool:
        low = (err or "").lower()
        return any(
            tok in low for tok in ("econnreset", "transport:", "timed out", "connection reset", "socket hang up")
        )

    last_run: ActorRun | None = None
    for attempt in range(retries + 1):
        run = ActorRun(command=argv)
        if not argv or shutil.which(argv[0]) is None:
            run.error = f"actor harness command not found on PATH: {argv[0] if argv else '(empty)'}"
            return run

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            run.error = f"failed to spawn actor harness: {type(e).__name__}: {e}"
            run.duration_ms = int((time.monotonic() - started) * 1000)
            last_run = run
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            return run

        # Read the event stream as it arrives rather than buffering to the end.
        # A timed-out attempt used to discard every event the actor had already
        # produced, so the one question that matters most on a timeout - where
        # did the model stop? - was the one the report could not answer.
        out_lines, err_chunks, timed_out = await _collect_streams(proc, config.timeout_s)
        # Nothing the actor spawned outlives the attempt - on EVERY exit shape,
        # not only abnormal ones. Reproduced live: an actor that exits 0 can
        # leave the MCP server it spawned running, and the nonzero-exit-only
        # cleanup below used to let that whole process group leak; a
        # cancellation arriving mid-drain was swallowed by _suppress the same
        # way. killpg on an already-gone group is a no-op (ProcessLookupError),
        # so unconditional teardown costs nothing on the clean path.
        _kill_process_group(proc)

        err_text = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
        run.stderr = err_text
        raw = [line.decode("utf-8", errors="replace").rstrip("\r\n") for line in out_lines]
        lines = raw
        run.raw_lines = raw
        calls, final_text = parse_actor_events(lines)
        run.tool_calls = calls
        run.final_text = final_text
        run.duration_ms = int((time.monotonic() - started) * 1000)
        last_run = run

        if timed_out:
            run.error = f"actor harness timed out after {config.timeout_s:.0f}s"
            if attempt < retries and _is_transient(run.error):
                await asyncio.sleep(2)
                continue
            return run

        run.exit_code = proc.returncode
        run.ok = proc.returncode == 0
        if not run.ok and not run.error:
            run.error = err_text[-1500:] if err_text else f"actor harness exited with code {proc.returncode}"

        if run.ok:
            return run
        # Retry on transient provider/transport failures.
        if attempt < retries and _is_transient(run.error + " " + run.stderr):
            await asyncio.sleep(2)
            continue
        return run

    return last_run if last_run is not None else ActorRun(command=argv)


class _suppress:
    """Best-effort cleanup guard: swallow ordinary errors, never cancellation.

    Regular exceptions from a best-effort step are swallowed so cleanup cannot
    mask the original error. ``BaseException`` subclasses propagate:
    ``CancelledError`` arriving during the drain gather used to be swallowed
    here, which cancelled the run's cancellation and left the actor's process
    group alive.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *exc: object) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


# =========================================================================== #
# Measuring half: independent REST verifier
# =========================================================================== #


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
    text = "".join(str(getattr(part, "text", "")) for part in (getattr(res, "content", None) or []))
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


# =========================================================================== #
# Per-step harness: the model performs ONE operation per step; the harness
# verifies that step via REST before moving to the next. This mirrors the
# ~90-step agent checklist exactly: "create the profile" then check it,
# "update the profile" then check it, one at a time.
# =========================================================================== #


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
        if payload.get("success") is True:
            return True
        if "data" in payload:
            return payload["data"] is not None
        # Anything else the tool answered with - a chart's content blocks, a
        # DNS answer, a status document - is the payload for that tool.
        return bool(payload)
    return bool(payload)


def _identifiers_from_answers(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each name a tool answer carried to the id that answer gave it.

    Built only from this attempt's successful answers, so the equivalence is the
    server's own statement, not the harness guessing which record a call meant.
    Short values (`type: "A"`) are skipped: they are labels, not identifiers, and
    matching on them would tie every entry in the list to the same word.
    """
    out: dict[str, list[str]] = {}
    for record in records:
        if not record.get("ok"):
            continue
        # Evidence pairs first: they survive result truncation (the projection
        # keeps five list items; evidence keeps fifty identities). The result
        # walk stays as the fallback and as coverage for identifiers the
        # evidence lift does not name (non-``name`` label fields).
        evidence = record.get("evidence")
        if isinstance(evidence, dict):
            for entry in evidence.get("ids", []):
                if not isinstance(entry, dict):
                    continue
                ident, name = entry.get("id"), entry.get("name")
                if (
                    isinstance(ident, str)
                    and isinstance(name, str)
                    and len(name) >= 6
                    and name != ident
                    and ident not in out.setdefault(name, [])
                ):
                    out[name].append(ident)
        try:
            payload = json.loads(str(record.get("result") or ""))
        except (ValueError, TypeError):
            continue
        for identifier, names in _walk_identifiers(payload):
            for name in names:
                if identifier not in out.setdefault(name, []):
                    out[name].append(identifier)
    return out


def _walk_identifiers(node: Any) -> list[tuple[str, list[str]]]:
    """(id, names) for every object in a payload that carries an `id` string."""
    found: list[tuple[str, list[str]]] = []
    if isinstance(node, dict):
        identifier = node.get("id")
        if isinstance(identifier, str) and len(identifier) >= 4:
            names = [
                value
                for key, value in node.items()
                if key != "id" and isinstance(value, str) and len(value) >= 6 and value != identifier
            ]
            if names:
                found.append((identifier, names))
        for value in node.values():
            found.extend(_walk_identifiers(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_identifiers(value))
    return found


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
    tool = str(expect.get("tool", ""))
    # Endpoint expectations are matched against the spec's path templates, so
    # `{profile_id}` stays literal and a step whose call targets another
    # profile (plots, log download) still matches.
    endpoints = [str(item) for item in expect.get("endpoints", [])]
    want_args = [s.replace("{profile_id}", pid) for s in expect.get("args", [])]
    want_result = [s.replace("{profile_id}", pid) for s in expect.get("result", [])]
    profile_token = str(expect.get("profile", ""))
    if profile_token == ANY_PROFILE:
        want_profile = ""
    elif profile_token == PLOT_PROFILE:
        want_profile = alt_pid or pid
    else:
        want_profile = pid
    # An identifier the server handed back in this same attempt names the thing
    # it names. The checklist asks for the record called
    # `ai-e2e-a-…example.com`; the server's own list answer is what tells a
    # caller that its id is `c41a6xwk`, and deleting by that id is the workflow
    # the server documents and hints at. Both spellings identify the record.
    ids_by_name = _identifiers_from_answers(records)
    label = " or ".join(endpoints) if endpoints else tool

    def matches(record: dict[str, Any]) -> bool:
        if endpoints:
            return str(record.get("endpoint", "")) in endpoints
        return record.get("tool") == tool

    # Why the closest candidate was rejected, so a failure names the condition
    # it failed instead of guessing from the tool name alone.
    why = f"server recorded no {label} call in this attempt"
    if any(matches(r) and not r.get("ok") for r in records):
        why = f"{label} reached the server but every such call failed"

    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if not matches(record) or not record.get("ok"):
            continue
        if want_profile:
            recorded = str(record.get("profile_id", ""))
            if recorded != want_profile:
                why = f"{label} ran against profile {recorded or '(none recorded)'}, not the {want_profile} under test"
                continue
        args_text = str(record.get("args", ""))
        missing_args = [
            s for s in want_args if s not in args_text and not any(i in args_text for i in ids_by_name.get(s, ()))
        ]
        if missing_args:
            why = f"{label} was called without {missing_args}"
            continue
        result_text = str(record.get("result", ""))
        evidence_text = json.dumps(record.get("evidence", {}))
        missing_answer = [s for s in want_result if s not in result_text and s not in evidence_text]
        if missing_answer:
            why = f"{label} answered without {missing_answer}"
            continue
        if expect.get("data", True) and not _answered(result_text):
            why = f"{label} succeeded but returned no payload"
            continue
        # Only a write that SUCCEEDED can undo the step. A failed duplicate
        # (observed: addRewrite of an existing record, rejected upstream)
        # changed nothing, so it must not fail the step.
        later = [r for r in records[index + 1 :] if r.get("write") and r.get("ok")]
        if later:
            undo = later[0]
            return False, f"{label} succeeded but was followed by the write {_call_label(undo)}"
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


def build_steps(targets: TaskTargets) -> list[Step]:
    """Thin delegator: the step catalog lives in ``ai_e2e_steps``."""
    return _steps.build_steps(targets)


# =========================================================================== #
# Targeted runs: run one inclusive range of the checklist
# =========================================================================== #

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
    chosen: set[int] = set()
    for entry in entries:
        lo, hi = _entry_bounds(steps, entry, "--steps")
        if hi < lo:
            raise StepRangeError(
                f"--steps {entry!r}: {steps[hi].id!r} comes before {steps[lo].id!r} — a range is written low to high"
            )
        chosen.update(range(lo + 1, hi + 2))
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


def _expect_label(step: Step) -> str:
    """The call a step must produce, as the server's call log will see it."""
    exp = step.expect
    if not exp:
        return "(no call expectation)"
    label = " or ".join(str(e) for e in exp.get("endpoints", [])) or str(exp.get("tool", ""))
    bits = [f"args~{exp['args']}"] if exp.get("args") else []
    bits += [f"result~{exp['result']}"] if exp.get("result") else []
    bits += [f"profile={exp['profile']}"] if exp.get("profile") else []
    return f"{label} {' '.join(bits)}".strip()


async def _verify_step(
    m: MeasureClient, targets: TaskTargets, pid: str, step: Step, run: ActorRun
) -> tuple[str, str, str, dict[str, Any]]:
    """Thin delegator: the REST verifiers live in ``ai_e2e_verify``."""
    return await _verify.verify_step(m, targets, pid, step, run)


def _extract_error_events(run: ActorRun) -> list[str]:
    """Pull provider/actor error messages out of the raw event stream."""
    errs: list[str] = []
    for line in run.raw_lines:
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and ev.get("type") == "error":
            e = ev.get("error") or {}
            msg = e.get("message") if isinstance(e, dict) else str(e)
            if msg:
                errs.append(str(msg))
    if run.error:
        errs.append(run.error)
    return errs


def _actor_read_the_instrument(run: ActorRun, call_log: str | os.PathLike[str]) -> bool:
    # True when the actor opened the harness's own call log. The path is in the
    # MCP config the actor can read, so the subject can see the instrument the
    # verdict is made from. Detection is by path or file name in any call's
    # arguments, whichever tool it used to get there.
    path = str(call_log or "")
    if not path:
        return False
    sent = path.lower()
    name = os.path.basename(path).lower()
    for call in run.tool_calls:
        try:
            blob = json.dumps(call.args, default=str).lower()
        except (TypeError, ValueError):  # pragma: no cover - defensive
            blob = str(call.args).lower()
        if sent in blob or (name and name in blob):
            return True
    return False


def _summarize_calls(run: ActorRun) -> list[dict[str, Any]]:
    """Compact record of each tool call the actor made."""
    return [{"tool": c.name, "args": c.args, "ok": c.ok, "result": (c.result_text or "")[:300]} for c in run.tool_calls]


def _transient_actor_failure(run: ActorRun, blob: str) -> tuple[str, str]:
    """Classify a failed actor attempt as ``(kind, reason)``; ``("", "")`` if real.

    Retrying these is fair test scheduling, not help for the model: none of
    these causes depends on the model's capability, and in each of them the
    attempt never put a real call in front of the server, so its verdict
    measured infrastructure rather than behaviour.

    * ``unknown-tool``: opencode registers MCP tools lazily, so an attempt can
      fail before registration completes. Its wording varies, and the older
      detector matched only one of them: "Unknown tool: 'x'" and "No tool named
      \"nextdns_search_tools\" is currently available." both mean the same thing,
      and the second was being scored as a model failure.
    * ``wrong-tool-path``: the attempt called the code-mode name
      (``tools.nextdns.search_tools``) as a direct tool. opencode exposes MCP
      servers through Code Mode by default, and the direct name it registers is
      ``<server>_<tool>``, so that call cannot resolve in any attempt. 25 such
      attempts across two runs, none of them ever reaching the server: retrying
      three times measures the same thing each time, so it gets its own kind and
      a smaller budget rather than the registration label.
    * ``stall``: the actor hit its timeout without producing a single tool call
      (observed: ECONNRESET from the model provider after 150s on a trivial
      prompt, which stalled 13 of 89 steps in one run).
    * ``provider-stream``: the model provider ended the response mid tool call,
      and the client records the call with empty arguments and the reason in
      ``state.error`` (observed: "OpenAI Chat stream ended without
      finish_reason", which read as the model calling ``call_tool({})``).
    """
    # These two are the client's own words about a name it could not resolve.
    # They must be read from what the client reported, never from the model's
    # narration: a model that writes "the tool is not available" without making
    # a call has refused to act, which is a verdict, not a retry.
    client = _client_reported_errors(run)
    if any(marker in client for marker in _CODE_MODE_TOOL_PATH_MARKERS):
        return "wrong-tool-path", "actor called the code-mode tool name directly"
    if any(marker in client for marker in _LAZY_REGISTRATION_MARKERS):
        return "unknown-tool", "MCP tool not registered yet"
    if "timed out" in run.error.lower() and not run.tool_calls:
        return "stall", run.error
    if any(marker in blob for marker in _PROVIDER_STREAM_MARKERS):
        return "provider-stream", "model provider ended the response mid tool call"
    if not run.tool_calls and any(marker in blob for marker in _PROVIDER_OUTAGE_MARKERS):
        return "provider-unavailable", "model provider is not serving this model"
    return "", ""


def _client_reported_errors(run: ActorRun) -> str:
    """Lower-cased text the *client* produced, as opposed to the model's narration.

    Tool results count even when the call is flagged ok: opencode reports a
    resolution failure as a completed call whose output is the error text
    (observed: "Error calling tool 'call_tool': Unknown tool: 'x'" recorded with
    ok=True). What is excluded is the model's own final text and the harness's
    verdict strings — a model that writes "the tool is not available" without
    making a call has refused to act, which is a verdict, not a retry.
    """
    parts = [run.error or "", run.stderr or ""]
    parts += [call.result_text or "" for call in run.tool_calls]
    parts += _extract_error_events(run)
    return " ".join(parts).lower()


# Signatures of opencode's lazy MCP tool registration, not of a model decision.
_LAZY_REGISTRATION_MARKERS = ("unknown tool", "no tool named", "is currently available")

# The provider refusing to serve the model at all. Observed verbatim:
# "Function id '9fa6fd04-...' version 'null': Specified function in account
# 'X1UCRn4VumVsuKcJJB603mP5Yx09bRnJ67Idchb50ys' is not found" - a bare
# `opencode run` with the same model reproduces it, so no MCP code is involved.
_PROVIDER_OUTAGE_MARKERS = (
    "specified function in account",
    "provider_model_doesnt_exist",
    "the model you selected does not exist",
)

# Three steps in a row of the same outage is the signal; the run is measuring
# nothing and would otherwise spend its whole budget doing it.
PROVIDER_OUTAGE_STEPS = 3


# The code-mode name used as a direct tool name. opencode registers a direct MCP
# tool as `<server>_<tool>`, so `tools.<server>.<tool>` is not a name it has.
_CODE_MODE_TOOL_PATH_MARKERS = ('no tool named "tools.', "no tool named 'tools.")


def _provider_outage(run: ActorRun) -> bool:
    # Distinct from a bad answer: the attempt produced no tool call at all, so
    # the step measured the provider rather than the model. The message arrives
    # as an {"type":"error"} event in the actor's event stream, not in stderr.
    if run.tool_calls:
        return False
    blob = " ".join(_extract_error_events(run) + [run.stderr or "", run.final_text or ""]).lower()
    return any(marker in blob for marker in _PROVIDER_OUTAGE_MARKERS)


# Signatures of a provider/transport drop rather than a model decision.
_PROVIDER_STREAM_MARKERS = (
    "stream ended without finish_reason",
    "econnreset",
    "connection reset",
    "socket hang up",
    "network error",
)

# How many attempts each transient kind gets. A stall is pure infrastructure
# and costs the step its entire verdict, so it gets more chances than a lazy
# registration hiccup; all are capped so one flaky step cannot eat the budget.
TRANSIENT_ATTEMPTS = {
    "unknown-tool": 3,
    "stall": 5,
    "provider-stream": 3,
    "wrong-tool-path": 2,
    "provider-unavailable": 2,
}


def _feedback_block(
    step: Step,
    run: ActorRun,
    status: str,
    error: str,
    reason: str,
    rest_v: dict[str, Any],
    server_calls: list[dict[str, Any]] | None = None,
    verbose: bool = False,
) -> list[str]:
    """Render the command -> actor -> verify -> result chain for one step."""
    out = [f"  COMMAND : {step.description}"]
    mcp_calls = [c for c in run.tool_calls if c.name in allowed_tools()]
    discovery = [c for c in run.tool_calls if c.name in ("search_tools", "call_tool")]
    if mcp_calls:
        for c in mcp_calls[:4]:
            out.append(
                f"  ACTOR   : called {c.name}({json.dumps(c.args)[:120]})"
                + ("" if c.ok else f"  -> ERROR: {(c.result_text or '')[:200]}")
            )
        if len(mcp_calls) > 4:
            out.append(f"  ACTOR   : ... {len(mcp_calls) - 4} more MCP call(s)")
    elif discovery:
        for c in discovery[:4]:
            out.append(
                f"  ACTOR   : called {c.name}({json.dumps(c.args)[:120]})"
                + ("" if c.ok else f"  -> ERROR: {(c.result_text or '')[:200]}")
            )
    elif run.tool_calls:
        out.append(f"  ACTOR   : no MCP operation observed ({len(run.tool_calls)} built-in/discovery call(s))")
    else:
        errs = _extract_error_events(run)
        if errs:
            out.append(f"  ACTOR   : no tool call — {errs[0][:250]}")
        elif run.exit_code not in (0, None):
            out.append(
                f"  ACTOR   : no tool call — harness exited code {run.exit_code}"
                + (f", stderr: {run.stderr[:200]}" if run.stderr else "")
            )
        else:
            out.append(f"  ACTOR   : no tool call — replied: {(run.final_text or '')[:200]!r}")
    if verbose:
        # Failed calls and attempts at tools outside the advertised catalog
        # (hallucinated names, first-class MCP calls outside execute, wrong
        # session ids): invisible to the verdict, but they explain retries.
        shown = {id(c) for c in (mcp_calls or discovery)[:4]}
        attempts = [c for c in run.tool_calls if id(c) not in shown and (c.is_error or c.name not in _known_tools())]
        for c in attempts[:4]:
            out.append(
                f"  ATTEMPT : {c.name}({json.dumps(c.args)[:100]})"
                + (f"  -> ERROR: {(c.result_text or '')[:150]}" if not c.ok else "  -> not an advertised tool")
            )
        if len(attempts) > 4:
            out.append(f"  ATTEMPT : ... {len(attempts) - 4} more attempt(s)")
    if server_calls is not None:
        # The server's own account of the attempt — the only evidence a read
        # step leaves anywhere.
        out.append(f"  SERVER  : {_server_calls_text(server_calls)}")
    endpoint = rest_v.get("endpoint", "")
    detail = {k: v for k, v in rest_v.items() if k not in ("endpoint",) and v not in (None, "", {}, [])}
    out.append(f"  VERIFY  : {endpoint or '(no REST endpoint)'}" + (f"  {json.dumps(detail)[:200]}" if detail else ""))
    tag = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[status]
    why = error or reason
    out.append(f"  RESULT  : {tag}" + (f"  — {why}" if why else ""))
    return out


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


# =========================================================================== #
# Reporting
# =========================================================================== #


def write_report(report: Report, path: str, actor_log: str | None = None) -> None:
    if path == "-":
        for r in report.results:
            print(json.dumps(r.to_dict()))
        print(json.dumps({"summary": report.summary()}))
        if report.actor_raw_lines:
            print("--- actor event stream ---", file=sys.stderr)
            for ln in report.actor_raw_lines:
                print(ln, file=sys.stderr)
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r.to_dict()) for r in report.results]
    lines.append(json.dumps({"summary": report.summary()}))
    common.write_private_text(out, "\n".join(lines) + "\n")
    # Sidecar: the actor's raw event stream, for debugging model behavior.
    sidecar = Path(actor_log) if actor_log else out.with_name(out.stem + ".actor.jsonl")
    if report.actor_raw_lines:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        common.write_private_text(sidecar, "\n".join(report.actor_raw_lines) + "\n")
        print(f"Actor event stream written to {sidecar}", file=sys.stderr)


def write_skip_report(path: str, reason: str = "NEXTDNS_API_KEY not set") -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    common.write_private_text(out, json.dumps({"verdict": "SKIP", "reason": reason}) + "\n")


def render_text_report(report: Report) -> str:
    lines = ["=" * 72, "NextDNS MCP — MCP-native E2E harness (actor=harness, measure=REST)", "=" * 72]
    s = report.summary()
    lines.append(f"VERDICT : {s['verdict']}")
    lines.append(f"total   : {s['total']}  (passed {s['passed']}, skipped {s['skipped']}, failed {s['failed']})")
    lines.append(f"profile : {report.profile_id}  ({report.profile_name})")
    if report.setup:
        lines.append(f"setup   : {report.setup}")
    lines.append(
        f"actor   : {report.actor_kind} — {len(report.actor_tools_used)} tool(s) observed, {s['actor_tool_calls']} event(s)"
    )
    lines.append(f"tools   : {', '.join(report.actor_tools_used) or '(none)'}")
    lines.append(f"cleanup : {s['cleanup']}")
    if report.skipped_sections:
        lines.append(f"skipped sections: {', '.join(report.skipped_sections)}")
    lines.append("-" * 72)
    for r in report.results:
        icon = {"passed": "PASS", "skipped": "SKIP", "failed": "FAIL"}[r.status]
        lines.append(f"[{icon}] {r.id}  —  {r.description}")
        fb = (r.mcp_response or {}).get("feedback") if isinstance(r.mcp_response, dict) else None
        if fb:
            lines.extend(fb)
        if r.status == "failed" and not fb:
            lines.append(f"       error: {r.error}")
        elif r.status == "skipped" and r.reason and not fb:
            lines.append(f"       reason: {r.reason}")
    lines.append("=" * 72)
    # End-of-run summary: overall + per-section tally + failed steps.
    lines.append("SUMMARY")
    lines.append(
        f"  verdict: {s['verdict']}  ({s['passed']} passed, {s['skipped']} skipped, {s['failed']} failed of {s['total']} steps)"
    )
    by_section: dict[str, dict[str, int]] = {}
    for r in report.results:
        sec = by_section.setdefault(r.section, {"passed": 0, "skipped": 0, "failed": 0})
        sec[r.status] += 1
    for section, counts in by_section.items():
        lines.append(
            f"  {section:12s}: {counts['passed']} passed, {counts['skipped']} skipped, {counts['failed']} failed"
        )
    failed_ids = [r.id for r in report.results if r.status == "failed"]
    if failed_ids:
        lines.append("  failed steps:")
        lines.extend(f"    - {fid}" for fid in failed_ids)
    lines.append("=" * 72)
    return "\n".join(lines)


# What the server said when it refused a call, as reusable patterns: the schema
# names the parameter it rejected, and the upstream status code is what a caller
# can act on.
_SCHEMA_REJECTION = re.compile(r"validation error for call\[(\w+)\]\s*(\w+)")
_UPSTREAM_STATUS = re.compile(r"'(\d{3}) ")


def _failure_modes(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Every failure mode visible in one report row, as (mode, tool).

    A step can fail in several ways at once — the schema rejected the arguments
    *and* the end state is wrong — so this reports all of them, not the first.
    Each mode is reported once per step, however many calls inside that step
    show it: the counts answer "how many steps did this cost", which is the
    question the tuning loop asks.
    """
    modes: list[tuple[str, str]] = []
    response = row.get("mcp_response") or {}
    records = response.get("server_calls") or []
    tool = str(row.get("mcp_tool") or "")

    if response.get("instrument_read"):
        # Not a capability failure and not infrastructure: the subject read the
        # record the verdict is made from, which makes this step's number mean
        # something else.
        modes.append(("actor read the harness call log", tool))

    if not records:
        # A step whose actor never got as far as a call is usually the model
        # stalling, not the server refusing: the harness already says which.
        # Separating the two is what keeps a run's infrastructure noise from
        # looking like a contract problem.
        if any(str(t).startswith("stall") for t in response.get("transients") or []):
            modes.append(("actor stalled without calling", tool))
        elif any(str(t).startswith("wrong-tool-path") for t in response.get("transients") or []):
            modes.append(("actor called a tool path this client does not expose", tool))
        elif any(str(t).startswith("provider-unavailable") for t in response.get("transients") or []):
            modes.append(("model provider was not serving the model", tool))
        else:
            modes.append(("no call reached the server", tool))

    for record in records:
        if record.get("ok"):
            continue
        name = str(record.get("tool") or tool)
        error = str(record.get("error") or "").replace("\n", " ")
        match = _SCHEMA_REJECTION.search(error)
        if match:
            modes.append((f"schema rejected argument '{match.group(2)}'", name))
            continue
        match = _UPSTREAM_STATUS.search(error)
        if match:
            modes.append((f"upstream returned {match.group(1)}", name))
            continue
        modes.append(("server error: " + error[:70], name))

    expected = response.get("expected_call") or {}
    expected_why = str(expected.get("why") or "") if expected.get("ok") is False else ""
    if expected_why:
        for marker, label in _EXPECTED_CALL_MODES:
            if marker in expected_why:
                modes.append((label, str((expected.get("expect") or {}).get("tool") or tool)))
                break

    # The REST half of the verdict. When the transcript half already said this
    # — the step failed because the expected call never happened, and that text
    # is what lands in `error` — counting it twice would overstate the mode.
    if row.get("status") == "failed" and row.get("error"):
        error_text = " ".join(str(row["error"]).split())
        # Compare before truncating: a verdict naming a long endpoint exceeds
        # the bound, and comparing the cut text would count the same failure
        # twice.
        if error_text and error_text != " ".join(str(expected_why).split()):
            modes.append(("end state: " + error_text[:70], tool))
    return list(dict.fromkeys(modes))


# Ordered: the first marker present in the verdict text decides the label.
_EXPECTED_CALL_MODES = (
    ("reached the server but every such call failed", "expected call always failed"),
    ("was called without", "expected call missing an argument"),
    ("answered without", "expected call answered without the payload"),
    ("returned no payload", "expected call returned no payload"),
    ("followed by the write", "step undone by a later write"),
    ("no ", "no matching call recorded"),
)


def summarize_reports(paths: list[str]) -> str:
    """Group a run's failures by what went wrong, most common first.

    The tuning loop this exists for: fix one failure mode, re-run the steps that
    show it, repeat. So the output names each mode, how many steps it cost, and
    which steps to re-run.
    """
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(row)
                if "summary" in row:
                    notes.extend(str(n) for n in (row["summary"].get("notes") or []))
    steps = [row for row in rows if "status" in row]
    if not steps:
        return "no step results in " + ", ".join(paths)

    by_mode: dict[tuple[str, str], list[str]] = {}
    skip_reasons: Counter = Counter()
    for row in steps:
        if row["status"] == "passed":
            continue
        # A skipped step was never attempted - a budget that ran out, or a run
        # aborted on an outage. Grouping it as a failure mode buries the modes
        # that were actually measured.
        if row["status"] == "skipped":
            skip_reasons[str(row.get("reason") or "skipped")] += 1
            continue
        for mode, tool in _failure_modes(row):
            by_mode.setdefault((mode, tool), []).append(str(row.get("id") or "?"))

    counts = Counter(row["status"] for row in steps)
    lines = [
        "=" * 72,
        (
            f"failure modes: {len(paths)} report(s), {len(steps)} step(s) "
            f"({counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed, {counts.get('skipped', 0)} skipped)"
        ),
        "=" * 72,
    ]
    if notes:
        lines.append("  RUN NOT A CLEAN MEASUREMENT - what happened outside the model:")
        for note in notes:
            lines.append(f"    ! {note}")
        lines.append("")
    if not by_mode:
        lines.append("  no failures")
    else:
        for (mode, tool), ids in sorted(by_mode.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            shown = ", ".join(ids[:6]) + (", ..." if len(ids) > 6 else "")
            lines.append(f"{len(ids):3}  {mode:44} {tool:15} {shown}")
    if skip_reasons:
        lines.append("  skipped, never attempted:")
        for reason, count in skip_reasons.most_common():
            lines.append(f"{count:3}  {reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Thin delegator: the CLI lives in ``ai_e2e_cli``."""
    return _cli.main(argv)


# The extracted responsibilities of the four long functions (build_steps,
# _verify_step, _run_harness_scoped, main) live in sibling modules; the harness
# module keeps the thin delegators above and the shared primitives they name.
# Importing them here — after the shared names are defined — avoids a circular
# import: each module imports the harness's primitives from it.
import ai_e2e_cli as _cli
import ai_e2e_run as _run
import ai_e2e_steps as _steps
import ai_e2e_verify as _verify

if __name__ == "__main__":
    raise SystemExit(main())
