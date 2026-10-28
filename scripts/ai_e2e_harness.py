#!/usr/bin/env python3
"""MCP-native, API-verified E2E harness for the NextDNS MCP server.

SPDX-License-Identifier: MIT

This script is the E2E validation harness for the NextDNS MCP server. It has a
deliberate two-halves shape:

* **The actor is an MCP-native LLM harness.** The script sends a task prompt
  into an AI coding harness that connects to the NextDNS MCP server *natively
  over stdio* (the repo's own ``nextdns_mcp.server``) and does the tool calling
  itself: it decides which grouped tool to call, with which arguments, and in
  which order. The default harness is ``opencode`` (proven, built-in MCP
  support); the harness is a **configurable command template**, so any harness
  that can mount the MCP server may substitute it. The script does **not**
  drive the MCP tools itself, and it does **not** act through a REST
  side-channel — the MCP path is the only actor path.
* **The script measures.** After the harness finishes, the script verifies what
  *actually happened* by talking to the NextDNS REST API directly with the API
  key — independently of the MCP path. It checks the resulting server state
  (profile provisioned + renamed, settings values, list entries, rewrite
  record) against the fixed targets embedded in the task, measures tool
  coverage (which of the 8 grouped tools the actor observed exercising), then
  owns the cleanup: it deletes the test profile via REST and verifies the
  deletion (404 read-back). The verdict is a PASS/FAIL/SKIP report in JSONL.

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
profile lifecycle itself, through the MCP ``manageProfiles`` tool (so the
server's ACLs apply); everything else is the caller's responsibility — if the
selection tests "remove X", include the step that adds X.

How each step is judged
-----------------------
Three conditions, all of which must hold:

1. The MCP server recorded the step's **exact** expected call — tool, operation
   and identifying arguments — and that call succeeded and answered with a
   payload. The server writes what it executed to a JSONL call log
   (``NEXTDNS_CALL_LOG``, see ``nextdns_mcp.call_log``); the harness reads back
   the lines its own attempt appended. ``--print-steps`` prints each step's
   expectation.
2. **No state-changing call followed it** in the same attempt. A confirming
   read afterwards is normal; a later write can undo the step's own work.
3. The **REST end state** is correct, read back over the raw API.

REST alone cannot decide a step. It proves the state, not who produced it: a
step asking for ``manageProfiles(operation="get")`` passed a run whose transcript
was nothing but errors, because an earlier step had created the profile. For a
read there is nothing to read back at all — the account looks the same whether
or not the read happened — so the server's own record, including the payload it
answered with, is the only evidence there is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
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
API_BASE = common.API_BASE
SECTIONS = common.SECTIONS
SETTINGS_CATEGORIES = common.SETTINGS_CATEGORIES
AGGREGATE_METRICS = common.AGGREGATE_METRICS
SERIES_METRICS = common.SERIES_METRICS
PLOT_METRICS = common.PLOT_METRICS
MCP_TOOLS = common.MCP_TOOLS
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
_list_absent = common._list_absent
_entry_ids = common._entry_ids
_rewrites_contains = common._rewrites_contains
_analytics_has_data = common._analytics_has_data
_random_tag = common._random_tag
make_profile_name = common.make_profile_name


# =========================================================================== #
# Harness (actor) configuration
# =========================================================================== #

# The 8 grouped tools the actor is expected to exercise (its target surface).
ALLOWED_TOOLS = list(common.MCP_TOOLS)

# Harness-side tools the actor may legitimately use (discovery, sandbox);
# attempts outside these and ALLOWED_TOOLS are flagged in verbose feedback.
_BUILTIN_TOOLS = frozenset(
    {"execute", "glob", "read", "write", "edit", "bash", "search", "websearch", "patch", "search_tools", "call_tool"}
)

# Precomputed union: ALLOWED_TOOLS is a list, so `list | frozenset` would
# raise TypeError at feedback-render time (caught only when a call succeeds
# with a non-catalog name, which short-circuit unit tests missed).
_KNOWN_TOOLS = frozenset(ALLOWED_TOOLS) | _BUILTIN_TOOLS

# Default way to launch the in-tree MCP server (overridden by config/env).
# Uses the running interpreter (``sys.executable``) plus a PYTHONPATH pointing
# at the in-tree package, so it works no matter how the harness itself was
# launched (``uv run python scripts/...`` from the repo root).
DEFAULT_SERVER = {
    "command": sys.executable,
    "args": ["-m", "nextdns_mcp.server"],
    "cwd": str(_REPO_ROOT),
    "env": {"PYTHONPATH": str(_REPO_ROOT / "src")},
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
        # the actor itself never needs the key (the server uses it).
        cfg.server["env"] = dict(cfg.server["env"])
        if key := os.environ.get("NEXTDNS_API_KEY"):
            cfg.server["env"].setdefault("NEXTDNS_API_KEY", key)
        cfg.server["env"].setdefault("NEXTDNS_API_BASE", common.API_BASE)
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
        # (e.g. manageProfiles delete with the wrong id) even under ALL.
        if plot_pid := os.environ.get("NEXTDNS_PLOT_PROFILE"):
            protected = cfg.server["env"].get("NEXTDNS_PROTECTED_PROFILES", "")
            ids = [p for p in protected.split(",") if p.strip()]
            if plot_pid.strip().lower() not in {p.strip().lower() for p in ids}:
                ids.append(plot_pid.strip())
            cfg.server["env"]["NEXTDNS_PROTECTED_PROFILES"] = ",".join(ids)
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
    """
    rendered: list[str] = []
    for tok in template:
        out = tok
        for key, val in values.items():
            out = out.replace(key, val)
        rendered.append(out)
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
    srv_env = {str(k): str(v) for k, v in srv.get("env", {}).items()}
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
            "write": False,
            "edit": False,
            "patch": False,
            # Observed in a live run: the model delegated a step to a subagent,
            # which is a second actor with its own context and tool access.
            "subagent": False,
            "task": False,
        },
        "mcp": {
            "nextdns": {
                "type": "local",
                "command": [srv["command"], *[str(a) for a in srv["args"]]],
                "cwd": srv.get("cwd"),
                "environment": srv_env,
                "enabled": True,
            }
        },
    }
    out = Path(dest_dir) / "opencode.json"
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
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
        """The entry the actor must leave present in each list type."""
        return {
            "allowlist": f"ai-e2e-allow-{self.tag}.example.com",
            "denylist": f"ai-e2e-deny-{self.tag}.example.com",
            "privacy_blocklists": "nextdns-recommended",
            "privacy_natives": "alexa",
            "security_tlds": "zip",
            "parental_categories": "gambling",
            "parental_services": "tiktok",
        }

    @property
    def rewrite_name(self) -> str:
        return f"ai-e2e-a-{self.tag}.example.com"

    @property
    def rewrite_content(self) -> str:
        return "192.0.2.100"


