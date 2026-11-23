"""Base-image reproducibility for both container variants (issue #607).

Both Dockerfiles used to resolve mutable `python:3.15-slim` /
`python:3.15-alpine` tags and then run build-time distro upgrades
(`apt-get upgrade -y` / `apk upgrade --no-cache`) in the builder and runtime
stages. Two rebuilds of the same revision therefore produced different
images: the base layer moved under the tag, and each upgrade pulled whatever
the distro mirrors served that day.

The fix pins every `FROM` to a digest (Dependabot's docker ecosystem proposes
digest updates weekly) and drops the upgrades, leaving the distro's package set
exactly as the pinned base ships it. No CI job tells the two builds apart —
both build and run fine either way — so these tests read the Dockerfiles as the
build contract they are, the same way `test_deno_install_provenance.py` reads
the Deno install: every `FROM` carries an explicit digest, and no `RUN`
upgrades the distro in place.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = (REPO_ROOT / "Dockerfile", REPO_ROOT / "Dockerfile.alpine")

_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\Z")
_FROM = re.compile(r"\AFROM\s+(?P<image>\S+)(?:\s+AS\s+\w+)?\Z", re.IGNORECASE)


def _instructions(text: str) -> list[str]:
    """Complete instructions, with ``\\`` continuations folded into one string."""
    instructions = []
    pending = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].strip()
            continue
        instructions.append(pending)
        pending = ""
    return instructions


def from_images(text: str) -> list[str]:
    """The image reference of every ``FROM`` instruction, in file order."""
    return [
        match.group("image") for instruction in _instructions(text) if (match := _FROM.match(instruction)) is not None
    ]


def run_instructions(text: str) -> list[str]:
    """The ``RUN`` instructions of a Dockerfile, in build order."""
    return [i for i in _instructions(text) if i.upper().startswith("RUN")]


def pinning_violations(text: str) -> list[str]:
    """Every ``FROM`` must name an explicit ``@sha256:`` digest."""
    return [f"FROM {image} is not digest-pinned" for image in from_images(text) if not _DIGEST.search(image)]


def upgrade_violations(text: str) -> list[str]:
    """No ``RUN`` may upgrade the distro in place; that reintroduces drift."""
    violations = []
    for instruction in run_instructions(text):
        if re.search(r"\bapt-get\s+upgrade\b", instruction, re.IGNORECASE):
            violations.append(f"build-time apt upgrade: {instruction[:72]}...")
        if re.search(r"\bapk\s+upgrade\b", instruction, re.IGNORECASE):
            violations.append(f"build-time apk upgrade: {instruction[:72]}...")
    return violations


def reproducibility_violations(text: str) -> list[str]:
    """Every way ``text`` makes a rebuild non-reproducible."""
    return [*pinning_violations(text), *upgrade_violations(text)]


#: The drifted build exactly as it stood on ``main`` before issue #607 was
#: fixed, kept so the guard can be guarded: every check below must reject it.
PRE_FIX_DOCKERFILE = """
FROM python:3.15-slim AS builder
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
FROM python:3.15-slim
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
"""

PRE_FIX_DOCKERFILE_ALPINE = """
FROM python:3.15-alpine AS builder
RUN apk upgrade --no-cache && apk add --no-cache build-base linux-headers
FROM python:3.15-alpine
RUN apk upgrade --no-cache
"""


def test_every_stage_of_every_dockerfile_is_digest_pinned() -> None:
    """A mutable tag means the base layer moves under an unchanged revision."""
    for dockerfile in DOCKERFILES:
        violations = pinning_violations(dockerfile.read_text())

        assert not violations, f"{dockerfile.name} has unpinned stages: {violations}"


def test_no_stage_runs_a_build_time_upgrade() -> None:
    """`upgrade` at build time makes the result depend on mirror state that day."""
    for dockerfile in DOCKERFILES:
        violations = upgrade_violations(dockerfile.read_text())

        assert not violations, f"{dockerfile.name} upgrades the distro in place: {violations}"


def test_builder_and_runtime_stages_share_one_base_digest() -> None:
    """Both stages must resolve the same base, or the runtime tracks a different
    image than the one the dependencies were built against."""
    for dockerfile in DOCKERFILES:
        bases = from_images(dockerfile.read_text())

        assert len(bases) == 2, f"{dockerfile.name} should resolve python twice, got: {bases}"
        assert len(set(bases)) == 1, f"{dockerfile.name} stages disagree on the base: {bases}"


def test_the_drifted_build_is_rejected_wholesale() -> None:
    """The whole contract, replayed against the pre-fix builds, reports the defect."""
    for text in (PRE_FIX_DOCKERFILE, PRE_FIX_DOCKERFILE_ALPINE):
        violations = reproducibility_violations(text)

        assert len(violations) >= 3, f"expected the drifted build to fail several checks, got: {violations}"


def test_the_guard_is_not_vacuous() -> None:
    """Guard the guard: each check alone must reject the drifted build."""
    assert pinning_violations(PRE_FIX_DOCKERFILE), "unpinned tags should fail the pinning check"
    assert pinning_violations(PRE_FIX_DOCKERFILE_ALPINE), "unpinned tags should fail the pinning check"
    assert upgrade_violations(PRE_FIX_DOCKERFILE), "apt-get upgrade should fail the upgrade check"
    assert upgrade_violations(PRE_FIX_DOCKERFILE_ALPINE), "apk upgrade should fail the upgrade check"
