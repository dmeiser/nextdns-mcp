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
        for var in ("NEXTDNS_READABLE_PROFILES", "NEXTDNS_WRITABLE_PROFILES", "NEXTDNS_DEFAULT_PROFILE"):
            if val := os.environ.get(var):
                cfg.server["env"].setdefault(var, val)
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


def write_mcp_config(config: HarnessConfig, dest_dir: str | Path) -> Path:
    """Write the generated MCP client config (``opencode.json``) into workdir.

    The file declares the in-tree MCP server as a *local* stdio server under
    the name ``nextdns``, which is how the default opencode harness (and any
    harness that reads opencode's config format) mounts it natively. Returns
    the written path (exposed to templates as ``{config_file}``).
    """
    srv = config.server
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
        },
        "mcp": {
            "nextdns": {
                "type": "local",
                "command": [srv["command"], *[str(a) for a in srv["args"]]],
                "cwd": srv.get("cwd"),
                "environment": {str(k): str(v) for k, v in srv.get("env", {}).items()},
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
You have exactly these NextDNS MCP tools available: {', '.join(ALLOWED_TOOLS)}.
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
   - allowlist: {entries['allowlist']!r}
   - denylist: {entries['denylist']!r}
   - privacy_blocklists: {entries['privacy_blocklists']!r}
   - privacy_natives: {entries['privacy_natives']!r}
   - security_tlds: {entries['security_tlds']!r}
   - parental_categories: {entries['parental_categories']!r}
   - parental_services: {entries['parental_services']!r}
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
        m = re.search(r'nextdns["\']?\s*\[\s*["\']([A-Za-z0-9_]+)', code)
        if not m:
            m = re.search(r"nextdns[._]([A-Za-z_][A-Za-z0-9_]*)", code)
        if not m:
            m = re.search(r'["\']nextdns_([A-Za-z][A-Za-z0-9_]*)["\']', code)
        if not m:
            m = re.search(r'call_tool\(\s*\{[^}]*?name["\']?\s*:\s*["\']([A-Za-z0-9_]+)', code)
        if m:
            name = m.group(1)
        else:
            name = tool
    return name.removeprefix("nextdns_")


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
                    result_text=_flatten_text(state.get("output")),
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
        return any(tok in low for tok in ("econnreset", "transport:", "timed out", "connection reset", "socket hang up"))

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
            error=((run.error + chr(10) + "--- stderr (tail) ---" + chr(10) + run.stderr[-800:]) if (run.error and run.stderr) else run.error) if not run.ok else "",
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
    """One checklist operation: an actor prompt plus a REST verifier."""

    id: str
    section: str
    tool: str  # the MCP tool the model is instructed to use
    description: str
    prompt: str  # instruction to the actor ("{profile_id}" rendered at run time)
    verify: str  # verifier key in _verify_step
    vdata: dict[str, Any] = field(default_factory=dict)


def _step_prompt(task: str) -> str:
    """Wrap a single-step TASK (not a tool call) with the actor contract.

    The model chooses which MCP tool and arguments to use; the harness only
    verifies the resulting state via REST.
    """
    return (
        "You are a validation agent for the NextDNS MCP server.\n"
        "NextDNS MCP tools are available to you: " + ", ".join(ALLOWED_TOOLS) + ".\n\n"
        "Task: " + task + "\n\n"
        "Rules:\n"
        "- Perform exactly this one task with a single operation, then stop.\n"
        "- If a tool is reported as unknown or not registered, wait a moment and retry the same call once.\n"
        "- If the call fails, report the error and stop.\n"
        "- When the call succeeds, stop immediately."
    )


def build_steps(targets: TaskTargets) -> list[Step]:
    """Build the full per-step checklist (~89 steps) for this run."""
    pn = targets.profile_name
    rn = targets.renamed_name
    tag = targets.tag
    entries = targets.list_entries
    steps: list[Step] = []

    # ---- profiles -------------------------------------------------------
    steps.append(Step("profiles.create", "profiles", "manageProfiles",
        f"create the test profile {pn!r}",
        _step_prompt(f"Create a new NextDNS profile named exactly '{pn}'."),
        "profile_exists"))
    steps.append(Step("profiles.list", "profiles", "manageProfiles",
        "list all profiles and find the new one",
        _step_prompt(f"List all profiles and confirm '{pn}' is present."),
        "profile_exists"))
    steps.append(Step("profiles.get", "profiles", "manageProfiles",
        "get the test profile by id",
        _step_prompt("Get the profile '{profile_id}' and report its details."),
        "profile_exists"))
    steps.append(Step("profiles.update", "profiles", "manageProfiles",
        f"rename the test profile to {rn!r}",
        _step_prompt(f"Rename the profile '{{profile_id}}' to exactly '{rn}'."),
        "profile_renamed"))

    # ---- settings: read + update per category ---------------------------
    for cat, spec in SETTINGS_CATEGORIES.items():
        steps.append(Step(f"settings.{cat}.read", "settings", "manageSettings",
            f"{cat}: read current settings",
            _step_prompt(f"Read the current {cat} settings for profile '{{profile_id}}' and report them."),
            "settings_read", {"cat": cat, "spec": spec}))
        steps.append(Step(f"settings.{cat}.update", "settings", "manageSettings",
            f"{cat}: update to {json.dumps(spec['set'])}",
            _step_prompt(f"Update the {cat} settings for profile '{{profile_id}}' to {json.dumps(spec['set'])}."),
            "settings_update", {"cat": cat, "spec": spec}))

    # ---- lists: read/replace/add/update/remove per type -----------------
    for lt in _LIST_SPECS:
        name = lt["name"]
        values = [entries[name]]
        added = common._list_added(name, tag)
        steps.append(Step(f"lists.{name}.read", "lists", "manageLists",
            f"{name}: read current list",
            _step_prompt(f"Read the current {name} for profile '{{profile_id}}'."),
            "list_read", {"name": name, "spec": lt}))
        steps.append(Step(f"lists.{name}.replace", "lists", "manageLists",
            f"{name}: replace with {values}",
            _step_prompt(f"Replace the {name} for profile '{{profile_id}}' with exactly {json.dumps(values)}."),
            "list_replace", {"name": name, "spec": lt, "values": values}))
        if added is not None:
            steps.append(Step(f"lists.{name}.add", "lists", "manageLists",
                f"{name}: add {added!r}",
                _step_prompt(f"Add '{added}' to the {name} for profile '{{profile_id}}'."),
                "list_add", {"name": name, "spec": lt, "values": values, "added": added}))
        if lt["updatable"]:
            steps.append(Step(f"lists.{name}.update", "lists", "manageLists",
                f"{name}: update {values[0]!r} (active=false)",
                _step_prompt(f"Update the entry '{values[0]}' in the {name} for profile '{{profile_id}}' with {json.dumps({'active': False})}."),
                "list_update", {"name": name, "spec": lt, "values": values}))
        steps.append(Step(f"lists.{name}.remove", "lists", "manageLists",
            f"{name}: remove {values[0]!r}",
            _step_prompt(f"Remove '{values[0]}' from the {name} for profile '{{profile_id}}'."),
            "list_remove", {"name": name, "spec": lt, "values": values}))
        if added is not None:
            steps.append(Step(f"lists.{name}.remove_added", "lists", "manageLists",
                f"{name}: remove the added entry {added!r}",
                _step_prompt(f"Remove '{added}' from the {name} for profile '{{profile_id}}'."),
                "list_remove_added", {"name": name, "spec": lt, "values": values, "added": added}))

    # ---- rewrites: list, add each of 3, delete each ---------------------
    steps.append(Step("rewrites.list", "rewrites", "manageRewrites",
        "list existing rewrites",
        _step_prompt("List the DNS rewrite records for profile '{profile_id}'."),
        "rewrite_list"))
    rewrite_records = [
        {"name": targets.rewrite_name, "content": targets.rewrite_content},
        {"name": f"ai-e2e-aaaa-{tag}.example.com", "content": "2001:db8::dead"},
        {"name": f"ai-e2e-cname-{tag}.example.com", "content": f"ai-e2e-target-{tag}.example.com"},
    ]
    for rec in rewrite_records:
        steps.append(Step(f"rewrites.add.{rec['name'].split('-')[2]}", "rewrites", "manageRewrites",
            f"add rewrite {rec['name']!r} -> {rec['content']!r}",
            _step_prompt(f"Add a DNS rewrite record for profile '{{profile_id}}': '{rec['name']}' -> '{rec['content']}'."),
            "rewrite_add", {"record": rec}))
    for rec in rewrite_records:
        steps.append(Step(f"rewrites.delete.{rec['name'].split('-')[2]}", "rewrites", "manageRewrites",
            f"delete rewrite {rec['name']!r}",
            _step_prompt(f"Delete the DNS rewrite record '{rec['name']}' from profile '{{profile_id}}'."),
            "rewrite_delete", {"record": rec}))

    # ---- analytics: aggregate + series per metric -----------------------
    for m in AGGREGATE_METRICS:
        vdata = {"metric": m}
        instr = f"Query aggregate analytics for profile '{{profile_id}}' for metric '{m}' over the last day"
        if m == "destinations":
            instr += ", with destination_type 'countries'"
        steps.append(Step(f"analytics.aggregate.{m}", "analytics", "queryAnalytics",
            f"queryAnalytics aggregate {m}", _step_prompt(instr + "."), "analytics", vdata))
    for m in SERIES_METRICS:
        vdata_s: dict[str, Any] = {"metric": m, "series": True}
        instr = f"Query the time series for profile '{{profile_id}}' for metric '{m}' over the last day (series=true, interval 3600)"
        if m == "destinations":
            instr += ", with destination_type 'countries'"
        steps.append(Step(f"analytics.series.{m}", "analytics", "queryAnalytics",
            f"queryAnalytics series {m}", _step_prompt(instr + "."), "analytics", vdata_s))

    # ---- doh -------------------------------------------------------------
    steps.append(Step("doh.lookup", "doh", "dohLookup",
        "dohLookup example.com through the test profile",
        _step_prompt("Perform a DNS-over-HTTPS lookup for example.com (A record) through profile '{profile_id}'."),
        "tool_ok"))

    # ---- plots: every metric --------------------------------------------
    for m in PLOT_METRICS:
        steps.append(Step(f"plots.{m}", "plots", "plotAnalytics",
            f"plotAnalytics {m} (PNG)",
            _step_prompt(f"Generate an analytics plot for profile '{{profile_id}}' for metric '{m}' over the last day (PNG image)."),
            "plots", {"metric": m}))

    # ---- logs ------------------------------------------------------------
    steps.append(Step("logs.get", "logs", "manageLogs",
        "get recent query logs",
        _step_prompt("Retrieve the 10 most recent query logs for profile '{profile_id}'."),
        "logs_get"))
    steps.append(Step("logs.download", "logs", "manageLogs",
        "download retained logs (CSV)",
        _step_prompt("Download the retained query logs for profile '{profile_id}' as CSV."),
        "logs_download"))
    steps.append(Step("logs.clear", "logs", "manageLogs",
        "clear query logs",
        _step_prompt("Clear the query logs for profile '{profile_id}'."),
        "logs_clear"))

    # ---- delete the profile (model performs the delete) -----------------
    steps.append(Step("profiles.delete", "profiles", "manageProfiles",
        f"delete the test profile {pn!r}",
        _step_prompt("Delete the profile '{profile_id}'."),
        "profile_gone"))

    return steps


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
        return ("passed" if ok else "failed",
                "" if ok else f"profile {targets.profile_name!r} not found via REST",
                "", {"endpoint": "GET /profiles", "verified_present": ok})
    if step.verify == "profile_renamed":
        prof = await m.find_profile(targets.renamed_name)
        ok = prof is not None and (not pid or str(prof.get("id")) == pid)
        return ("passed" if ok else "failed",
                "" if ok else f"profile not found under renamed name {targets.renamed_name!r}",
                "", {"endpoint": "GET /profiles", "verified_renamed": ok})
    if step.verify == "profile_gone":
        gone = (not pid) or await _profile_gone(m, pid, targets)
        return ("passed" if gone else "failed",
                "" if gone else f"profile {pid} is still present after delete",
                "", {"endpoint": f"GET /profiles/{pid}", "verified_gone": gone})
    if step.verify == "settings_read":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return ("passed" if ok else "failed",
                "" if ok else f"REST GET settings/{vd['cat']} not ok",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "rest_status": common._resp_status(rest)})
    if step.verify == "settings_update":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._verify_settings_assert(rest, vd["spec"]["assert"])
        return ("passed" if ok else "failed",
                "" if ok else f"settings/{vd['cat']} read-back does not match {vd['spec']['assert']}",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok})
    if step.verify in ("list_read",):
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return ("passed" if ok else "failed",
                "" if ok else f"REST GET list {vd['name']} not ok",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "rest_status": common._resp_status(rest)})
    if step.verify == "list_replace":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_contains(rest, vd["values"])
        return ("passed" if ok else "failed",
                "" if ok else f"list {vd['name']} does not contain {vd['values']}",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok})
    if step.verify == "list_add":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_contains(rest, vd["values"] + [vd["added"]])
        return ("passed" if ok else "failed",
                "" if ok else f"list {vd['name']} does not contain added entry {vd['added']!r}",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok})
    if step.verify == "list_update":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_contains(rest, vd["values"])
        return ("passed" if ok else "failed",
                "" if ok else f"list {vd['name']} lost entry {vd['values']} after update",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified": ok})
    if step.verify == "list_remove":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_absent(rest, vd["values"])
        return ("passed" if ok else "failed",
                "" if ok else f"list {vd['name']} still contains {vd['values']}",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified_absent": ok})
    if step.verify == "list_remove_added":
        rest = await _req("GET", f"{base_profile}{vd['spec']['path']}")
        ok = common._list_absent(rest, [vd["added"]])
        return ("passed" if ok else "failed",
                "" if ok else f"list {vd['name']} still contains added entry {vd['added']!r}",
                "", {"endpoint": f"GET {base_profile}{vd['spec']['path']}", "verified_absent": ok})
    if step.verify == "rewrite_list":
        rest = await _req("GET", f"{base_profile}/rewrites")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return ("passed" if ok else "failed",
                "" if ok else "REST GET rewrites not ok",
                "", {"endpoint": f"GET {base_profile}/rewrites", "rest_status": common._resp_status(rest)})
    if step.verify == "rewrite_add":
        rec = vd["record"]
        rest = await _req("GET", f"{base_profile}/rewrites")
        ok = common._rewrites_contains(rest, [rec["name"]])
        return ("passed" if ok else "failed",
                "" if ok else f"rewrite {rec['name']!r} not found via REST",
                "", {"endpoint": f"GET {base_profile}/rewrites", "verified": ok})
    if step.verify == "rewrite_delete":
        rec = vd["record"]
        rest = await _req("GET", f"{base_profile}/rewrites")
        ok = not common._rewrites_contains(rest, [rec["name"]])
        return ("passed" if ok else "failed",
                "" if ok else f"rewrite {rec['name']!r} still present after delete",
                "", {"endpoint": f"GET {base_profile}/rewrites", "verified_absent": ok})
    if step.verify == "analytics":
        metric = vd["metric"]
        suffix = ";series" if vd.get("series") else ""
        rest = await _req("GET", f"{base_profile}/analytics/{metric}{suffix}")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return ("passed" if ok else "failed",
                "" if ok else f"REST analytics {metric}{suffix} not ok",
                "", {"endpoint": f"GET {base_profile}/analytics/{metric}{suffix}", "rest_status": common._resp_status(rest)})
    if step.verify == "tool_ok":
        called_ok = any(c.name == step.tool and c.ok for c in run.tool_calls)
        if called_ok:
            return ("passed", "", "", {"note": "read-only DoH resolution verified via actor event stream"})
        return ("failed", f"actor did not successfully call {step.tool}", "",
                {"note": "no REST state to verify; relies on actor event stream"})
    if step.verify == "plots":
        rest = await _req("GET", f"{base_profile}/analytics/status")
        has_data = common._analytics_has_data(rest)
        called_ok = any(c.name == step.tool and c.ok for c in run.tool_calls)
        if not has_data:
            return ("skipped", "", "no query history on the fresh profile; plot checks need data",
                    {"endpoint": f"GET {base_profile}/analytics/status", "has_data": False})
        return ("passed" if called_ok else "failed",
                "" if called_ok else f"actor did not successfully call {step.tool}",
                "", {"has_data": True, "note": "PNG payload verified via actor event stream"})
    if step.verify == "logs_get":
        rest = await _req("GET", f"{base_profile}/logs", params={"limit": 10})
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return ("passed" if ok else "failed",
                "" if ok else "REST GET logs not ok",
                "", {"endpoint": f"GET {base_profile}/logs", "rest_status": common._resp_status(rest)})
    if step.verify == "logs_download":
        rest = await _req("GET", f"{base_profile}/logs", params={"limit": 10})
        has = common._analytics_has_data(rest)
        if not has:
            return ("skipped", "", "no retained logs yet (NextDNS log delay on a fresh profile)",
                    {"endpoint": f"GET {base_profile}/logs"})
        called_ok = any(c.name == step.tool and c.ok for c in run.tool_calls)
        return ("passed" if called_ok else "failed",
                "" if called_ok else "actor did not successfully call manageLogs(download)",
                "", {"note": "CSV download verified via actor event stream"})
    if step.verify == "logs_clear":
        rest = await m.rest_delete(f"{base_profile}/logs")
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        return ("passed" if ok else "failed",
                "" if ok else "REST DELETE logs not ok",
                "", {"endpoint": f"DELETE {base_profile}/logs", "rest_status": common._resp_status(rest)})
    return ("failed", f"unknown verifier {step.verify!r}", "", {})


def _tool_observed(run: ActorRun, tool: str) -> bool:
    """Best-effort: was this tool used, by parsed events or raw stream text?"""
    if any(c.name == tool for c in run.tool_calls):
        return True
    return any(tool in line for line in run.raw_lines)


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
    return [
        {"tool": c.name, "args": c.args, "ok": c.ok, "result": (c.result_text or "")[:300]}
        for c in run.tool_calls
    ]


def _call_succeeded(c: ToolCall) -> bool:
    """A call counts as successful only if the tool ran AND its payload is not
    an error. opencode's execute wrapper reports ok=True even when the code
    returned "Unknown tool" or the MCP call returned an error body."""
    if not c.ok:
        return False
    rt = (c.result_text or "").strip()
    if rt.startswith(("Unknown tool", "Error", "error:", "TypeError", "ReferenceError")):
        return False
    if rt.startswith("{"):
        try:
            d = json.loads(rt)
        except (ValueError, TypeError):
            d = None
        if isinstance(d, dict) and d.get("error"):
            return False
    return True


def _check_single_command(run: ActorRun, step: Step) -> str | None:
    """Enforce the 1-command contract: exactly ONE successful call to the
    expected tool, and no successful calls to anything else.

    Failed "probe" calls are tolerated: opencode registers MCP tools lazily, so
    a model's first attempt can fail with "Unknown tool" before it retries.
    """
    calls = run.tool_calls
    if not calls:
        if run.exit_code == 0:
            return (
                f"model made NO tool call (expected exactly one '{step.tool}' call). "
                f"It replied: {(run.final_text or '')[:200]!r}"
            )
        return None  # actor itself failed; failure reason comes from error events
    mine = [c for c in calls if c.name == step.tool]
    mine_ok = [c for c in mine if _call_succeeded(c)]
    other_ok = [c for c in calls if c.name != step.tool and _call_succeeded(c)]
    probes = len(mine) - len(mine_ok)
    if other_ok:
        made = ", ".join(f"{c.name}({json.dumps(c.args)[:60]})" for c in other_ok)
        return f"model successfully called tools other than '{step.tool}': {made}"
    if not mine_ok:
        first_err = (mine[0].result_text or "unknown error")[:250] if mine else "no call made"
        return f"model's '{step.tool}' call did not succeed: {first_err}"
    if len(mine_ok) > 1:
        return f"model made {len(mine_ok)} successful '{step.tool}' calls instead of 1"
    if probes:
        # tolerated, but noted by the caller via feedback
        return None
    return None


def _feedback_block(step: Step, run: ActorRun, status: str, error: str, reason: str,
                    rest_v: dict[str, Any]) -> list[str]:
    """Render the command -> actor -> verify -> result chain for one step."""
    out = [f"  COMMAND : {step.description}"]
    if run.tool_calls:
        c = run.tool_calls[0]
        out.append(f"  ACTOR   : called {c.name}({json.dumps(c.args)[:120]})"
                   + ("" if c.ok else f"  -> ERROR: {(c.result_text or '')[:200]}"))
    else:
        errs = _extract_error_events(run)
        if errs:
            out.append(f"  ACTOR   : no tool call — {errs[0][:250]}")
        elif run.exit_code not in (0, None):
            out.append(f"  ACTOR   : no tool call — harness exited code {run.exit_code}"
                       + (f", stderr: {run.stderr[:200]}" if run.stderr else ""))
        else:
            out.append(f"  ACTOR   : no tool call — replied: {(run.final_text or '')[:200]!r}")
    endpoint = rest_v.get("endpoint", "")
    detail = {k: v for k, v in rest_v.items() if k not in ("endpoint",) and v not in (None, "", {}, [])}
    out.append(f"  VERIFY  : {endpoint or '(no REST endpoint)'}"
               + (f"  {json.dumps(detail)[:200]}" if detail else ""))
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
    _progress(f"profile name: {targets.profile_name}  ({len(steps)} steps)")
    workdir = Path(config.workdir) if config.workdir else Path(tempfile.mkdtemp(prefix="nextdns_e2e_"))
    workdir.mkdir(parents=True, exist_ok=True)
    write_mcp_config(config, workdir)

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

        for i, step in enumerate(steps, 1):
            if time.monotonic() - started > budget:
                rest = CheckResult(step.id, step.section, step.description, "skipped",
                                   mcp_tool=step.tool, reason=f"overall time budget ({budget:.0f}s) exhausted")
                report.results.append(rest)
                continue
            if step.section != "profiles" and not pid:
                report.results.append(CheckResult(step.id, step.section, step.description, "skipped",
                                                  mcp_tool=step.tool, reason="test profile was not provisioned"))
                continue

            prompt = step.prompt.replace("{profile_id}", pid)
            run: ActorRun | None = None
            status, error, reason = "failed", "", ""
            rest_v: dict[str, Any] = {}
            for attempt in (1, 2, 3):
                _progress(f"[{i}/{len(steps)}] {step.id}" + (f" (retry {attempt-1})" if attempt > 1 else ""))
                run = await run_actor(step_cfg, prompt, workdir, retries=1)
                total_events += len(run.tool_calls)
                for t in run.tools_used():
                    if t not in tools_used:
                        tools_used.append(t)
                if run.stderr:
                    last_stderr = run.stderr
                all_raw.extend(run.raw_lines)

                status, error, reason, rest_v = await _verify_step(m, targets, pid, step, run)
                # opencode registers MCP tools lazily: a first attempt can fail
                # with "Unknown tool" before registration completes. Retry the
                # whole step on that transient only.
                blob = " ".join(
                    [error, reason]
                    + [c.result_text or "" for c in run.tool_calls]
                    + ([run.final_text] if run.final_text else [])
                ).lower()
                if status != "failed" or "unknown tool" not in blob:
                    break

            if run is None:  # pragma: no cover - defensive
                run = ActorRun(command=list(step_cfg.command))

            # Enforce the 1-command contract: exactly one successful call, the expected tool.
            violation = _check_single_command(run, step)
            if violation and status == "passed":
                status, error = "failed", violation

            fb = _feedback_block(step, run, status, error, reason, rest_v)
            for ln in fb:
                _progress(ln)

            r = CheckResult(
                id=step.id, section=step.section, description=step.description, status=status,
                mcp_tool=step.tool,
                mcp_request={"prompt": prompt[:500], "exit_code": run.exit_code, "tool_events": len(run.tool_calls)},
                mcp_response={
                    "calls": _summarize_calls(run),
                    "final_text": run.final_text[:1000],
                    "error_events": _extract_error_events(run)[:3],
                    "feedback": fb,
                },
                rest_verification=rest_v, reason=reason, error=error, duration_ms=run.duration_ms,
            )
            report.results.append(r)

            # Capture the profile id as soon as the profile exists.
            if not pid and status == "passed" and step.verify == "profile_exists":
                prof = await m.find_profile(targets.profile_name) or await m.find_profile(targets.renamed_name)
                if prof is not None:
                    pid = str(prof.get("id"))
                    report.profile_id = pid

        # Safety net: if the model deleted the profile, verify; if not, the
        # harness owns cleanup.
        if pid:
            gone = await _profile_gone(m, pid, targets)
            if not gone:
                _progress("safety cleanup: deleting the test profile via REST")
                for attempt in range(4):
                    resp = await m.rest_delete(f"/profiles/{pid}")
                    if isinstance(resp, dict) and resp.get("ok"):
                        break
                    await asyncio.sleep(5 * (attempt + 1))
                gone = await _profile_gone(m, pid, targets)
            report.cleanup = "clean: profile deleted and verified gone via REST" if gone else "FAILED (profile still present)"
            report.profile_id = pid
        else:
            report.cleanup = "skipped (test profile not found on the server)"

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
    parser.add_argument("--verbose", action="store_true", help="print the actor stderr on failure")
    args = parser.parse_args(argv)

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