def build_task_prompt(targets: TaskTargets) -> str:
    """The task handed to the actor.

    The prompt fixes every target (profile names, settings values, list
    entries, the rewrite) so the measuring half can verify the *resulting
    state* after the run without any out-of-band channel.
    """
    entries = targets.list_entries
    settings_lines = "\n".join(
        f"   - {cat}: set {json.dumps(spec['set'])}" for cat, spec in SETTINGS_CATEGORIES.items()
    )
    return f"""You are an autonomous validation agent for the NextDNS MCP server.
You have exactly these NextDNS MCP tools available: {", ".join(ALLOWED_TOOLS)}.
Use them to complete the task below. Do your own tool calling — decide the
arguments and ordering yourself from the tool descriptions.

## Task
Exercise the full NextDNS MCP tool surface against a dedicated test profile and
carry out each step below. These exact values are checked afterwards, so use
them verbatim.

1. Create a new profile named exactly: {targets.profile_name!r}
2. Get the new profile (you will need its profile_id for the remaining steps).
3. Update the new profile's name to exactly: {targets.renamed_name!r}
4. For EACH settings category, first read it, then update it to exactly these
   values (leave every other field unchanged):
{settings_lines}
5. For EACH list type, read it, then make sure EXACTLY this entry is present
   (add it if missing; do not remove any other entries):
   - allowlist: {entries["allowlist"]!r}
   - denylist: {entries["denylist"]!r}
   - privacy_blocklists: {entries["privacy_blocklists"]!r}
   - privacy_natives: {entries["privacy_natives"]!r}
   - security_tlds: {entries["security_tlds"]!r}
   - parental_categories: {entries["parental_categories"]!r}
   - parental_services: {entries["parental_services"]!r}
6. Add an A-record rewrite named exactly {targets.rewrite_name!r} with content
   exactly {targets.rewrite_content!r}.
7. Query analytics for the new profile for at least the 'status' and
   'queryTypes' metrics (aggregate, from_time=-1d).
8. Run a DoH lookup (dohLookup) for example.com through the new profile.
9. Get, then download, then clear the query logs for the new profile.
10. Generate at least one plot (plotAnalytics, metric=status) for the new
    profile.

## Rules
- Perform all writes against the test profile only. Never touch other profiles.
- If a call fails, note the error and continue with the remaining steps.
- Do NOT delete the test profile: the harness owns cleanup and deletes it after
  you finish.
- Do not modify any code or configuration.
- When you are done, reply with a short summary of what you did and the test
  profile id.
"""


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
        # (tools.nextdns.call_tool({name: "manageLists", ...})) resolves to the
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
        PLACEHOLDER_SERVER_ENV: json.dumps(config.server.get("env", {})),
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
            )
        except Exception as e:  # noqa: BLE001
            run.error = f"failed to spawn actor harness: {type(e).__name__}: {e}"
            run.duration_ms = int((time.monotonic() - started) * 1000)
            last_run = run
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            return run

        try:
            stdout, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=config.timeout_s)
        except TimeoutError:
            with _suppress():
                proc.kill()
            run.error = f"actor harness timed out after {config.timeout_s:.0f}s"
            run.stderr = ""
            run.duration_ms = int((time.monotonic() - started) * 1000)
            last_run = run
            if attempt < retries and _is_transient(run.error):
                await asyncio.sleep(2)
                continue
            return run

        err_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        run.stderr = err_text
        raw = stdout.decode("utf-8", errors="replace").splitlines()
        lines = raw
        run.raw_lines = raw
        calls, final_text = parse_actor_events(lines)
        run.tool_calls = calls
        run.final_text = final_text
        run.exit_code = proc.returncode
        run.ok = proc.returncode == 0
        if not run.ok and not run.error:
            run.error = err_text[-1500:] if err_text else f"actor harness exited with code {proc.returncode}"
        run.duration_ms = int((time.monotonic() - started) * 1000)
        last_run = run

        if run.ok:
            return run
        # Retry on transient provider/transport failures.
        if attempt < retries and _is_transient(run.error + " " + run.stderr):
            await asyncio.sleep(2)
            continue
        return run

    return last_run if last_run is not None else ActorRun(command=argv)


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


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
            base_url=common.API_BASE,
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


def _coverage_check(run: ActorRun, tool: str) -> CheckResult:
    """Observational: did the actor's event stream show this tool being used?

    This is a best-effort signal — a harness that routes MCP calls through a
    wrapper tool (e.g. opencode's ``execute``) can defeat name attribution —
    and it is reported as ``skipped`` (never ``failed``) so it can never fail
    the verdict on its own.
    """
    calls = [c for c in run.tool_calls if c.name == tool]
    if not calls:
        return CheckResult(
            id=f"coverage.{tool}",
            section="coverage",
            description=f"actor exercised the '{tool}' tool",
            status="skipped",
            mcp_tool=tool,
            reason="tool not observed in the harness event stream (best-effort signal)",
        )
    return CheckResult(
        id=f"coverage.{tool}",
        section="coverage",
        description=f"actor exercised the '{tool}' tool ({len(calls)} observed call(s))",
        status="passed",
        mcp_tool=tool,
        mcp_request={"calls": len(calls)},
    )


async def _measure_profile(m: MeasureClient, targets: TaskTargets, report: Report) -> dict[str, Any] | None:
    """State check: the test profile exists and was renamed as the task required.

    The actor renames the profile during the run, so by the time we measure it
    lives under the *renamed* name, not the original. We therefore provision
    (exists) if it is found under either name, and judge the rename check on
    whether it is specifically found under the renamed name.
    """
    orig = await m.find_profile(targets.profile_name)
    renamed = await m.find_profile(targets.renamed_name)
    if orig is not None:
        prof = orig
    elif renamed is not None:
        prof = renamed
    else:
        report.results.append(
            CheckResult(
                id="profiles.provision",
                section="profiles",
                description=f"test profile {targets.profile_name!r} exists on the server",
                status="failed",
                mcp_tool="manageProfiles",
                rest_verification={"endpoint": "GET /profiles", "verified_present": False},
                error="profile was not found via REST — the actor did not provision it",
            )
        )
        return None
    pid = str(prof.get("id"))
    report.profile_id = pid
    report.results.append(
        CheckResult(
            id="profiles.provision",
            section="profiles",
            description=f"test profile {targets.profile_name!r} exists on the server",
            status="passed",
            mcp_tool="manageProfiles",
            rest_verification={"endpoint": "GET /profiles", "verified_present": True, "profile_id": prof.get("id")},
        )
    )
    renamed_ok = isinstance(renamed, dict) and str(renamed.get("id")) == pid
    r = CheckResult(
        id="profiles.rename",
        section="profiles",
        description=f"test profile was renamed to {targets.renamed_name!r}",
        status="passed" if renamed_ok else "failed",
        mcp_tool="manageProfiles",
        mcp_request={"operation": "update", "name": targets.renamed_name},
        rest_verification={"endpoint": "GET /profiles", "verified_renamed": renamed_ok},
    )
    if not renamed_ok:
        r.error = f"profile {pid} was not found under its renamed name via REST"
    report.results.append(r)
    return prof


async def _measure_settings(m: MeasureClient, pid: str, report: Report) -> None:
    for cat, spec in SETTINGS_CATEGORIES.items():
        rest = await m.rest_get(f"/profiles/{pid}{spec['path']}")
        ok = _verify_settings_assert(rest, spec["assert"])
        r = CheckResult(
            id=f"settings.{cat}",
            section="settings",
            description=f"settings/{cat} read-back shows the task's target values {json.dumps(spec['assert'])}",
            status="passed" if ok else "failed",
            mcp_tool="manageSettings",
            mcp_request={"operation": "update", "category": cat, "settings": spec["set"]},
            rest_verification={
                "endpoint": f"GET /profiles/{pid}{spec['path']}",
                "rest_status": common._resp_status(rest),
            },
        )
        if not ok:
            r.error = f"REST read-back for settings/{cat} does not match the target values"
        report.results.append(r)


async def _measure_lists(m: MeasureClient, pid: str, targets: TaskTargets, report: Report) -> None:
    entries = targets.list_entries
    for spec in common._LIST_SPECS:
        name = spec["name"]
        base = f"/profiles/{pid}{spec['path']}"
        rest = await m.rest_get(base)
        present = _list_contains(rest, [entries[name]])
        r = CheckResult(
            id=f"lists.{name}",
            section="lists",
            description=f"list {name} read-back contains the target entry {entries[name]!r}",
            status="passed" if present else "failed",
            mcp_tool="manageLists",
            mcp_request={"list_type": name, "operation": "add", "entry": entries[name]},
            rest_verification={"endpoint": f"GET {base}", "rest_status": common._resp_status(rest)},
        )
        if not present:
            r.error = f"REST read-back for list {name} does not contain the target entry"
        report.results.append(r)


async def _measure_rewrites(m: MeasureClient, pid: str, targets: TaskTargets, report: Report) -> None:
    base = f"/profiles/{pid}/rewrites"
    rest = await m.rest_get(base)
    present = _rewrites_contains(rest, [targets.rewrite_name])
    r = CheckResult(
        id="rewrites.add",
        section="rewrites",
        description=f"rewrites read-back contains the target record {targets.rewrite_name!r}",
        status="passed" if present else "failed",
        mcp_tool="manageRewrites",
        mcp_request={"operation": "add", "name": targets.rewrite_name, "content": targets.rewrite_content},
        rest_verification={"endpoint": f"GET {base}", "rest_status": common._resp_status(rest)},
    )
    if not present:
        r.error = "REST read-back for rewrites does not contain the target record"
    report.results.append(r)


