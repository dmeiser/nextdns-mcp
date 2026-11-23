"""Actor (harness) configuration for the NextDNS MCP E2E harness.

SPDX-License-Identifier: MIT

Everything the actor half of the run needs before a subprocess starts:

* the tool catalog the actor is measured against (``allowed_tools``, read from
  the shipped server rather than typed out) and the tool -> endpoint mapping
  (``_endpoint``, resolved from the shipped OpenAPI document);
* the default MCP-server launch description and harness command template, the
  placeholders both expand (``render_argv``), and the call-log filename the
  server writes and the harness reads back (``CALL_LOG_NAME``);
* :class:`HarnessConfig` — the explicit, overridable JSON/env description of a
  run — and the generated per-run MCP client config (``write_mcp_config``);
* the fail-closed profile-protection probe that denies the actor every
  pre-existing profile before the run may start, and the shared ``_progress``
  printer.

Split out of ``ai_e2e_harness.py``; that module re-exports these names so the
existing import paths (and the unit tests, which load it by file path) keep
working.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import ai_e2e_common as common
from ai_e2e_checklist import CALL_LOG_NAME

# --------------------------------------------------------------------------- #
# Make the in-tree ``nextdns_mcp`` package importable when running as a script
# (``uv run python scripts/ai_e2e_harness.py``) without needing an install.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[1]


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
