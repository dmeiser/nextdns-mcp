#!/usr/bin/env python3
"""Gate: a pull request whose entire diff is comments has to justify itself.

Why this exists: a two-file pull request of pure narration passed six green
checks in the sibling app repository, because every gate asked "does it still
work?" and none asked "does this comment need to exist?". This script asks the
one question a machine can answer about a comment — whether the diff changed
anything but comments — and stops there. Whether a surviving comment answers
*why* rather than *what* is a review judgement, so the scan is syntactic by
design: no prose parser, no LLM.

The label is the author's assertion; the pull request body is where the reader-
facing claim is stated for the reviewer, and nothing here reads it. The label
check fails closed: no `comment-only` label means rejection whatever the body
says, because a gate whose exception cannot be taken rejects every legitimate
comment fix.

Usage:
    python scripts/check_comment_only_diff.py --base <sha> --head <sha> --labels "a,b"

Exit codes:
    0  the diff changes non-comment content, or it is comment-only and labelled
    1  the diff is comment-only without the `comment-only` label, or it cannot be read
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

COMMENT_ONLY_LABEL = "comment-only"

# Quoted verbatim from the `## Comments` section of AGENT.md, so the rejection
# itself teaches the rule instead of only denying the merge.
RULE = "A comment may exist only to answer: why does this particular confusing piece of code exist, and what problem was it meant to solve?"
DELETION_TEST = "Delete the comment. Does the next reader now do something wrong? No → delete it. Yes → keep it, and make it say what goes wrong."

# Docs prose carries no behaviour, so a diff confined to these files is comment-only.
PROSE_SUFFIXES = {".adoc", ".markdown", ".md", ".mdx", ".rst", ".txt"}

COMMENT_MARKERS: dict[str, tuple[str, ...]] = {
    ".bash": ("#",),
    ".cfg": ("#",),
    ".css": ("//",),
    ".go": ("//",),
    ".h": ("//",),
    ".ini": ("#",),
    ".js": ("//",),
    ".jsonc": ("//",),
    ".jsx": ("//",),
    ".kt": ("//",),
    ".py": ("#",),
    ".pyi": ("#",),
    ".rb": ("#",),
    ".rs": ("//",),
    ".scss": ("//",),
    ".sh": ("#",),
    ".swift": ("//",),
    ".toml": ("#",),
    ".ts": ("//",),
    ".tsx": ("//",),
    ".xml": ("<!--",),
    ".yaml": ("#",),
    ".yml": ("#",),
    ".zsh": ("#",),
}

# Suffix-less files that use shell-style comments.
COMMENT_MARKER_PREFIXES = ("Containerfile", "Dockerfile", "Makefile")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reject an unlabelled comment-only diff.")
    parser.add_argument("--base", required=True, help="base commit of the comparison")
    parser.add_argument("--head", required=True, help="head commit of the comparison")
    parser.add_argument(
        "--labels",
        default="",
        help="comma-separated labels on the pull request (empty when it has none)",
    )
    return parser.parse_args(argv)


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _block_path(block: str, first_line: str) -> str:
    """The file a `diff --git` block describes: the new side of a rename or edit."""
    for raw in block.splitlines():
        if raw.startswith("+++ ") and not raw.endswith(" /dev/null"):
            return raw[6:]
    for raw in block.splitlines():
        if raw.startswith("--- "):
            return raw[6:]
    return first_line.split(" b/", 1)[1] if " b/" in first_line else ""


def _changed_lines(diff: str) -> list[tuple[str, str]]:
    """Every added or removed line in a unified diff, paired with its file.

    Parsing is per `diff --git` block, and only lines inside a `@@` hunk count:
    a file header such as `--- a/x` must never be mistaken for a removed line,
    and a removed line that happens to start with `-- ` must never be mistaken
    for a header.
    """
    lines: list[tuple[str, str]] = []
    for block in re.split(r"(?m)^diff --git ", diff):
        first_line = block.splitlines()[0] if block else ""
        path = _block_path(block, first_line)
        inside_hunk = False
        for raw in block.splitlines():
            if not inside_hunk:
                inside_hunk = raw.startswith("@@")
                continue
            if raw.startswith("\\"):
                continue
            if raw[:1] in {"+", "-"}:
                lines.append((path, raw[1:]))
    return lines


def _comment_markers(path: str) -> tuple[str, ...] | None:
    """Comment syntax for `path`, docs prose as the empty tuple, `None` if unknown."""
    name = path.rsplit("/", 1)[-1]
    suffix = "" if "." not in name else name[name.rindex(".") :]
    if suffix in PROSE_SUFFIXES:
        return ()
    markers = COMMENT_MARKERS.get(suffix)
    if markers is None and any(name.startswith(prefix) for prefix in COMMENT_MARKER_PREFIXES):
        markers = ("#",)
    return markers


def _is_strippable(path: str, line: str) -> bool:
    """True when `line` cannot carry behaviour: blank, docs prose, or a comment."""
    text = line.strip()
    if not text:
        return True
    markers = _comment_markers(path)
    if markers is None:
        # Unknown file type: assume it can hold code rather than risk rejecting a
        # real change, which is the one failure this gate must never cause.
        return False
    if not markers:
        return True
    return any(text.startswith(marker) for marker in markers)


def _reject(changed: list[tuple[str, str]]) -> None:
    """Print the rejection with the rule quoted, so the log teaches it."""
    files = sorted({path or "<unknown>" for path, _ in changed})

    def say(line: str) -> None:
        print(line, file=sys.stderr)

    say(
        f"REJECTED: every changed line in this pull request is a comment, a blank line, or docs prose ({', '.join(files)})."
    )
    say("")
    say('AGENT.md, "## Comments":')
    say(f"  {RULE}")
    say("")
    say("  The test to apply:")
    say(f"  {DELETION_TEST}")
    say("")
    say(
        "Before pushing a comment-only change, first ask whether a rename or a restructure removes the need for the comment."
    )
    say(
        f"To land a comment fix that is genuinely needed: add the `{COMMENT_ONLY_LABEL}` label and state in the pull request body"
    )
    say("which reader-facing claim was wrong and what the new text makes correct.")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    labels = {name.strip() for name in args.labels.split(",") if name.strip()}
    try:
        diff = _git("diff", "-U0", "--no-color", f"{args.base}...{args.head}")
    except RuntimeError as error:
        print(f"Cannot read the pull request diff: {error}", file=sys.stderr)
        print("This gate fails closed: the checkout must contain both commits (fetch-depth: 0).", file=sys.stderr)
        return 1

    changed = _changed_lines(diff)
    if not changed:
        print("No changed lines to judge (empty diff, rename, or mode change): the rule does not apply.")
        return 0

    code_lines = [(path, line) for path, line in changed if not _is_strippable(path, line)]
    if code_lines:
        files = ", ".join(sorted({path for path, _ in code_lines})[:10])
        print(f"Not a comment-only diff: {len(code_lines)} of {len(changed)} changed lines are code ({files}).")
        if COMMENT_ONLY_LABEL in labels:
            print(f"`{COMMENT_ONLY_LABEL}` is set but has no effect here: the diff changes code.")
        return 0

    if COMMENT_ONLY_LABEL in labels:
        print(
            f"Comment-only diff with the `{COMMENT_ONLY_LABEL}` label: the reviewer reads the pull request body for the reader-facing claim that was wrong."
        )
        return 0

    _reject(changed)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