def _readonly_coverage(run: ActorRun, tool: str, section: str, cid: str, description: str) -> CheckResult:
    """Coverage check for read-only surface (analytics / doh / plots / logs).

    No REST state change is observable, so the event stream is the only signal.
    Reported as skipped (not failed) when unobserved.
    """
    calls = [c for c in run.tool_calls if c.name == tool]
    if not calls:
        return CheckResult(
            id=cid,
            section=section,
            description=description,
            status="skipped",
            mcp_tool=tool,
            reason="tool not observed in the harness event stream (best-effort signal)",
        )
    return CheckResult(
        id=cid,
        section=section,
        description=f"{description} ({len(calls)} observed call(s))",
        status="passed",
        mcp_tool=tool,
        mcp_request={"calls": len(calls)},
    )


def _measure_actor_run(run: ActorRun, report: Report) -> None:
    """Gate: did the actor harness run at all?"""
    report.results.append(
        CheckResult(
            id="actor.run",
            section="actor",
            description=f"actor harness exited cleanly (exit={run.exit_code}, {len(run.tool_calls)} tool event(s) observed)",
            status="passed" if run.ok else "failed",
            mcp_tool=None,
            mcp_response={"exit_code": run.exit_code, "tool_events": len(run.tool_calls)},
            error=(
                (run.error + chr(10) + "--- stderr (tail) ---" + chr(10) + run.stderr[-800:])
                if (run.error and run.stderr)
                else run.error
            )
            if not run.ok
            else "",
        )
    )


def _measure_coverage(run: ActorRun, report: Report) -> None:
    for tool in ALLOWED_TOOLS:
        report.results.append(_coverage_check(run, tool))


async def _cleanup(m: MeasureClient, pid: str, targets: TaskTargets, report: Report) -> None:
    """Harness-owned cleanup: delete the test profile, then verify via REST.

    The actor was explicitly told not to delete the profile, so the deletion
    and its verification belong to the measuring half.
    """
    resp = await m.rest_delete(f"/profiles/{pid}")
    if not (isinstance(resp, dict) and resp.get("ok")):
        report.cleanup = f"FAILED (REST DELETE /profiles/{pid} status={common._resp_status(resp)})"
        report.results.append(
            CheckResult(
                id="profiles.cleanup",
                section="profiles",
                description="test profile deleted by the harness and verified gone",
                status="failed",
                mcp_tool=None,
                mcp_request={"operation": "delete", "profile_id": pid},
                rest_verification={"endpoint": f"DELETE /profiles/{pid}", "rest_status": common._resp_status(resp)},
                error=f"REST DELETE was not ok (status={common._resp_status(resp)})",
            )
        )
        return
    gone = await _profile_gone(m, pid, targets)
    report.cleanup = "passed" if gone else "FAILED (profile still present after DELETE)"
    r = CheckResult(
        id="profiles.cleanup",
        section="profiles",
        description="test profile deleted by the harness and verified gone",
        status="passed" if gone else "failed",
        mcp_tool=None,
        mcp_request={"operation": "delete", "profile_id": pid},
        rest_verification={"endpoint": f"GET /profiles/{pid}", "verified_gone": gone},
    )
    if not gone:
        r.error = f"profile {pid} is still present after the harness deleted it"
    report.results.append(r)


async def _profile_gone(m: MeasureClient, pid: str, targets: TaskTargets) -> bool:
    """The profile is gone when neither name (original or renamed) resolves."""
    by_id = await m.rest_get(f"/profiles/{pid}")
    if isinstance(by_id, dict) and by_id.get("ok"):
        body = by_id.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and data.get("id"):
            return False
    if await m.find_profile(targets.profile_name) is not None:
        return False
    return await m.find_profile(targets.renamed_name) is None


async def _all_profile_ids(m: MeasureClient) -> dict[str, str]:
    """Map every profile id currently on the account to its name."""
    resp = await m.rest_get("/profiles")
    out: dict[str, str] = {}
    if isinstance(resp, dict) and resp.get("ok"):
        for p in (resp.get("body") or {}).get("data", []):
            if isinstance(p, dict) and p.get("id"):
                out[str(p["id"])] = str(p.get("name", ""))
    return out


def _never_delete_ids() -> set[str]:
    """Profile ids the stray sweep must never remove (protected + plot)."""
    ids: set[str] = set()
    for var in ("NEXTDNS_PROTECTED_PROFILES", "NEXTDNS_PLOT_PROFILE"):
        ids |= {p.strip() for p in os.environ.get(var, "").split(",") if p.strip()}
    return ids


async def _sweep_stray_profiles(m: MeasureClient, pre_existing: set[str], keep: set[str]) -> None:
    """Delete profiles the run created beyond the test profile.

    A model asked to create a profile sometimes creates several (a failed
    run left a stray 'new-profile' behind), and strays accumulate in a real
    account across runs. Anything that appeared during the run and is
    neither the test profile nor a protected/plot profile is removed.
    """
    protected = _never_delete_ids()
    for stray, name in (await _all_profile_ids(m)).items():
        if stray in pre_existing or stray in keep or stray in protected:
            continue
        _progress(f"stray sweep: removing profile {stray} ({name!r}) created during the run")
        await m.rest_delete(f"/profiles/{stray}")


async def _self_heal_profile(m: MeasureClient, pid: str, targets: TaskTargets) -> str:
    """Recreate the test profile via REST if the actor deleted it mid-run.

    A step prompt like "remove this entry from the denylist" can be misread as
    "delete the profile", and manageProfiles(delete) really does delete it —
    every later step then fails on 404s and the report measures a cascade, not
    the model's capability. The harness owns the profile lifecycle (the actor
    is told cleanup is harness-owned), so recreating it here is test
    scheduling, not an environment change: the prompts and the MCP server stay
    exactly as the actor sees them. The profile is recreated under its
    RENAMED name because every post-rename step verifies against that name.

    Returns the (possibly new) profile id, or "" if recreation failed.
    """
    if not pid:
        return pid
    resp = await m.rest_get(f"/profiles/{pid}")
    if isinstance(resp, dict) and resp.get("ok"):
        return pid
    created = await m.rest_post("/profiles", {"name": targets.renamed_name})
    new_id = ""
    if isinstance(created, dict) and created.get("ok"):
        body = created.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and data.get("id"):
            new_id = str(data["id"])
    if not new_id:
        found = await m.find_profile(targets.renamed_name)
        if found is not None:
            new_id = str(found.get("id", ""))
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
        env={str(k): str(v) for k, v in (server.get("env") or {}).items()},
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


async def _harness_provision_profile(config: HarnessConfig, targets: TaskTargets) -> tuple[str, str]:
    """Create the test profile through the MCP tool; returns (profile_id, note).

    A targeted run that starts after ``profiles.create`` still needs a profile
    for every later step to act on, and the harness owns that profile either
    way, so it creates it the way the server exposes it.
    """
    res = await _mcp_call(config.server, "manageProfiles", {"operation": "create", "name": targets.profile_name})
    data = res.get("data")
    body = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    pid = str(body.get("id", "")) if isinstance(body, dict) else ""
    if not pid:
        return "", f"FAILED: manageProfiles(create) returned {res.get('error') or data!r}"
    return pid, f"test profile {targets.profile_name!r} provisioned by the harness via manageProfiles(create)"


async def _harness_delete_profile(config: HarnessConfig, pid: str) -> str:
    """Delete the test profile through the MCP tool; returns an error string."""
    res = await _mcp_call(config.server, "manageProfiles", {"operation": "delete", "profile_id": pid})
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
    tool: str  # the MCP tool the model is instructed to use
    description: str
    prompt: str  # instruction to the actor ("{profile_id}" rendered at run time)
    verify: str  # verifier key in _verify_step
    vdata: dict[str, Any] = field(default_factory=dict)
    # The exact call this step must produce, matched against the server's own
    # call log (nextdns_mcp.call_log): {"tool", "operation", "args": [needed
    # substrings], "result": [needed substrings], "data": bool}. "{profile_id}"
    # inside a substring is rendered at run time.
    expect: dict[str, Any] = field(default_factory=dict)


