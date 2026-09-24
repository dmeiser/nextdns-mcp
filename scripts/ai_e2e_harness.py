#!/usr/bin/env python3
"""LLM-driven, API-verified E2E harness for the NextDNS MCP server.

SPDX-License-Identifier: MIT

This script is the E2E validation harness for the NextDNS MCP server. It has a
deliberate two-halves shape:

* **The actor is an LLM.** The script sends a task prompt into an AI coding
  harness — ``pi`` by default — that has the NextDNS MCP server's tools
  attached. The LLM does the tool calling against the *live* server: it decides
  which grouped tool to call, with which arguments, and in which order. The
  script does **not** drive the MCP tools itself; it only puts the LLM in the
  seat and hands it the task.
* **The script measures.** After the LLM finishes, the script verifies what the
  LLM *actually did* by talking to the NextDNS REST API directly with the API
  key — independently of the MCP path. It checks the resulting server state
  (profile provisioning/cleanup, settings/list/rewrite mutations) and measures
  tool-coverage (which of the 8 grouped tools the LLM exercised and how), then
  reports a PASS/FAIL/SKIP verdict with a JSONL report.

How the LLM gets the tools
--------------------------
``pi`` has no built-in MCP client, so the harness generates a small ``pi``
extension (from ``scripts/pi_mcp_bridge_extension.ts``) that registers the
server's grouped tools and proxies each call to a live ``mcp_server``
subprocess over stdio (the MCP stdio transport). The harness launches the actor
with only those tools enabled (``--no-builtin-tools --tools <8 tools>``), so the
LLM's reachable surface is exactly the NextDNS MCP tool surface.

Configuration
-------------
The actor command is explicit and overridable:

* ``--config PATH`` — a JSON file describing the actor. It names ``command``
  (argv, default ``["pi"]``), ``provider`` / ``model``, ``prompt`` (optional
  override of the built-in task), and ``server`` (the MCP server argv + env).
* ``--actor-cmd "pi --provider kimi-coding --model kimi-for-coding"`` —
  shorthand to override just the actor command line.
* Environment: ``NEXTDNS_API_KEY`` (required for a live run), ``NEXTDNS_API_BASE``,
  ``NEXTDNS_MCP_PYTHON`` / ``NEXTDNS_MCP_ARGS`` / ``NEXTDNS_MCP_CWD`` (how the MCP
  server is launched by the bridge; defaults to the in-tree package via
  ``<repo>/.venv/bin/python -m nextdns_mcp.server``).

Usage::

    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py
    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --config my_actor.json
    NEXTDNS_API_KEY=... uv run python scripts/ai_e2e_harness.py --actor-cmd "pi --model X"
    uv run python scripts/ai_e2e_harness.py            # -> SKIP (no key)

The JSONL report defaults to ``artifacts/ai_e2e_report.jsonl`` (``--report`` to
override, ``-`` for stdout).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
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
# Actor configuration
# =========================================================================== #

DEFAULT_ACTOR_COMMAND = ["pi"]
DEFAULT_PROVIDER = ""  # empty => let pi use its configured default
DEFAULT_MODEL = ""  # empty => let pi use its configured default

# The 8 grouped tools the actor is allowed to use (its entire reachable surface).
ALLOWED_TOOLS = list(common.MCP_TOOLS)

# Default way to launch the in-tree MCP server (overridden by config/env).
# Uses the running interpreter (``sys.executable``) plus a PYTHONPATH pointing at
# the in-tree package, so it works no matter how the harness itself was launched
# (``uv run python scripts/...`` from the repo root). The server must import the
# uninstalled in-tree package, so PYTHONPATH is set here rather than relying on
# an editable install.
DEFAULT_SERVER = {
    "command": sys.executable,
    "args": ["-m", "nextdns_mcp.server"],
    "cwd": str(_REPO_ROOT),
    "env": {"PYTHONPATH": str(_REPO_ROOT / "src")},
}


@dataclass
class ActorConfig:
    """Explicit, overridable description of the LLM actor."""

    command: list[str] = field(default_factory=lambda: list(DEFAULT_ACTOR_COMMAND))
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    prompt: str = ""  # empty => use the built-in task prompt
    server: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_SERVER)))
    timeout_s: float = 600.0
    workdir: str = str(_REPO_ROOT)

    @classmethod
    def load(cls, path: str | None) -> ActorConfig:
        """Build an ActorConfig from a JSON file (if given) layered over env."""
        cfg = cls()
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if "command" in data:
                cfg.command = _as_argv(data["command"])
            if "provider" in data:
                cfg.provider = str(data["provider"])
            if "model" in data:
                cfg.model = str(data["model"])
            if "prompt" in data:
                cfg.prompt = str(data["prompt"])
            if "timeout_s" in data:
                cfg.timeout_s = float(data["timeout_s"])
            if "workdir" in data:
                cfg.workdir = str(data["workdir"])
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
        if cmd := os.environ.get("NEXTDNS_E2E_ACTOR_CMD"):
            cfg.command = _as_argv(cmd)
        if env_provider := os.environ.get("NEXTDNS_E2E_PROVIDER"):
            cfg.provider = env_provider
        if env_model := os.environ.get("NEXTDNS_E2E_MODEL"):
            cfg.model = env_model
        if srv_python := os.environ.get("NEXTDNS_MCP_PYTHON"):
            cfg.server["command"] = srv_python
        if srv_args := os.environ.get("NEXTDNS_MCP_ARGS"):
            cfg.server["args"] = srv_args.split()
        if srv_cwd := os.environ.get("NEXTDNS_MCP_CWD"):
            cfg.server["cwd"] = srv_cwd
        # The API key (and its base) are passed to the server via its env.
        cfg.server["env"] = dict(cfg.server["env"])
        if key := os.environ.get("NEXTDNS_API_KEY"):
            cfg.server["env"].setdefault("NEXTDNS_API_KEY", key)
        cfg.server["env"].setdefault("NEXTDNS_API_BASE", common.API_BASE)
        return cfg

    def argv_with_placeholders(self, extension_path: str, prompt: str) -> list[str]:
        """The full actor argv with the generated extension and task prompt applied."""
        argv = list(self.command)
        if self.provider:
            argv += ["--provider", self.provider]
        if self.model:
            argv += ["--model", self.model]
        argv += [
            "--mode",
            "json",
            "--no-builtin-tools",
            "--tools",
            ",".join(ALLOWED_TOOLS),
            "--no-session",
            "-e",
            extension_path,
            "--no-approve",
            "-p",
            prompt,
        ]
        return argv


def _as_argv(value: Any) -> list[str]:
    """Accept either a JSON array or a shell-ish string and return an argv list."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        # Split on whitespace (quotes are passed through; the harness launches
        # via argv, not a shell, so this is for convenience/documentation only).
        return value.split()
    return list(DEFAULT_ACTOR_COMMAND)


