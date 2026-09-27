"""Build-context hygiene tests for ``.dockerignore`` (issue #282).

The whole build context is uploaded to the Docker daemon on every
``docker build`` regardless of which ``COPY`` lines run, and the context
tarball is what the layer cache is keyed on. A local ``mypy`` run leaves
hundreds of ``.db`` files under ``.mypy_cache/`` (32 in a single worktree)
and ``ruff`` leaves its own cache under ``.ruff_cache/``; neither directory
is referenced by any ``COPY`` line, so those bytes are transferred on every
build, never used, and an unrelated type-check run invalidates the layer
cache and forces a full dependency re-install.

These tests do not grep the raw file for one literal string. They resolve
``.dockerignore`` into a typed pattern list, apply Docker's documented
matching rules to concrete build-context paths, and assert that the two
cache directories (and the files inside them) are excluded from the context
while every path the Dockerfiles actually copy is not.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"
DOCKERFILES = (REPO_ROOT / "Dockerfile", REPO_ROOT / "Dockerfile.alpine")

# The two largest local cache directories the issue reported as missing.
TOOL_CACHE_DIRS = (".mypy_cache", ".ruff_cache")

# Representative files a local tool run leaves behind (see the issue).
TOOL_CACHE_FILES = (
    ".mypy_cache/3.12/nextdns_mcp/server.data.db",
    ".ruff_cache/0.14.0/CACHEDIR.TAG",
    ".mypy_cache/CACHEDIR.TAG",
)

# Files the image build genuinely needs; excluding a cache dir must not
# exclude any of these.
REQUIRED_CONTEXT_PATHS = (
    "src/nextdns_mcp/server.py",
    "src/nextdns_mcp/nextdns-openapi.yaml",
    "pyproject.toml",
    "uv.lock",
)


@dataclass(frozen=True)
class Pattern:
    """One ``.dockerignore`` line resolved against Docker's matching rules."""

    raw: str
    negated: bool
    #: ``.mypy_cache/`` -- trailing slash restricts the match to a directory.
    dir_only: bool
    #: ``.mypy_cache`` -- the literal or glob the path is matched against.
    body: str


def parse_patterns(text: str) -> list[Pattern]:
    """Resolve ``.dockerignore`` text into patterns, dropping blanks/comments."""
    patterns: list[Pattern] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        negated = stripped.startswith("!")
        body = stripped[1:] if negated else stripped
        dir_only = body.endswith("/")
        patterns.append(Pattern(raw=stripped, negated=negated, dir_only=dir_only, body=body.rstrip("/")))
    return patterns


def is_excluded(patterns: list[Pattern], path: str) -> bool:
    """True when ``path`` is excluded from the build context.

    Docker matches a pattern against the path relative to the context root:
    a pattern matches the path itself or anything beneath it. A trailing
    slash restricts the pattern to directories, so it never matches a file.
    The last matching pattern wins, which is what makes ``!`` re-includes work.
    """
    excluded = False
    for pattern in patterns:
        prefix = f"{pattern.body}/"
        matched = path.startswith(prefix) or path == pattern.body
        if not matched and not pattern.dir_only:
            matched = fnmatch.fnmatch(path, pattern.body) or fnmatch.fnmatch(
                Path(path).name, pattern.body
            )
        if matched:
            excluded = not pattern.negated
    return excluded


def copied_paths(dockerfile: Path) -> set[str]:
    """Every context path a Dockerfile copies (from ``COPY``/``ADD`` sources)."""
    copy_line = re.compile(r"^\s*(?:COPY|ADD)\s+(.*)$", re.IGNORECASE)
    sources: set[str] = set()
    for raw in dockerfile.read_text().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line.upper().startswith("COPY ") or line.upper().startswith("ADD "):
            parts = line.split()[1:]
            if not parts:
                continue
            for source in parts[:-1]:  # the last token is the destination
                if not source.startswith("--"):
                    sources.add(source.lstrip("./").rstrip("/"))
    return sources


def test_tool_cache_files_are_excluded_from_the_build_context() -> None:
    """Issue #282: the two local tool caches must not enter the build context."""
    patterns = parse_patterns(DOCKERIGNORE_PATH.read_text())

    leaked = [path for path in TOOL_CACHE_FILES if not is_excluded(patterns, path)]

    assert not leaked, (
        f".dockerignore does not exclude {leaked}; every build uploads these "
        "unused bytes and an unrelated mypy/ruff run invalidates the layer cache"
    )


def test_tool_cache_omission_is_what_the_test_detects() -> None:
    """Guard the guard: dropping the two lines must make the check fail.

    Proves ``is_excluded`` really reports the pre-fix behavior, so a passing
    run means the patterns are present rather than a vacuous assertion.
    """
    text = DOCKERIGNORE_PATH.read_text()
    pre_fix = "\n".join(
        line for line in text.splitlines() if line.strip().rstrip("/") not in TOOL_CACHE_DIRS
    )
    patterns = parse_patterns(pre_fix)

    leaked = [path for path in TOOL_CACHE_FILES if not is_excluded(patterns, path)]

    assert len(leaked) == len(TOOL_CACHE_FILES), f"expected the pre-fix .dockerignore to leak {TOOL_CACHE_FILES}"


def test_required_context_files_are_not_excluded() -> None:
    """Excluding cache directories must not drop files the image needs."""
    patterns = parse_patterns(DOCKERIGNORE_PATH.read_text())

    missing = [path for path in REQUIRED_CONTEXT_PATHS if is_excluded(patterns, path)]

    assert not missing, f".dockerignore excludes build inputs: {missing}"


def test_no_dockerfile_copies_a_tool_cache_directory() -> None:
    """The exclusions are safe because no ``COPY`` line references a cache dir."""
    for dockerfile in DOCKERFILES:
        offending = sorted(src for src in copied_paths(dockerfile) if src in TOOL_CACHE_DIRS)

        assert not offending, f"{dockerfile.name} copies ignored cache directories: {offending}"