def _expect(
    tool: str,
    operation: str = "",
    args: tuple[str, ...] = (),
    result: tuple[str, ...] = (),
    data: bool = True,
) -> dict[str, Any]:
    """Compact builder for a step's expected call (see Step.expect)."""
    return {
        "tool": tool,
        "operation": operation,
        "args": list(args),
        "result": list(result),
        "data": data,
    }


# Written by the MCP server (nextdns_mcp.call_log) into the run's workdir; the
# harness reads it back per step.
CALL_LOG_NAME = "mcp_calls.jsonl"


def _read_new_calls(path: Path, offset: int) -> list[dict[str, Any]]:
    """Call records the server appended since ``offset`` bytes into the log."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        chunk = fh.read()
    rows: list[dict[str, Any]] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _answered(result_text: str) -> bool:
    """Did the call answer with a payload?

    Effectiveness, not emptiness: a fresh test profile really does have an empty
    allowlist, so ``{"data": []}`` is a correct answer to "read the allowlist",
    and the write tools answer ``{"success": true}`` rather than an object. What
    is not correct is a call that returned no payload at all.
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
        # Tools without a data envelope (plots return content blocks).
        return bool(payload.get("content"))
    return bool(payload)


def _expected_call_ok(records: list[dict[str, Any]], expect: dict[str, Any], pid: str) -> tuple[bool, str]:
    """The two transcript conditions of the pass rule.

    1. the server recorded a SUCCESSFUL call matching this step's expected
       operation exactly — tool, operation, and the arguments that identify it
       (which list, which metric, which entry) — and, for a read, the call
       answered with a payload, since the API cannot show that a read happened;
    2. no state-changing call came AFTER it in the same attempt. Confirming
       reads afterwards are normal and allowed; undoing the step's own write is
       not.

    Returns (ok, reason_when_not).
    """
    if not expect:
        return True, ""
    tool = str(expect.get("tool", ""))
    operation = str(expect.get("operation", ""))
    want_args = [s.replace("{profile_id}", pid) for s in expect.get("args", [])]
    want_result = [s.replace("{profile_id}", pid) for s in expect.get("result", [])]
    label = f"{tool}({operation or '-'})"

    # Why the closest candidate was rejected, so a failure names the condition
    # it failed instead of guessing from the tool name alone.
    why = f"server recorded no {label} call in this attempt"
    if any(r.get("tool") == tool and not r.get("ok") for r in records):
        why = f"{label} reached the server but every such call failed"

    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record.get("tool") != tool or not record.get("ok"):
            continue
        if operation and str(record.get("operation", "")) != operation:
            continue
        args_text = str(record.get("args", ""))
        missing_args = [s for s in want_args if s not in args_text]
        if missing_args:
            why = f"{label} was called without {missing_args}"
            continue
        result_text = str(record.get("result", ""))
        missing_answer = [s for s in want_result if s not in result_text]
        if missing_answer:
            why = f"{label} answered without {missing_answer}"
            continue
        if expect.get("data", True) and not _answered(result_text):
            why = f"{label} succeeded but returned no payload"
            continue
        # Only a write that SUCCEEDED can undo the step. A failed duplicate
        # (observed: manageRewrites(add) of an existing record, rejected
        # upstream) changed nothing, so it must not fail the step.
        later = [r for r in records[index + 1 :] if r.get("write") and r.get("ok")]
        if later:
            undo = later[0]
            return False, (
                f"{label} succeeded but was followed by the write {undo.get('tool')}({undo.get('operation')})"
            )
        return True, ""

    return False, why