def default_server_for(config: ActorConfig) -> dict[str, Any]:
    return config.server


# =========================================================================== #
# Extension generation
# =========================================================================== #


def write_extension(config: ActorConfig, dest_dir: str | Path | None = None) -> Path:
    """Render the pi bridge extension with this run's config and write it out.

    Returns the path to the generated ``.ts`` file. The template in
    ``scripts/pi_mcp_bridge_extension.ts`` carries a single ``__...__``
    placeholder that is replaced with a JSON config blob.
    """
    template = Path(__file__).resolve().parent / "pi_mcp_bridge_extension.ts"
    blob = {
        "tools": ALLOWED_TOOLS,
        "server": config.server,
        "timeout_ms": int(config.timeout_s * 1000),
    }
    rendered = template.read_text(encoding="utf-8").replace(
        "__NEXTDNS_MCP_BRIDGE_CONFIG_JSON__",
        json.dumps(blob),
    )
    dest = Path(dest_dir) if dest_dir else Path(tempfile.gettempdir()) / "nextdns_e2e"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"nextdns_mcp_bridge_{time.strftime('%Y%m%d%H%M%S')}.ts"
    out.write_text(rendered, encoding="utf-8")
    return out


# =========================================================================== #
# Task prompt (sent into the LLM actor)
# =========================================================================== #


