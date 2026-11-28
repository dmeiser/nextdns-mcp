"""The CLI entry point of the AI E2E harness.

The responsibilities of ``main`` (``scripts/ai_e2e_harness.py``) live here as
focused functions: the argument parser, the print-steps/selection stage, the
environment/config resolution and the live-run reporting. The harness module
keeps the thin delegator; this module turns argv into a report.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import ai_e2e_harness as harness
from dotenv import load_dotenv


def build_parser() -> argparse.ArgumentParser:
    """The harness's full argument parser."""
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
        choices=("off", "bm25"),
        help="override FastMCP progressive tool disclosure on the MCP server "
        "(sets NEXTDNS_TOOL_SEARCH; default bm25: tools/list exposes only "
        "search_tools + call_tool, off advertises the full catalog)",
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
    parser.add_argument(
        "--codemode",
        choices=("on", "off"),
        default="off",
        help=(
            "opencode Code Mode (default: off). 'on' restores opencode's own default, where the "
            "model's only native tool is `execute` and this server's tools are JavaScript bindings "
            "inside it; 'off' registers them natively as nextdns_<tool>, so no client-side JS "
            "evaluation sits between the model and the contract."
        ),
    )
    return parser


def _print_steps_or_select(args: Any, cli_steps: list[Any]) -> int | None:
    """The print-steps / selection stage. Returns an exit code, or None to continue."""
    # The step catalog needs no API key and runs no model: it is how a user
    # finds the ids and positions a --steps selection takes, and it states what
    # a selection that leaves out the profile lifecycle implies. A bad selection
    # is rejected here, before an expensive live run, with the catalog as the
    # remedy.
    if args.print_steps:
        print(harness.render_step_catalog(cli_steps))
        return 0
    selection = args.steps or ""
    if args.start_step or args.end_step:
        selection = ",".join([p for p in (selection, f"{args.start_step or 1}-{args.end_step or len(cli_steps)}") if p])
    if selection:
        try:
            chosen = harness.select_steps(cli_steps, selection)
        except harness.StepRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(harness.render_step_catalog(cli_steps))
            return 2
        ids = ", ".join(s.id for _, s in chosen) if len(chosen) <= 12 else f"{chosen[0][1].id} .. {chosen[-1][1].id}"
        harness._progress(f"targeted selection: {len(chosen)} of {len(cli_steps)} steps ({ids})")
    return None


def _load_dotenv_file(args: Any) -> None:
    """Load the .env file if present (or the one given explicitly)."""
    for candidate in (harness._REPO_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
    if args.env_file:
        load_dotenv(args.env_file, override=True)


def _config_from_args(args: Any) -> tuple[Any, int | None]:
    """Build the HarnessConfig from the CLI args; (config, None) or (None, code)."""
    try:
        config = harness.HarnessConfig.load(args.config)
    except (ValueError, harness.HarnessAbortError) as exc:
        # A plaintext http base (or any other invalid base) is refused before
        # any request carries the key, and a failed profile-protection probe
        # aborts rather than running unprotected; there is nothing to run.
        print(f"error: {exc}", file=sys.stderr)
        return None, 2
    if args.harness_cmd:
        config.command = harness._as_argv(args.harness_cmd)
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
    config.codemode = args.codemode == "on"
    return config, None


def _finalize(args: Any, report: Any) -> int:
    """Write the report, print it, and return the exit code."""
    harness.write_report(report, args.report, args.actor_log)
    if args.report != "-":
        print(f"Report written to {args.report}")
    if not args.quiet:
        print(harness.render_text_report(report))
    if args.verbose and report.actor_stderr:
        print("--- actor stderr ---")
        print(report.actor_stderr)
        print("--- end stderr ---")
    # A skipped step certifies nothing: it is neither a pass nor a failure,
    # and treating it as neither keeps "the suite is green" meaning every
    # published behaviour actually ran. (Targeted --steps runs select their
    # own subset; steps INSIDE the selection that skip still fail the exit.)
    summary = report.summary()
    if summary["failed"]:
        return 1
    if summary["skipped"]:
        print(f"{summary['skipped']} step(s) skipped; steps that did not run do not certify the tool they stand for")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Turn argv into a report: parse, resolve, load, run, report."""
    args = build_parser().parse_args(argv)

    # Needs no API key and runs no model: it reads a report that already exists.
    if args.summarize:
        print(harness.summarize_reports(list(args.summarize)))
        return 0

    if args.tool_search:
        os.environ["NEXTDNS_TOOL_SEARCH"] = args.tool_search

    cli_steps = harness.build_steps(
        harness.TaskTargets(
            profile_name=harness.common._resolve_profile_name(args.profile_name or None), tag=harness._random_tag()
        )
    )
    exit_code = _print_steps_or_select(args, cli_steps)
    if exit_code is not None:
        return exit_code

    # Load environment from a .env file if present (or the one given explicitly).
    _load_dotenv_file(args)

    api_key = os.environ.get("NEXTDNS_API_KEY")
    if not api_key:
        print("SKIP: NEXTDNS_API_KEY is not set — no live E2E performed.")
        print("The harness requires a real NextDNS API key to run the actor against the live server.")
        if args.report != "-":
            harness.write_skip_report(args.report, "NEXTDNS_API_KEY not set")
            print(f"Report written to {args.report}")
        return 0

    config, error_code = _config_from_args(args)
    if error_code is not None:
        return error_code

    try:
        report = asyncio.run(harness.run_harness(api_key, config))
    except ValueError as exc:
        # Same refusal class as config load: the measuring client also resolves
        # the base at run time, and a late discovery must fail as loudly.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return _finalize(args, report)