def _server_calls_text(records: list[dict[str, Any]]) -> str:
    """One-line rendering of the server's own record, for the live feedback."""
    if not records:
        return "(no tool call reached the server)"
    return "; ".join(
        f"{r.get('tool')}({r.get('operation') or '-'})" + ("" if r.get("ok") else f" ERR {str(r.get('error'))[:60]}")
        for r in records[:6]
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
    """Build the full per-step checklist (~89 steps) for this run."""
    pn = targets.profile_name
    rn = targets.renamed_name
    tag = targets.tag
    entries = targets.list_entries
    steps: list[Step] = []

    # ---- profiles -------------------------------------------------------
    steps.append(
        Step(
            "profiles.create",
            "profiles",
            "manageProfiles",
            f"create the test profile {pn!r}",
            _step_prompt(f"Create a new NextDNS profile named exactly '{pn}'."),
            "profile_exists",
            expect=_expect("manageProfiles", "create", args=(pn,)),
        )
    )
    steps.append(
        Step(
            "profiles.list",
            "profiles",
            "manageProfiles",
            "list all profiles and find the new one",
            _step_prompt(f"List all profiles and confirm '{pn}' is present."),
            "profile_exists",
            # The answer has to name the profile: a list call that never mentions it
            # did not find it.
            expect=_expect("manageProfiles", "list", result=(pn,)),
        )
    )
    steps.append(
        Step(
            "profiles.get",
            "profiles",
            "manageProfiles",
            "get the test profile by id",
            _step_prompt("Get the profile '{profile_id}' and report its details."),
            "profile_exists",
            expect=_expect("manageProfiles", "get", args=("{profile_id}",), result=("{profile_id}",)),
        )
    )
    steps.append(
        Step(
            "profiles.update",
            "profiles",
            "manageProfiles",
            f"rename the test profile to {rn!r}",
            _step_prompt(f"Rename the profile '{{profile_id}}' to exactly '{rn}'."),
            "profile_renamed",
            expect=_expect("manageProfiles", "update", args=(rn,)),
        )
    )

    # ---- settings: read + update per category ---------------------------
    for cat, spec in SETTINGS_CATEGORIES.items():
        steps.append(
            Step(
                f"settings.{cat}.read",
                "settings",
                "manageSettings",
                f"{cat}: read current settings",
                _step_prompt(f"Read the current {cat} settings for profile '{{profile_id}}' and report them."),
                "settings_read",
                {"cat": cat, "spec": spec},
                expect=_expect("manageSettings", "get", args=(cat,)),
            )
        )
        steps.append(
            Step(
                f"settings.{cat}.update",
                "settings",
                "manageSettings",
                f"{cat}: update to {json.dumps(spec['set'])}",
                _step_prompt(f"Update the {cat} settings for profile '{{profile_id}}' to {json.dumps(spec['set'])}."),
                "settings_update",
                {"cat": cat, "spec": spec},
                # Pin the payload, not just the tool: the step is about these fields.
                expect=_expect("manageSettings", "update", args=(cat, *spec["set"].keys())),
            )
        )

    # ---- lists: read/replace/add/update/remove per type -----------------
    for lt in _LIST_SPECS:
        name = lt["name"]
        values = [entries[name]]
        added = common._list_added(name, tag)
        steps.append(
            Step(
                f"lists.{name}.read",
                "lists",
                "manageLists",
                f"{name}: read current list",
                _step_prompt(f"Read the current {name} for profile '{{profile_id}}'."),
                "list_read",
                {"name": name, "spec": lt},
                expect=_expect("manageLists", "get", args=(name,)),
            )
        )
        steps.append(
            Step(
                f"lists.{name}.replace",
                "lists",
                "manageLists",
                f"{name}: replace with {values}",
                _step_prompt(f"Replace the {name} for profile '{{profile_id}}' with exactly {json.dumps(values)}."),
                "list_replace",
                {"name": name, "spec": lt, "values": values},
                expect=_expect("manageLists", "replace", args=(name, values[0])),
            )
        )
        if added is not None:
            steps.append(
                Step(
                    f"lists.{name}.add",
                    "lists",
                    "manageLists",
                    f"{name}: add {added!r}",
                    _step_prompt(f"Add '{added}' to the {name} for profile '{{profile_id}}'."),
                    "list_add",
                    {"name": name, "spec": lt, "values": values, "added": added},
                    expect=_expect("manageLists", "add", args=(name, added)),
                )
            )
        if lt["updatable"]:
            steps.append(
                Step(
                    f"lists.{name}.update",
                    "lists",
                    "manageLists",
                    f"{name}: update {values[0]!r} (active=false)",
                    _step_prompt(
                        f"Update the entry '{values[0]}' in the {name} for profile '{{profile_id}}' with {json.dumps({'active': False})}."
                    ),
                    "list_update",
                    {"name": name, "spec": lt, "values": values},
                    # entry_id is only knowable from a prior read, so pin the list
                    # and the field the step asks to change.
                    expect=_expect("manageLists", "update", args=(name, "active")),
                )
            )
        steps.append(
            Step(
                f"lists.{name}.remove",
                "lists",
                "manageLists",
                f"{name}: remove {values[0]!r}",
                _step_prompt(f"Remove '{values[0]}' from the {name} for profile '{{profile_id}}'."),
                "list_remove",
                {"name": name, "spec": lt, "values": values},
                expect=_expect("manageLists", "remove", args=(name,)),
            )
        )
        if added is not None:
            steps.append(
                Step(
                    f"lists.{name}.remove_added",
                    "lists",
                    "manageLists",
                    f"{name}: remove the added entry {added!r}",
                    _step_prompt(f"Remove '{added}' from the {name} for profile '{{profile_id}}'."),
                    "list_remove_added",
                    {"name": name, "spec": lt, "values": values, "added": added},
                    expect=_expect("manageLists", "remove", args=(name,)),
                )
            )

    # ---- rewrites: list, add each of 3, delete each ---------------------
    steps.append(
        Step(
            "rewrites.list",
            "rewrites",
            "manageRewrites",
            "list existing rewrites",
            _step_prompt("List the DNS rewrite records for profile '{profile_id}'."),
            "rewrite_list",
            expect=_expect("manageRewrites", "list"),
        )
    )
    rewrite_records = [
        {"name": targets.rewrite_name, "content": targets.rewrite_content},
        {"name": f"ai-e2e-aaaa-{tag}.example.com", "content": "2001:db8::dead"},
        {"name": f"ai-e2e-cname-{tag}.example.com", "content": f"ai-e2e-target-{tag}.example.com"},
    ]
    for rec in rewrite_records:
        steps.append(
            Step(
                f"rewrites.add.{rec['name'].split('-')[2]}",
                "rewrites",
                "manageRewrites",
                f"add rewrite {rec['name']!r} -> {rec['content']!r}",
                # Phrased as a mapping, not "A -> B": the small model read the arrow
                # as a sequence of two actions and reported having "added and then
                # deleted" the record — it undid its own successful write.
                _step_prompt(
                    f"Add a DNS rewrite record for profile '{{profile_id}}' so that the hostname "
                    f"'{rec['name']}' resolves to '{rec['content']}'."
                ),
                "rewrite_add",
                {"record": rec},
                expect=_expect("manageRewrites", "add", args=(rec["name"], rec["content"])),
            )
        )
    for rec in rewrite_records:
        steps.append(
            Step(
                f"rewrites.delete.{rec['name'].split('-')[2]}",
                "rewrites",
                "manageRewrites",
                f"delete rewrite {rec['name']!r}",
                _step_prompt(f"Delete the DNS rewrite record '{rec['name']}' from profile '{{profile_id}}'."),
                "rewrite_delete",
                {"record": rec},
                expect=_expect("manageRewrites", "delete", args=(rec["name"],)),
            )
        )

    # ---- analytics: aggregate + series per metric -----------------------
    for m in AGGREGATE_METRICS:
        vdata = {"metric": m}
        instr = f"Query aggregate analytics for profile '{{profile_id}}' for metric '{m}' over the last day"
        args = (m,)
        if m == "destinations":
            instr += ", with destination_type 'countries'"
            args += ("countries",)
        steps.append(
            Step(
                f"analytics.aggregate.{m}",
                "analytics",
                "queryAnalytics",
                f"queryAnalytics aggregate {m}",
                _step_prompt(instr + "."),
                "analytics",
                vdata,
                expect=_expect("queryAnalytics", args=args),
            )
        )
    for m in SERIES_METRICS:
        vdata_s: dict[str, Any] = {"metric": m, "series": True}
        instr = f"Query the time series for profile '{{profile_id}}' for metric '{m}' over the last day (series=true, interval 3600)"
        args_s = (m, "series")
        if m == "destinations":
            instr += ", with destination_type 'countries'"
            args_s += ("countries",)
        steps.append(
            Step(
                f"analytics.series.{m}",
                "analytics",
                "queryAnalytics",
                f"queryAnalytics series {m}",
                _step_prompt(instr + "."),
                "analytics",
                vdata_s,
                expect=_expect("queryAnalytics", args=args_s),
            )
        )

    # ---- doh -------------------------------------------------------------
    steps.append(
        Step(
            "doh.lookup",
            "doh",
            "dohLookup",
            "dohLookup example.com through the test profile",
            _step_prompt("Perform a DNS-over-HTTPS lookup for example.com (A record) through profile '{profile_id}'."),
            "tool_ok",
            expect=_expect("dohLookup", args=("example.com",), result=("example.com",)),
        )
    )

    # ---- plots: every metric --------------------------------------------
    for m in PLOT_METRICS:
        steps.append(
            Step(
                f"plots.{m}",
                "plots",
                "plotAnalytics",
                f"plotAnalytics {m} (PNG)",
                _step_prompt(
                    f"Generate an analytics plot for profile '{{profile_id}}' for metric '{m}' over the last day (PNG image)."
                ),
                "plots",
                {"metric": m},
                expect=_expect("plotAnalytics", args=(m,)),
            )
        )

    # ---- logs ------------------------------------------------------------
    steps.append(
        Step(
            "logs.get",
            "logs",
            "manageLogs",
            "get recent query logs",
            _step_prompt("Retrieve the 10 most recent query logs for profile '{profile_id}'."),
            "logs_get",
            expect=_expect("manageLogs", "get"),
        )
    )
    steps.append(
        Step(
            "logs.download",
            "logs",
            "manageLogs",
            "download retained logs (CSV)",
            _step_prompt("Download the retained query logs for profile '{profile_id}' as CSV."),
            "logs_download",
            expect=_expect("manageLogs", "download"),
        )
    )
    steps.append(
        Step(
            "logs.clear",
            "logs",
            "manageLogs",
            "clear query logs",
            _step_prompt("Clear the query logs for profile '{profile_id}'."),
            "logs_clear",
            expect=_expect("manageLogs", "clear"),
        )
    )

    # ---- delete the profile (model performs the delete) -----------------
    steps.append(
        Step(
            "profiles.delete",
            "profiles",
            "manageProfiles",
            f"delete the test profile {pn!r}",
            _step_prompt("Delete the profile '{profile_id}'."),
            "profile_gone",
            expect=_expect("manageProfiles", "delete", args=("{profile_id}",)),
        )
    )

    return steps


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
    label = str(exp.get("tool", ""))
    if exp.get("operation"):
        label += f"({exp['operation']})"
    bits = [f"args~{exp['args']}"] if exp.get("args") else []
    bits += [f"result~{exp['result']}"] if exp.get("result") else []
    return f"{label} {' '.join(bits)}".strip()


async def _verify_step(
    m: MeasureClient, targets: TaskTargets, pid: str, step: Step, run: ActorRun
) -> tuple[str, str, str, dict[str, Any]]:
    """REST-verify one step. Returns (status, error, reason, rest_verification)."""
    vd = step.vdata
    base_profile = f"/profiles/{pid}" if pid else None

    async def _req(method: str, path: str, **kw: Any) -> Any:
        if method == "GET":
            return await m.rest_get(path, **kw)
        return await m.rest_delete(path)

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
    if step.verify == "profile_gone":
        gone = (not pid) or await _profile_gone(m, pid, targets)
        return (
            "passed" if gone else "failed",
            "" if gone else f"profile {pid} is still present after delete",
            "",
            {"endpoint": f"GET /profiles/{pid}", "verified_gone": gone},
        )
    if step.verify == "settings_read":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else f"REST GET settings/{vd['cat']} not ok",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "rest_status": common._resp_status(rest)},
        )
    if step.verify == "settings_update":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._verify_settings_assert(rest, vd["spec"]["assert"])
        return (
            "passed" if ok else "failed",
            "" if ok else f"settings/{vd['cat']} read-back does not match {vd['spec']['assert']}",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok},
        )
    if step.verify in ("list_read",):
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else f"REST GET list {vd['name']} not ok",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "rest_status": common._resp_status(rest)},
        )
    if step.verify == "list_replace":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_contains(rest, vd["values"])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} does not contain {vd['values']}",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok},
        )
    if step.verify == "list_add":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        # Only this step's own effect: the entry it asked to add. Checking the
        # earlier step's entries as well let a failed `replace` cascade into the
        # `add` verdict and blame the wrong entry.
        ok = common._list_contains(rest, [vd["added"]])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} does not contain the entry this step asked to add: {vd['added']!r}",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok},
        )
    if step.verify == "list_update":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_contains(rest, vd["values"])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} lost entry {vd['values']} after update",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok},
        )
    if step.verify == "list_remove":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_absent(rest, vd["values"])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} still contains {vd['values']}",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified_absent": ok},
        )
    if step.verify == "list_remove_added":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_absent(rest, [vd["added"]])
        return (
            "passed" if ok else "failed",
            "" if ok else f"list {vd['name']} still contains added entry {vd['added']!r}",
            "",
            {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified_absent": ok},
        )
    if step.verify == "rewrite_list":
        rest = await _req("GET", f"{base_profile}/rewrites")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else "REST GET rewrites not ok",
            "",
            {"endpoint": f"GET {base_profile}/rewrites", "rest_status": common._resp_status(rest)},
        )
    if step.verify == "rewrite_add":
        rec = vd["record"]
        rest = await _req("GET", f"{base_profile}/rewrites")
        ok = common._rewrites_contains(rest, [rec["name"]])
        return (
            "passed" if ok else "failed",
            "" if ok else f"rewrite {rec['name']!r} not found via REST",
            "",
            {"endpoint": f"GET {base_profile}/rewrites", "verified": ok},
        )
    if step.verify == "rewrite_delete":
        rec = vd["record"]
        rest = await _req("GET", f"{base_profile}/rewrites")
        ok = not common._rewrites_contains(rest, [rec["name"]])
        return (
            "passed" if ok else "failed",
            "" if ok else f"rewrite {rec['name']!r} still present after delete",
            "",
            {"endpoint": f"GET {base_profile}/rewrites", "verified_absent": ok},
        )
    if step.verify == "analytics":
        metric = vd["metric"]
        series = bool(vd.get("series"))
        suffix = ";series" if series else ""
        params: dict[str, Any] = {"from": "-1d", "to": "now"}
        if series:
            params["interval"] = 3600
        if metric == "destinations":
            params["type"] = "countries"
        rest = await _req("GET", f"{base_profile}/analytics/{metric}{suffix}", params=params)
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
    if step.verify == "tool_ok":
        # DoH resolution leaves no account state to read back, so REST cannot
        # decide this step at all. The server's call log does (see
        # _expected_call_ok): the lookup ran and answered.
        return ("passed", "", "", {"note": "read-only DoH lookup; verdict from the server call log"})
    if step.verify == "plots":
        plot_pid = os.environ.get("NEXTDNS_PLOT_PROFILE") or pid
        rest = await _req("GET", f"/profiles/{plot_pid}/analytics/status")
        has_data = common._analytics_has_data(rest)
        if not has_data:
            return (
                "skipped",
                "",
                "no query history on the fresh profile; plot checks need data",
                {"endpoint": f"GET {base_profile}/analytics/status", "has_data": False},
            )
        return (
            "passed",
            "",
            "",
            {
                "has_data": True,
                "endpoint": f"GET /profiles/{plot_pid}/analytics/status",
                "note": "plot generation verified by the server call log",
            },
        )
    if step.verify == "logs_get":
        rest = await _req("GET", f"{base_profile}/logs", params={"limit": 10})
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else "REST GET logs not ok",
            "",
            {"endpoint": f"GET {base_profile}/logs", "rest_status": common._resp_status(rest)},
        )
    if step.verify == "logs_download":
        log_pid = os.environ.get("NEXTDNS_PLOT_PROFILE") or pid
        rest = await _req("GET", f"/profiles/{log_pid}/logs", params={"limit": 10})
        # Log entries are mostly strings (domain, status, timestamps), so an
        # analytics-style numeric-data check would wrongly report "no logs".
        body = rest.get("body") if isinstance(rest, dict) else None
        has = bool(isinstance(body, dict) and body.get("data"))
        if not has:
            return (
                "skipped",
                "",
                "no retained logs yet (NextDNS log delay on a fresh profile)",
                {"endpoint": f"GET {base_profile}/logs"},
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
    if step.verify == "logs_clear":
        rest = await m.rest_delete(f"{base_profile}/logs")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return (
            "passed" if ok else "failed",
            "" if ok else "REST DELETE logs not ok",
            "",
            {"endpoint": f"DELETE {base_profile}/logs", "rest_status": common._resp_status(rest)},
        )
    return ("failed", f"unknown verifier {step.verify!r}", "", {})


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
      \"tools.nextdns.search_tools\" is currently available." both mean the same
      thing, and the second was being scored as a model failure.
    * ``stall``: the actor hit its timeout without producing a single tool call
      (observed: ECONNRESET from the model provider after 150s on a trivial
      prompt, which stalled 13 of 89 steps in one run).
    * ``provider-stream``: the model provider ended the response mid tool call,
      and the client records the call with empty arguments and the reason in
      ``state.error`` (observed: "OpenAI Chat stream ended without
      finish_reason", which read as the model calling ``call_tool({})``).
    """
    if any(marker in blob for marker in _LAZY_REGISTRATION_MARKERS):
        return "unknown-tool", "MCP tool not registered yet"
    if "timed out" in run.error.lower() and not run.tool_calls:
        return "stall", run.error
    if any(marker in blob for marker in _PROVIDER_STREAM_MARKERS):
        return "provider-stream", "model provider ended the response mid tool call"
    return "", ""


# Signatures of opencode's lazy MCP tool registration, not of a model decision.
_LAZY_REGISTRATION_MARKERS = ("unknown tool", "no tool named", "is currently available")

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
TRANSIENT_ATTEMPTS = {"unknown-tool": 3, "stall": 5, "provider-stream": 3}


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
    mcp_calls = [c for c in run.tool_calls if c.name in ALLOWED_TOOLS]
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
        attempts = [c for c in run.tool_calls if id(c) not in shown and (c.is_error or c.name not in _KNOWN_TOOLS)]
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


async def run_harness(api_key: str, config: HarnessConfig) -> Report:
    tag = common._random_tag()
    resolved_profile_name = common._resolve_profile_name(config.profile_name or None)
    targets = TaskTargets(profile_name=resolved_profile_name, tag=tag)
    report = Report(started_at=time.time(), api_key_present=True, profile_name=targets.profile_name)
    report.actor_kind = "mcp-native-harness (per-step)"
    report.actor_command = config.command

    steps = build_steps(targets)
    selected = select_steps(steps, config.step_selection(len(steps)))
    total = len(steps)
    if len(selected) < total:
        ids = (
            ", ".join(s.id for _, s in selected)
            if len(selected) <= 12
            else f"{selected[0][1].id} .. {selected[-1][1].id}"
        )
        _progress(f"targeted run: {len(selected)} of {total} steps ({ids})")
    _progress(f"profile name: {targets.profile_name}  ({len(selected)} of {total} steps)")
    workdir = Path(config.workdir) if config.workdir else Path(tempfile.mkdtemp(prefix="nextdns_e2e_"))
    workdir.mkdir(parents=True, exist_ok=True)
    write_mcp_config(config, workdir)
    call_log = workdir / CALL_LOG_NAME

    step_cfg = HarnessConfig()
    step_cfg.command = list(config.command)
    step_cfg.model = config.model
    step_cfg.server = config.server
    step_cfg.timeout_s = config.step_timeout_s

    pid = ""
    started = time.monotonic()
    budget = config.timeout_s
    total_events = 0
    tools_used: list[str] = []
    last_stderr = ""
    all_raw: list[str] = []

    async with MeasureClient(api_key) as m:
        # Remove stale same-name profiles left by earlier interrupted runs so
        # they cannot pollute this run's verification.
        for nm in (targets.profile_name, targets.renamed_name):
            stale = await m.find_profile(nm)
            if stale is not None:
                _progress(f"removing stale profile {stale.get('id')} ({nm}) from an earlier run")
                await m.rest_delete(f"/profiles/{stale['id']}")

        # Snapshot the account so profiles the ACTOR creates (it sometimes
        # creates extras beyond the test profile) can be swept at the end.
        pre_existing = set(await _all_profile_ids(m))

        # Targeted run: when the range does not include profiles.create the
        # harness provisions the test profile itself, through the MCP tool so
        # the server's ACLs apply. Without it every later step has nothing to
        # act on and the report measures nothing.
        if needs_harness_setup(selected):
            pid, note = await _harness_provision_profile(config, targets)
            report.setup = note
            report.profile_id = pid
            _progress(f"targeted setup: {note}")

        for i, step in selected:
            if time.monotonic() - started > budget:
                rest = CheckResult(
                    step.id,
                    step.section,
                    step.description,
                    "skipped",
                    mcp_tool=step.tool,
                    reason=f"overall time budget ({budget:.0f}s) exhausted",
                )
                report.results.append(rest)
                continue
            if step.section != "profiles" and not pid:
                report.results.append(
                    CheckResult(
                        step.id,
                        step.section,
                        step.description,
                        "skipped",
                        mcp_tool=step.tool,
                        reason="test profile was not provisioned",
                    )
                )
                continue
            if step.section != "profiles" and pid:
                # The actor may delete the test profile mid-run (wrong-tool
                # mistake); recreate it so the report measures each step, not
                # a 404 cascade. Harness scheduling only — no env change.
                healed = await _self_heal_profile(m, pid, targets)
                if healed != pid:
                    pid = healed
                    report.profile_id = pid

            prompt_pid = pid
            if (step.section == "plots" or step.id == "logs.download") and os.environ.get("NEXTDNS_PLOT_PROFILE"):
                prompt_pid = os.environ["NEXTDNS_PLOT_PROFILE"]
            prompt = step.prompt.replace("{profile_id}", prompt_pid)
            run: ActorRun | None = None
            status, error, reason = "failed", "", ""
            rest_v: dict[str, Any] = {}
            # Attempt loop: infrastructure transients re-run the step (see
            # _transient_actor_failure), each kind with its own cap.
            transients: list[str] = []
            attempts_by_kind = {kind: 0 for kind in TRANSIENT_ATTEMPTS}
            attempt = 0
            server_calls: list[dict[str, Any]] = []
            while True:
                attempt += 1
                _progress(f"[{i}/{total}] {step.id}" + (f" (retry {attempt - 1})" if attempt > 1 else ""))
                # Where the server's call log stands before this attempt; the
                # lines after it are this attempt's authoritative record.
                log_offset = call_log.stat().st_size if call_log.exists() else 0
                # One subprocess per attempt: run_actor's own internal retry is
                # off here because it would silently double the window this
                # step's call-log slice covers. Observed: a step whose first
                # attempt timed out at 300s and whose second produced no parsed
                # events reported "actor=0, server=56" - the server records
                # were real, they just came from both attempts. The step loop
                # below already retries transients, with the bookkeeping.
                run = await run_actor(step_cfg, prompt, workdir, retries=0)
                total_events += len(run.tool_calls)
                for t in run.tools_used():
                    if t not in tools_used:
                        tools_used.append(t)
                if run.stderr:
                    last_stderr = run.stderr
                all_raw.extend(run.raw_lines)
                server_calls = _read_new_calls(call_log, log_offset)

                status, error, reason, rest_v = await _verify_step(m, targets, pid, step, run)
                # REST says what the state is, not who produced it, and for a
                # read it cannot see the call at all. So the server's own record
                # decides the transcript half of the verdict: the exact expected
                # call succeeded, and nothing wrote over it afterwards.
                call_ok, call_why = _expected_call_ok(server_calls, step.expect, pid)
                if status == "passed" and not call_ok:
                    status = "failed"
                    error = call_why
                kind, why = _transient_actor_failure(
                    run,
                    " ".join(
                        [error, reason]
                        + [c.result_text or "" for c in run.tool_calls]
                        + [str(r.get("error") or "") for r in server_calls]
                        + ([run.final_text] if run.final_text else [])
                    ).lower(),
                )
                if status != "failed" or not kind or attempts_by_kind[kind] >= TRANSIENT_ATTEMPTS[kind]:
                    break
                attempts_by_kind[kind] += 1
                transients.append(f"{kind}: {why}")
                _progress(f"  RETRY   : transient actor failure ({why}); retrying")

            if run is None:  # pragma: no cover - defensive
                run = ActorRun(command=list(step_cfg.command))

            # Verdict comes from the task outcome (REST verification), not from
            # how many tool calls the model needed to get there.
            fb = _feedback_block(
                step, run, status, error, reason, rest_v, server_calls=server_calls, verbose=config.verbose
            )
            for ln in fb:
                _progress(ln)

            r = CheckResult(
                id=step.id,
                section=step.section,
                description=step.description,
                status=status,
                mcp_tool=step.tool,
                mcp_request={
                    "prompt": prompt[:500],
                    "exit_code": run.exit_code,
                    "tool_events": len(run.tool_calls),
                    "attempts": attempt,
                },
                mcp_response={
                    "calls": _summarize_calls(run),
                    "final_text": run.final_text[:1000],
                    "error_events": _extract_error_events(run)[:3],
                    "feedback": fb,
                    # Recorded, not just printed: a report that cannot say how
                    # many attempts a step took hides infrastructure noise.
                    "transients": transients,
                    # The server's own record of the attempt: what it executed,
                    # with which operation, and whether it succeeded.
                    "server_calls": server_calls,
                    # How the transcript half of the verdict was decided, so a
                    # failure says which condition it failed.
                    "expected_call": {"ok": call_ok, "why": call_why, "expect": step.expect},
                },
                rest_verification=rest_v,
                reason=reason,
                error=error,
                duration_ms=run.duration_ms,
            )
            report.results.append(r)

            # Capture the profile id as soon as the profile exists — even when
            # this step's own verdict failed. Otherwise every later prompt
            # renders a literal "{profile_id}" and the report measures a
            # cascade of argument errors instead of the model's behaviour.
            if not pid and step.verify in ("profile_exists", "profile_renamed"):
                prof = await m.find_profile(targets.profile_name) or await m.find_profile(targets.renamed_name)
                if prof is not None:
                    pid = str(prof.get("id"))
                    report.profile_id = pid

        # Safety net: if the model deleted the profile, verify; if not, the
        # harness owns cleanup. For a targeted range that excludes
        # profiles.delete the teardown is harness-owned from the start, so it
        # goes through the same MCP tool the actor would have used.
        if pid:
            gone = await _profile_gone(m, pid, targets)
            if not gone and needs_harness_teardown(selected):
                err = await _harness_delete_profile(config, pid)
                gone = await _profile_gone(m, pid, targets)
                if gone:
                    _progress("targeted teardown: test profile deleted via manageProfiles(delete)")
                else:
                    _progress(f"targeted teardown incomplete via MCP ({err or 'profile still present'})")
            if not gone:
                _progress("safety cleanup: deleting the test profile via REST")
                for attempt in range(4):
                    resp = await m.rest_delete(f"/profiles/{pid}")
                    if isinstance(resp, dict) and resp.get("ok"):
                        break
                    await asyncio.sleep(5 * (attempt + 1))
                gone = await _profile_gone(m, pid, targets)
            report.cleanup = (
                "clean: profile deleted and verified gone via REST" if gone else "FAILED (profile still present)"
            )
            report.profile_id = pid
        else:
            report.cleanup = "skipped (test profile not found on the server)"

        # Remove any extra profiles the actor created during the run so they
        # do not accumulate in the account between runs.
        await _sweep_stray_profiles(m, pre_existing, {pid} if pid else set())

    report.actor_tool_calls = total_events
    report.actor_tools_used = tools_used
    report.actor_stderr = last_stderr[:4000]
    report.actor_raw_lines = all_raw[-500:]
    report.actor_duration_ms = int((time.monotonic() - started) * 1000)
    report.finished_at = time.time()
    return report


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
    with out.open("w", encoding="utf-8") as fh:
        for r in report.results:
            fh.write(json.dumps(r.to_dict()) + "\n")
        fh.write(json.dumps({"summary": report.summary()}) + "\n")
    # Sidecar: the actor's raw event stream, for debugging model behavior.
    sidecar = Path(actor_log) if actor_log else out.with_name(out.stem + ".actor.jsonl")
    if report.actor_raw_lines:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("\n".join(report.actor_raw_lines) + "\n", encoding="utf-8")
        print(f"Actor event stream written to {sidecar}", file=sys.stderr)


def write_skip_report(path: str, reason: str = "NEXTDNS_API_KEY not set") -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"verdict": "SKIP", "reason": reason}) + "\n", encoding="utf-8")


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
# names the parameter it rejected, the tool names the one it needed, and the
# upstream status code is what a caller can act on.
_SCHEMA_REJECTION = re.compile(r"validation error for call\[(\w+)\]\s*(\w+)")
_TOOL_REJECTION = re.compile(r"(\w+) is required for (\w+) operation")
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

    if not records:
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
        match = _TOOL_REJECTION.search(error)
        if match:
            modes.append((f"tool rejected call: {match.group(1)} missing", name))
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
        detail = " ".join(str(row["error"]).split())[:70]
        if detail and detail != expected_why:
            modes.append(("end state: " + detail, tool))
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
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    steps = [row for row in rows if "status" in row]
    if not steps:
        return "no step results in " + ", ".join(paths)

    by_mode: dict[tuple[str, str], list[str]] = {}
    for row in steps:
        if row["status"] == "passed":
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
    if not by_mode:
        lines.append("  no failures")
        return "\n".join(lines)
    for (mode, tool), ids in sorted(by_mode.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        shown = ", ".join(ids[:6]) + (", ..." if len(ids) > 6 else "")
        lines.append(f"{len(ids):3}  {mode:44} {tool:15} {shown}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP-native, API-verified E2E harness for the NextDNS MCP server.")
    parser.add_argument(
        "--config",
        help="JSON config describing the run (command/model/prompt/workdir/timeout_s/server)",
    )
    parser.add_argument(
        "--harness-cmd",
        help="actor harness command line, e.g. 'opencode run --standalone ... --model ollama-cloud/x'",
    )
    parser.add_argument("--model", help="model for the {model} placeholder in the harness command")
    parser.add_argument("--profile-name", help="test profile name (supports {date}/{timestamp} placeholders)")
    parser.add_argument(
        "--report", default="artifacts/ai_e2e_report.jsonl", help="path for the JSONL report, or '-' for stdout"
    )
    parser.add_argument("--quiet", action="store_true", help="do not print the text report")
    parser.add_argument("--env-file", help="path to a .env file to load (defaults to ./.env and <repo>/.env)")
    parser.add_argument("--actor-log", help="path for the actor raw event stream (default: <report>.actor.jsonl)")
    parser.add_argument(
        "--tool-search",
        choices=("off", "regex", "bm25"),
        help="enable FastMCP progressive tool disclosure on the MCP server "
        "(sets NEXTDNS_TOOL_SEARCH; tools/list then exposes only search_tools + call_tool)",
    )
    parser.add_argument(
        "--budget", type=float, help="overall run budget in seconds (default 7200; slow models need more)"
    )
    parser.add_argument("--step-timeout", type=float, help="per-step actor timeout in seconds (default 300)")
    parser.add_argument(
        "--steps",
        help="targeted selection: comma-separated step ids, 1-based positions or "
        "A-B inclusive ranges (see --print-steps), e.g. "
        "'20-21,lists.privacy_natives.replace-36,profiles.delete'",
    )
    parser.add_argument(
        "--start-step",
        help="first step of an inclusive targeted range: a step id "
        "(e.g. lists.allowlist.add) or a 1-based position (see --print-steps)",
    )
    parser.add_argument(
        "--end-step",
        help="last step of an inclusive targeted range: a step id or a 1-based position (see --print-steps)",
    )
    parser.add_argument(
        "--print-steps",
        action="store_true",
        help="print the step catalog (the ids and positions --steps takes) and exit",
    )
    parser.add_argument(
        "--summarize",
        nargs="+",
        metavar="REPORT",
        help="group the failures in one or more JSONL reports by what went wrong, and exit: "
        "which single server-side change fixes the most steps",
    )
    parser.add_argument("--verbose", action="store_true", help="print the actor stderr on failure")
    args = parser.parse_args(argv)

    # Needs no API key and runs no model: it reads a report that already exists.
    if args.summarize:
        print(summarize_reports(list(args.summarize)))
        return 0

    if args.tool_search:
        os.environ["NEXTDNS_TOOL_SEARCH"] = args.tool_search

    # The step catalog needs no API key and runs no model: it is how a user
    # finds the ids and positions a --steps selection takes, and it states what
    # a selection that leaves out the profile lifecycle implies. A bad selection
    # is rejected here, before an expensive live run, with the catalog as the
    # remedy.
    cli_steps = build_steps(
        TaskTargets(profile_name=common._resolve_profile_name(args.profile_name or None), tag=_random_tag())
    )
    if args.print_steps:
        print(render_step_catalog(cli_steps))
        return 0
    selection = args.steps or ""
    if args.start_step or args.end_step:
        selection = ",".join([p for p in (selection, f"{args.start_step or 1}-{args.end_step or len(cli_steps)}") if p])
    if selection:
        try:
            chosen = select_steps(cli_steps, selection)
        except StepRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(render_step_catalog(cli_steps))
            return 2
        ids = ", ".join(s.id for _, s in chosen) if len(chosen) <= 12 else f"{chosen[0][1].id} .. {chosen[-1][1].id}"
        _progress(f"targeted selection: {len(chosen)} of {len(cli_steps)} steps ({ids})")

    # Load environment from a .env file if present (or the one given explicitly).
    if _load_dotenv is not None:
        for candidate in (_REPO_ROOT / ".env", Path.cwd() / ".env"):
            if candidate.exists():
                _load_dotenv(candidate, override=False)
        if args.env_file:
            _load_dotenv(args.env_file, override=True)

    api_key = os.environ.get("NEXTDNS_API_KEY")
    if not api_key:
        print("SKIP: NEXTDNS_API_KEY is not set — no live E2E performed.")
        print("The harness requires a real NextDNS API key to run the actor against the live server.")
        if args.report != "-":
            write_skip_report(args.report, "NEXTDNS_API_KEY not set")
            print(f"Report written to {args.report}")
        return 0

    config = HarnessConfig.load(args.config)
    if args.harness_cmd:
        config.command = _as_argv(args.harness_cmd)
    if args.model:
        config.model = args.model
    if args.profile_name:
        config.profile_name = args.profile_name
    if args.budget:
        config.timeout_s = args.budget
    if args.step_timeout:
        config.step_timeout_s = args.step_timeout
    config.steps = args.steps or ""
    config.start_step = args.start_step or ""
    config.end_step = args.end_step or ""
    config.verbose = bool(args.verbose)

    report = asyncio.run(run_harness(api_key, config))
    write_report(report, args.report, args.actor_log)
    if args.report != "-":
        print(f"Report written to {args.report}")
    if not args.quiet:
        print(render_text_report(report))
    if args.verbose and report.actor_stderr:
        print("--- actor stderr ---")
        print(report.actor_stderr)
        print("--- end stderr ---")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