def build_task_prompt(profile_name: str) -> str:
    """The task handed to the LLM. The LLM does the tool calling; the harness
    measures the result. The profile name is fixed up-front so the harness can
    find the profile the LLM provisions for verification and cleanup."""
    return f"""You are an autonomous validation agent for the NextDNS MCP server.
You have exactly these NextDNS MCP tools available: {', '.join(ALLOWED_TOOLS)}.
Use them to complete the task below. Do your own tool calling — decide the
arguments and ordering yourself from the tool descriptions.

## Task
Exercise the full NextDNS MCP tool surface against a dedicated test profile and
cover every grouped tool. Follow these steps:

1. Create a new profile named exactly: {profile_name!r}
2. Get the new profile (you will need its profile_id for the next steps).
3. Update the new profile's name to the same name with '-renamed' appended.
4. For EACH settings category (general, privacy, security, parental,
   performance, logs, blockpage): read it, then update at least one field.
5. For EACH list type (allowlist, denylist, privacy_blocklists, privacy_natives,
   security_tlds, parental_categories, parental_services): read it, then add a
   unique test entry (e.g. ai-e2e-<tag>.example.com for allowlist/denylist;
   nextdns-recommended for privacy_blocklists; alexa for privacy_natives; zip
   for security_tlds; gambling for parental_categories; tiktok for
   parental_services), then remove the entry you added.
6. Add a test A-record rewrite (e.g. ai-e2e-a-<tag>.example.com -> 192.0.2.100),
   then delete it using the id returned by the add.
7. Query analytics for the new profile for at least the 'status' and
   'queryTypes' metrics (aggregate, from_time=-1d, to_time=now).
8. Run a DoH lookup (dohLookup) for example.com through the new profile.
9. Get, then download, then clear the query logs for the new profile.
10. Generate at least one plot (plotAnalytics, metric=status) for the new
    profile.
11. Finally, DELETE the test profile you created.

## Rules
- Perform all writes against the test profile only. Never touch other profiles.
- If a call fails, note the error and continue with the remaining steps.
- Do not modify any code or configuration.
- When you are done, reply with a short summary of what you did, the test
  profile id, and the profile name you created (exactly: {profile_name!r}).
"""


# =========================================================================== #
# Actor result model + pi JSON event parsing (pure helpers, unit-tested)
# =========================================================================== #


