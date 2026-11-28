#!/usr/bin/env python3
"""Shared version CLI for the release workflows (issue #770).

Single owner of the version get/bump/check logic that used to be inlined,
with drift, across five workflows: ``version-bumper.yml``,
``weekly-maintenance.yml``, ``version-guard.yml``, ``docker-publish.yml``
and ``auto-release.yml``.

The release version lives in four tracked files (``uv.lock`` is re-locked by
the caller after a bump): ``pyproject.toml``, ``src/nextdns_mcp/__init__.py``,
``Dockerfile`` and ``Dockerfile.alpine``.

Subcommands:
    get    print the project version from pyproject.toml
    bump   compute the next version from a baseline and rewrite the four files
    check  verify the four files agree and are ahead of a baseline checkout
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


class VersionError(Exception):
    """A version could not be read, computed or applied."""


@dataclass(frozen=True)
class VersionFile:
    """One tracked file carrying the release version."""

    path: str
    pattern: re.Pattern[str]  # capture group 1 is the version
    template: str  # replacement text; {version} is substituted


VERSION_FILES: tuple[VersionFile, ...] = (
    VersionFile(
        "pyproject.toml",
        re.compile(r'^version\s*=\s*"([^"]*)"', re.MULTILINE),
        'version = "{version}"',
    ),
    VersionFile(
        "src/nextdns_mcp/__init__.py",
        re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']'),
        '__version__ = "{version}"',
    ),
    VersionFile(
        "Dockerfile",
        re.compile(r'LABEL org\.opencontainers\.image\.version=["\']([^"\']+)["\']'),
        'LABEL org.opencontainers.image.version="{version}"',
    ),
    VersionFile(
        "Dockerfile.alpine",
        re.compile(r'LABEL org\.opencontainers\.image\.version=["\']([^"\']+)["\']'),
        'LABEL org.opencontainers.image.version="{version}"',
    ),
)


def read_pyproject_version(root: Path) -> str:
    """The project version declared in ``root/pyproject.toml``."""
    path = root / "pyproject.toml"
    if not path.is_file():
        raise VersionError(f"pyproject.toml not found at {path}")
    with path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise VersionError(f"invalid TOML in {path}: {exc}") from exc
    try:
        version = data["project"]["version"]
    except KeyError:
        raise VersionError("'project.version' not found in pyproject.toml") from None
    if not isinstance(version, str) or not version.strip():
        raise VersionError("'project.version' is empty or not a string")
    return version.strip()


def read_file_version(root: Path, version_file: VersionFile) -> str | None:
    """The version currently recorded in one tracked file, or None when absent."""
    match = version_file.pattern.search((root / version_file.path).read_text())
    return match.group(1) if match else None


def bump_version(current: str, label: str) -> str:
    """The next version for a ``bump-*`` label, applied to ``current``."""
    try:
        major, minor, patch = (int(part) for part in current.split("."))
    except ValueError:
        raise VersionError(f"cannot bump non-semver version {current!r}") from None
    if label == "bump-major":
        major, minor, patch = major + 1, 0, 0
    elif label == "bump-minor":
        minor, patch = minor + 1, 0
    elif label == "bump-patch":
        patch += 1
    else:
        raise VersionError(f"unknown bump label {label!r}")
    return f"{major}.{minor}.{patch}"


def apply_version(root: Path, version: str) -> None:
    """Rewrite the release version in every tracked file under ``root``."""
    for version_file in VERSION_FILES:
        path = root / version_file.path
        old = path.read_text()
        replacement = version_file.template.format(version=version)
        new = version_file.pattern.sub(replacement, old)
        if old != new:
            path.write_text(new)
            print(f"✅ Updated {version_file.path}", file=sys.stderr)
        else:
            print(f"⚠️ No change needed for {version_file.path}", file=sys.stderr)


def normalize_version(version: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) as integers; suffixes like ``-rc.1`` are ignored."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_greater(v1: str, v2: str) -> bool:
    """True when ``v1`` is a newer version than ``v2`` (string fallback when unparsable)."""
    t1, t2 = normalize_version(v1), normalize_version(v2)
    if t1 is None or t2 is None:
        return v1 > v2
    return t1 > t2


def cmd_get(args: argparse.Namespace) -> int:
    """Print the project version; the sole stdout line, for shell capture."""
    print(read_pyproject_version(Path(args.root)))
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    """Bump from the baseline version and rewrite the tracked files."""
    root = Path(args.root)
    baseline = Path(args.baseline) if args.baseline else root
    current = read_pyproject_version(baseline)
    next_version = bump_version(current, args.label)
    print(f"Bumping {current} -> {next_version} (due to {args.label})", file=sys.stderr)
    apply_version(root, next_version)
    print(next_version)  # sole stdout line, for shell capture
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Verify the tracked files agree with each other and exceed the baseline."""
    root = Path(args.root)
    baseline = Path(args.baseline)
    try:
        baseline_version = read_pyproject_version(baseline)
        # tomllib, not the regex, so `check` reads the same [project] version `get` does.
        project_version = read_pyproject_version(root)
        versions = {
            version_file.path: read_file_version(root, version_file)
            for version_file in VERSION_FILES
            if version_file.path != "pyproject.toml"
        }
    except (OSError, VersionError) as exc:
        print(f"❌ Error extracting versions: {exc}")
        return 1

    print(f"Baseline Version: {baseline_version}")
    print(f"PR pyproject.toml: {project_version}")
    for path, version in versions.items():
        print(f"PR {path}: {version}")

    errors: list[str] = []
    if not is_greater(project_version, baseline_version):
        errors.append(f"Version in pyproject.toml ({project_version}) must be greater than main ({baseline_version}).")
    for path, version in versions.items():
        if version != project_version:
            errors.append(f"Mismatch: pyproject.toml ({project_version}) != {path} ({version})")

    if errors:
        print("\n🚨 Version Guard Failed:")
        for error in errors:
            print(f"  - {error}")
        print("\nAdd a 'bump-*' label to auto-fix or update manually.")
        return 1

    print("\n✅ All version strings are synced and incremented.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="version_sync.py",
        description="Shared version get/bump/check CLI for the release workflows.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("get", help="print the project version from pyproject.toml")
    get.add_argument("--root", default=".", help="checkout root to read (default: current directory)")
    get.set_defaults(func=cmd_get)

    bump = sub.add_parser("bump", help="bump the version and rewrite the tracked files")
    bump.add_argument("--label", required=True, choices=["bump-patch", "bump-minor", "bump-major"])
    bump.add_argument("--root", default=".", help="checkout root to rewrite (default: current directory)")
    bump.add_argument(
        "--baseline", default=None, help="checkout root to read the current version from (default: --root)"
    )
    bump.set_defaults(func=cmd_bump)

    check = sub.add_parser("check", help="verify the tracked files agree and exceed the baseline")
    check.add_argument("--root", default=".", help="checkout root to validate (default: current directory)")
    check.add_argument("--baseline", required=True, help="baseline checkout root the version must exceed")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, VersionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