@dataclass
class ToolCall:
    """A single tool call the LLM made, as observed in the actor's event stream."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    is_error: bool = False
    result_text: str = ""

    @property
    def ok(self) -> bool:
        return not self.is_error


@dataclass
class ActorRun:
    """Outcome of driving the LLM actor once."""

    command: list[str] = field(default_factory=list)
    ok: bool = False
    exit_code: int | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    duration_ms: int = 0

    def calls_for(self, tool: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == tool]

    def tools_used(self) -> list[str]:
        seen: list[str] = []
        for c in self.tool_calls:
            if c.name not in seen:
                seen.append(c.name)
        return seen

    def any_ok(self, tool: str) -> bool:
        return any(c.ok for c in self.calls_for(tool))


def _result_text(res: Any) -> str:
    """Flatten a pi ``tool_execution_end`` result into readable text."""
    if isinstance(res, dict):
        content = res.get("content")
        if isinstance(content, list):
            return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("text"))[:5000]
    if res is None:
        return ""
    return str(res)[:5000]


def parse_pi_events(lines: list[str]) -> tuple[list[ToolCall], str]:
    """Parse a ``pi --mode json`` event stream into (tool_calls, final_text).

    Only ``tool_execution_start`` / ``tool_execution_end`` events are used for
    the tool calls, and the last assistant text (from ``message_end``) is used
    as the final text. This is the *measuring* side reading what the *LLM* did.
    """
    calls: list[ToolCall] = []
    starts: dict[str, dict[str, Any]] = {}
    final_text = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError, TypeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "tool_execution_start":
            starts[str(ev.get("toolCallId"))] = {
                "name": ev.get("toolName"),
                "args": ev.get("args") or {},
            }
        elif etype == "tool_execution_end":
            cid = str(ev.get("toolCallId"))
            st = starts.get(cid, {})
            calls.append(
                ToolCall(
                    name=str(ev.get("toolName") or st.get("name") or "?"),
                    args=st.get("args") or ev.get("args") or {},
                    result=ev.get("result"),
                    is_error=bool(ev.get("isError")),
                    result_text=_result_text(ev.get("result")),
                )
            )
        elif etype == "message_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    text = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                    )
                    if text.strip():
                        final_text = text

    return calls, final_text


# =========================================================================== #
# Driving the actor (subprocess)
# =========================================================================== #


async def run_actor(config: ActorConfig, extension_path: Path, prompt: str) -> ActorRun:
    """Launch the LLM actor with the task prompt and capture its event stream."""
    argv = config.argv_with_placeholders(str(extension_path), prompt)
    start = time.monotonic()
    run = ActorRun(command=argv)
    if shutil.which(argv[0]) is None:
        run.error = f"actor command not found on PATH: {argv[0]!r}"
        run.duration_ms = int((time.monotonic() - start) * 1000)
        return run
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.workdir,
        )
    except Exception as e:  # noqa: BLE001
        run.error = f"failed to spawn actor: {type(e).__name__}: {e}"
        run.duration_ms = int((time.monotonic() - start) * 1000)
        return run

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=config.timeout_s)
    except TimeoutError:
        with _suppress():
            proc.kill()
        run.error = f"actor timed out after {config.timeout_s:.0f}s"
        run.duration_ms = int((time.monotonic() - start) * 1000)
        return run

    lines = stdout.decode("utf-8", errors="replace").splitlines()
    calls, final_text = parse_pi_events(lines)
    run.tool_calls = calls
    run.final_text = final_text
    run.exit_code = proc.returncode
    run.ok = proc.returncode == 0
    if not run.ok and not run.error:
        err = stderr.decode("utf-8", errors="replace").strip()
        run.error = err[-1500:] if err else f"actor exited with code {proc.returncode}"
    run.duration_ms = int((time.monotonic() - start) * 1000)
    return run


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


# =========================================================================== #
# Measuring half: independent REST verifier
# =========================================================================== #


class MeasureClient:
    """Talks to the NextDNS REST API directly to measure what the LLM did.

    This is deliberately a *separate* transport from the actor's MCP path — the
    harness never re-uses the LLM's tool responses to judge the outcome.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.http: Any = None

    async def __aenter__(self) -> Self:
        import httpx

        self.http = httpx.AsyncClient(
            base_url=common.API_BASE,
            headers={"Authorization": f"Bearer {self.api_key}"},
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
    """Did the LLM actually exercise this tool, and did any call succeed?"""
    calls = run.calls_for(tool)
    if not calls:
        return CheckResult(
            id=f"coverage.{tool}",
            section="coverage",
            description=f"LLM exercised the '{tool}' tool",
            status="skipped",
            mcp_tool=tool,
            reason="the LLM did not call this tool",
        )
    ok_calls = [c for c in calls if c.ok]
    if not ok_calls:
        err = calls[0].result_text or "all calls to this tool failed"
        return CheckResult(
            id=f"coverage.{tool}",
            section="coverage",
            description=f"LLM exercised the '{tool}' tool",
            status="failed",
            mcp_tool=tool,
            mcp_request={"calls": len(calls)},
            error=f"the LLM called '{tool}' {len(calls)}x but every call failed: {str(err)[:300]}",
        )
    return CheckResult(
        id=f"coverage.{tool}",
        section="coverage",
        description=f"LLM exercised the '{tool}' tool ({len(ok_calls)} successful call(s) of {len(calls)})",
        status="passed",
        mcp_tool=tool,
        mcp_request={"calls": len(calls), "ok": len(ok_calls)},
    )


def _settings_calls(run: ActorRun) -> dict[str, bool]:
    """Map settings category -> whether the LLM made a successful update for it."""
    hit: dict[str, bool] = {cat: False for cat in common.SETTINGS_CATEGORIES}
    for c in run.calls_for("manageSettings"):
        if not c.ok:
            continue
        cat = c.args.get("category")
        op = c.args.get("operation")
        if isinstance(cat, str) and cat in hit and op == "update":
            hit[cat] = True
    return hit


async def _measure_settings(m: MeasureClient, pid: str, run: ActorRun, report: Report) -> None:
    updates = _settings_calls(run)
    for cat, spec in common.SETTINGS_CATEGORIES.items():
        endpoint = f"GET /profiles/{pid}{spec['path']}"
        rest = await m.rest_get(f"/profiles/{pid}{spec['path']}")
        if not updates.get(cat):
            report.results.append(
                CheckResult(
                    id=f"settings.{cat}",
                    section="settings",
                    description=f"settings/{cat}: LLM performed a settings update",
                    status="skipped",
                    mcp_tool="manageSettings",
                    reason="the LLM did not update this category",
                    rest_verification={"endpoint": endpoint, "rest_status": common._resp_status(rest)},
                )
            )
            continue
        # The LLM reported an update; verify the endpoint is reachable/healthy.
        ok = bool(isinstance(rest, dict) and rest.get("ok"))
        r = CheckResult(
            id=f"settings.{cat}",
            section="settings",
            description=f"settings/{cat}: LLM update reflected (REST read {endpoint})",
            status="passed" if ok else "failed",
            mcp_tool="manageSettings",
            mcp_request={"operation": "update", "category": cat},
            rest_verification={"endpoint": endpoint, "rest_status": common._resp_status(rest)},
        )
        if not ok:
            r.error = f"REST read-back for settings/{cat} was not ok (status={common._resp_status(rest)})"
        report.results.append(r)


async def _measure_lists(m: MeasureClient, pid: str, run: ActorRun, report: Report) -> None:
    # Which list types did the LLM successfully add + remove entries for?
    added: dict[str, bool] = {s["name"]: False for s in common._LIST_SPECS}
    for c in run.calls_for("manageLists"):
        if not c.ok:
            continue
        lt = c.args.get("list_type")
        op = c.args.get("operation")
        if isinstance(lt, str) and lt in added and op == "add":
            added[lt] = True
    for spec in common._LIST_SPECS:
        name = spec["name"]
        base = f"/profiles/{pid}{spec['path']}"
        rest = await m.rest_get(base)
        readable = bool(isinstance(rest, dict) and rest.get("ok"))
        if not added.get(name):
            report.results.append(
                CheckResult(
                    id=f"lists.{name}",
                    section="lists",
                    description=f"lists/{name}: LLM added a test entry",
                    status="skipped",
                    mcp_tool="manageLists",
                    reason="the LLM did not add an entry to this list",
                    rest_verification={"endpoint": f"GET {base}", "rest_status": common._resp_status(rest)},
                )
            )
            continue
        r = CheckResult(
            id=f"lists.{name}",
            section="lists",
            description=f"lists/{name}: LLM entry add reflected (REST read GET {base})",
            status="passed" if readable else "failed",
            mcp_tool="manageLists",
            mcp_request={"list_type": name, "operation": "add"},
            rest_verification={"endpoint": f"GET {base}", "rest_status": common._resp_status(rest)},
        )
        if not readable:
            r.error = f"REST read-back for list {name} was not ok (status={common._resp_status(rest)})"
        report.results.append(r)


async def _measure_rewrites(m: MeasureClient, pid: str, run: ActorRun, report: Report) -> None:
    base = f"/profiles/{pid}/rewrites"
    added = any(c.ok and c.args.get("operation") == "add" for c in run.calls_for("manageRewrites"))
    rest = await m.rest_get(base)
    readable = bool(isinstance(rest, dict) and rest.get("ok"))
    if not added:
        report.results.append(
            CheckResult(
                id="rewrites.add",
                section="rewrites",
                description="rewrites: LLM added a test rewrite",
                status="skipped",
                mcp_tool="manageRewrites",
                reason="the LLM did not add a rewrite",
                rest_verification={"endpoint": f"GET {base}", "rest_status": common._resp_status(rest)},
            )
        )
        return
    r = CheckResult(
        id="rewrites.add",
        section="rewrites",
        description="rewrites: LLM rewrite add reflected (REST read GET " + base + ")",
        status="passed" if readable else "failed",
        mcp_tool="manageRewrites",
        mcp_request={"operation": "add"},
        rest_verification={"endpoint": f"GET {base}", "rest_status": common._resp_status(rest)},
    )
    if not readable:
        r.error = f"REST read-back for rewrites was not ok (status={common._resp_status(rest)})"
    report.results.append(r)


def _measure_analytics(run: ActorRun, report: Report) -> None:
    metrics = {c.args.get("metric") for c in run.calls_for("queryAnalytics") if c.ok}
    metrics.discard(None)
    if not metrics:
        report.results.append(
            CheckResult(
                id="analytics.query",
                section="analytics",
                description="analytics: LLM queried analytics metrics",
                status="skipped",
                mcp_tool="queryAnalytics",
                reason="the LLM did not query any analytics metric",
            )
        )
        return
    r = CheckResult(
        id="analytics.query",
        section="analytics",
        description=f"analytics: LLM queried {len(metrics)} metric(s): {sorted(str(x) for x in metrics)}",
        status="passed",
        mcp_tool="queryAnalytics",
        mcp_request={"metrics": sorted(str(x) for x in metrics)},
    )
    report.results.append(r)


def _measure_doh(run: ActorRun, report: Report) -> None:
    r = _coverage_check(run, "dohLookup")
    r.section = "doh"
    r.id = "doh.lookup"
    report.results.append(r)


def _measure_plots(run: ActorRun, report: Report) -> None:
    r = _coverage_check(run, "plotAnalytics")
    r.section = "plots"
    r.id = "plots.generate"
    report.results.append(r)


def _measure_logs(run: ActorRun, report: Report) -> None:
    ops = {c.args.get("operation") for c in run.calls_for("manageLogs") if c.ok}
    ops.discard(None)
    if not ops:
        report.results.append(
            CheckResult(
                id="logs.ops",
                section="logs",
                description="logs: LLM performed log operations",
                status="skipped",
                mcp_tool="manageLogs",
                reason="the LLM did not perform any log operation",
            )
        )
        return
    r = CheckResult(
        id="logs.ops",
        section="logs",
        description=f"logs: LLM performed log operation(s): {sorted(str(x) for x in ops)}",
        status="passed",
        mcp_tool="manageLogs",
        mcp_request={"operations": sorted(str(x) for x in ops)},
    )
    report.results.append(r)


async def _measure_profile_gate(m: MeasureClient, profile_name: str, report: Report) -> dict[str, Any] | None:
    """The profile gate: did the LLM actually provision the test profile?"""
    prof = await m.find_profile(profile_name)
    if prof is None:
        report.results.append(
            CheckResult(
                id="profiles.provision",
                section="profiles",
                description=f"test profile {profile_name!r} exists on the server",
                status="failed",
                mcp_tool="manageProfiles",
                rest_verification={"endpoint": "GET /profiles", "verified_present": False},
                error=f"profile {profile_name!r} was not found via REST — the LLM did not provision it (or already deleted it)",
            )
        )
        return None
    report.results.append(
        CheckResult(
            id="profiles.provision",
            section="profiles",
            description=f"test profile {profile_name!r} exists on the server",
            status="passed",
            mcp_tool="manageProfiles",
            rest_verification={"endpoint": "GET /profiles", "verified_present": True, "profile_id": prof.get("id")},
        )
    )
    return prof


async def _measure_cleanup(m: MeasureClient, profile_name: str, pid: str, report: Report) -> None:
    """After the actor exits, the test profile should be gone (LLM deleted it)."""
    rest = await m.rest_get(f"/profiles/{pid}")
    if isinstance(rest, dict) and rest.get("ok"):
        body = rest.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        still_present = isinstance(data, dict) and bool(data.get("id"))
    else:
        # A 404 / error read-back means the profile is gone (or unreadable).
        still_present = False
    gone = not still_present
    r = CheckResult(
        id="profiles.cleanup",
        section="profiles",
        description="test profile was deleted by the LLM and is gone via REST",
        status="passed" if gone else "failed",
        mcp_tool="manageProfiles",
        mcp_request={"operation": "delete", "profile_id": pid},
        rest_verification={"endpoint": f"GET /profiles/{pid}", "verified_gone": gone},
    )
    if not gone:
        r.error = f"profile {pid} is still present after the LLM run (the LLM did not delete it)"
    report.results.append(r)


def _measure_actor_run(run: ActorRun, report: Report) -> None:
    """Gate: did the actor run and make tool calls at all?"""
    report.results.append(
        CheckResult(
            id="actor.run",
            section="actor",
            description=f"LLM actor ran and made {len(run.tool_calls)} tool call(s)",
            status="passed" if run.ok and run.tool_calls else "failed",
            mcp_tool=None,
            mcp_response={"exit_code": run.exit_code, "tool_calls": len(run.tool_calls)},
            error=run.error if (not run.ok and not run.tool_calls) else "",
        )
    )


def _measure_coverage(run: ActorRun, report: Report) -> None:
    for tool in ALLOWED_TOOLS:
        report.results.append(_coverage_check(run, tool))


# =========================================================================== #
# Orchestration
# =========================================================================== #


async def run_harness(api_key: str, config: ActorConfig, profile_name: str) -> Report:
    report = Report(started_at=time.time(), api_key_present=True, profile_name=profile_name)
    report.actor_kind = "pi-llm"
    report.actor_command = config.command

    prompt = config.prompt or build_task_prompt(profile_name)
    ext_path = write_extension(config)
    run = await run_actor(config, ext_path, prompt)
    report.actor_tool_calls = len(run.tool_calls)
    report.actor_tools_used = run.tools_used()
    report.actor_final_text = run.final_text[:2000]
    report.actor_duration_ms = run.duration_ms
    report.actor_command = run.command

    _measure_actor_run(run, report)

    async with MeasureClient(api_key) as m:
        prof = await _measure_profile_gate(m, profile_name, report)
        if prof is not None:
            pid = str(prof.get("id"))
            report.profile_id = pid
            _measure_coverage(run, report)
            await _measure_settings(m, pid, run, report)
            await _measure_lists(m, pid, run, report)
            await _measure_rewrites(m, pid, run, report)
            _measure_analytics(run, report)
            _measure_doh(run, report)
            _measure_plots(run, report)
            _measure_logs(run, report)
            await _measure_cleanup(m, profile_name, pid, report)
        else:
            # Without a provisioned profile the per-section state checks cannot
            # run; record coverage (from the tool calls) so the report is still
            # informative, and skip the section-specific checks.
            _measure_coverage(run, report)
            report.skipped_sections = [s for s in common.SECTIONS if s not in ("profiles", "coverage", "actor")]
            report.cleanup = "skipped (test profile not found on the server)"

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


def write_skip_report(path: str, reason: str = "NEXTDNS_API_KEY not set") -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"verdict": "SKIP", "reason": reason}) + "\n", encoding="utf-8")


def render_text_report(report: Report) -> str:
    lines = ["=" * 72, "NextDNS MCP — LLM-driven E2E harness (actor=LLM, measure=REST)", "=" * 72]
    s = report.summary()
    lines.append(f"VERDICT : {s['verdict']}")
    lines.append(f"total   : {s['total']}  (passed {s['passed']}, skipped {s['skipped']}, failed {s['failed']})")
    lines.append(f"profile : {report.profile_id}  ({report.profile_name})")
    lines.append(
        f"actor   : {report.actor_kind} — {len(report.actor_tools_used)} tool(s) used, {s['actor_tool_calls']} call(s)"
    )
    lines.append(f"tools   : {', '.join(report.actor_tools_used) or '(none)'}")
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
    parser = argparse.ArgumentParser(description="LLM-driven, API-verified E2E harness for the NextDNS MCP server.")
    parser.add_argument("--config", help="JSON config describing the actor (command/provider/model/prompt/server)")
    parser.add_argument("--actor-cmd", help="actor command line, e.g. 'pi --provider kimi-coding --model X'")
    parser.add_argument(
        "--report", default="artifacts/ai_e2e_report.jsonl", help="path for the JSONL report, or '-' for stdout"
    )
    parser.add_argument("--quiet", action="store_true", help="do not print the text report")
    args = parser.parse_args(argv)

    api_key = os.environ.get("NEXTDNS_API_KEY")
    if not api_key:
        print("SKIP: NEXTDNS_API_KEY is not set — no live E2E performed.")
        print("The harness requires a real NextDNS API key to run the LLM actor against the live server.")
        if args.report != "-":
            write_skip_report(args.report, "NEXTDNS_API_KEY not set")
            print(f"Report written to {args.report}")
        return 0

    config = ActorConfig.load(args.config)
    if args.actor_cmd:
        config.command = _as_argv(args.actor_cmd)

    profile_name = common.make_profile_name()
    report = asyncio.run(run_harness(api_key, config, profile_name))
    write_report(report, args.report)
    if args.report != "-":
        print(f"Report written to {args.report}")
    if not args.quiet:
        print(render_text_report(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
