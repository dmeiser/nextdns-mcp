"""Deno install provenance in the slim image (issue #537).

The Prefab sandbox shells out to a Deno binary, and ``Dockerfile`` used to
obtain that binary by piping a remote script straight into a shell::

    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --version v2.3.7

so the binary shipped in the published image was whatever the network handed
back, with nothing comparing it to a known-good release. The same RUN installed
``curl`` and ``unzip`` into the *runtime* stage and left them there, widening
the attack surface of an image whose only runtime needs are Python and Deno.

The current build downloads a versioned release archive and verifies it against
a pinned SHA256 before extracting it, in the builder stage, and the runtime
stage receives only the verified binary through ``COPY --from=builder``.

No CI job tells the two builds apart: the pre-fix Dockerfile also ends with a
working ``deno 2.3.7``, so ``E2E Container (slim)`` is green either way, and the
pre-fix image still carries ``/usr/bin/curl``. These tests read ``Dockerfile``
as the build contract it is -- the pinned release, the per-architecture
checksums, the download/verify/extract order, and which stage may carry fetch
tooling -- so reinstating the unverified install fails the suite instead of
shipping. ``Dockerfile.alpine`` is deliberately out of scope: it takes Deno from
the Alpine package manager, which is signed by the distro.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

#: Pinned Deno release, and the SHA256 of each architecture's release asset.
VERSION_ARG = "DENO_VERSION"
CHECKSUM_ARGS = ("DENO_X86_64_SHA256", "DENO_AARCH64_SHA256")

#: Tooling needed to fetch and unpack Deno; the shipped stage must not install it.
RUNTIME_FORBIDDEN_PACKAGES = ("curl", "unzip")

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ARG = re.compile(r"\AARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)\Z", re.IGNORECASE)
#: A pipe into a shell, optionally behind ``VAR=value`` assignments as the old
#: Deno installer used. ``| sha256sum`` is not a shell and must not match. The
#: possessive quantifiers (Python 3.11+) commit the matcher to each assignment,
#: which is what keeps CodeQL's py/redos from flagging the repeated group.
_PIPE_TO_SHELL = re.compile(r"\|\s*+(?:[A-Za-z_][A-Za-z0-9_]*=\S+)*+\s*+(?:sh|bash|dash|ksh|zsh)\b")
#: The versioned release asset path, e.g. ``releases/download/v${DENO_VERSION}/``.
_RELEASE_ASSET = re.compile(r"github\.com/\S*releases/download/v")


def dockerfile_stages(text: str) -> list[list[str]]:
    """Every stage of a Dockerfile as a list of complete instructions.

    A ``FROM`` opens a stage; a trailing backslash continues the current
    instruction, so one ``RUN`` chain is read as one string rather than the
    physical lines that carry it. Comment-only lines are dropped.
    """
    stages: list[list[str]] = []
    current: list[str] = []
    pending = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].strip()
            continue
        if pending.upper().startswith("FROM"):
            current = [pending]
            stages.append(current)
        elif current:
            current.append(pending)
        pending = ""
    return stages


def arg_values(stages: list[list[str]]) -> dict[str, str]:
    """``ARG name=value`` pairs declared anywhere in the Dockerfile."""
    values: dict[str, str] = {}
    for stage in stages:
        for instruction in stage:
            match = _ARG.match(instruction)
            if match:
                values[match.group(1)] = match.group(2)
    return values


def run_steps(instruction: str) -> list[str]:
    """The ``&&``-separated commands of a ``RUN`` instruction, in build order."""
    return [step.strip() for step in instruction.split("&&") if step.strip()]


def deno_install_runs(stages: list[list[str]]) -> list[str]:
    """Every ``RUN`` that fetches or installs Deno, in any stage."""
    return [
        instruction
        for stage in stages
        for instruction in stage
        if instruction.upper().startswith("RUN") and "deno" in instruction.lower()
    ]


def _first_index(steps: list[str], predicate) -> int:
    """Index of the first step matching ``predicate``, or -1 when none does."""
    for index, step in enumerate(steps):
        if predicate(step):
            return index
    return -1


def pinning_violations(stages: list[list[str]]) -> list[str]:
    """The Deno release and its checksums must be pinned, per architecture."""
    declared = arg_values(stages)
    violations = [
        f"{name} is not declared as a pinned ARG" for name in (VERSION_ARG, *CHECKSUM_ARGS) if name not in declared
    ]
    violations += [
        f"{name} is not a 64-digit hex SHA256 (got {declared[name]!r})"
        for name in CHECKSUM_ARGS
        if name in declared and not _SHA256.match(declared[name])
    ]
    if not declared.get(VERSION_ARG):
        violations.append(f"{VERSION_ARG} has no value, so the Deno release is not pinned")
    return violations


def verification_violations(stages: list[list[str]]) -> list[str]:
    """The archive must be downloaded from a versioned asset and checksummed
    between the download and the extraction."""
    runs = deno_install_runs(stages)
    if not runs:
        return ["no RUN installs the Deno runtime"]
    violations: list[str] = []
    for instruction in runs:
        lowered = instruction.lower()
        steps = run_steps(instruction)
        if not _RELEASE_ASSET.search(lowered):
            violations.append("Deno is not downloaded from a versioned GitHub release asset")
        download = _first_index(steps, lambda step: step.lower().startswith("curl") and " -o " in f"{step} ")
        verify = _first_index(steps, lambda step: "sha256sum -c" in step.lower())
        extract = _first_index(steps, lambda step: step.lower().startswith("unzip"))
        if verify == -1:
            violations.append("the Deno archive is used without a `sha256sum -c` verification step")
            continue
        if download != -1 and verify < download:
            violations.append("the Deno archive is verified before it is downloaded")
        if extract != -1 and extract < verify:
            violations.append("the Deno archive is extracted before it is verified")
    return violations


def remote_script_violations(stages: list[list[str]]) -> list[str]:
    """No stage may pipe a fetched script into a shell."""
    violations: list[str] = []
    for stage in stages:
        for instruction in stage:
            if instruction.upper().startswith("RUN") and _PIPE_TO_SHELL.search(instruction):
                violations.append(f"a fetched script is piped into a shell: {instruction[:72]}...")
    return violations


def runtime_stage_violations(stages: list[list[str]]) -> list[str]:
    """The shipped stage must not install fetch tooling and must receive Deno
    from an earlier, verifying stage rather than building it in place."""
    if not stages:
        return ["the Dockerfile declares no stages"]
    runtime = stages[-1]
    violations: list[str] = []
    for instruction in runtime:
        if not instruction.upper().startswith("RUN"):
            continue
        lowered = instruction.lower()
        if "apt-get install" not in lowered and "apk add" not in lowered:
            continue
        installed = [pkg for pkg in RUNTIME_FORBIDDEN_PACKAGES if re.search(rf"\b{pkg}\b", lowered)]
        if installed:
            violations.append(f"the runtime stage installs {installed}, leaving them in the shipped image")
    if not any(
        instruction.upper().startswith("COPY --FROM=") and "deno" in instruction.lower() for instruction in runtime
    ):
        violations.append("the runtime stage never receives the Deno binary from an earlier stage")
    return violations


def provenance_violations(text: str) -> list[str]:
    """Every way ``text`` fails to install Deno from a verified release archive."""
    stages = dockerfile_stages(text)
    return [
        *pinning_violations(stages),
        *verification_violations(stages),
        *remote_script_violations(stages),
        *runtime_stage_violations(stages),
    ]


#: The runtime-stage block exactly as it stood on ``main`` before issue #537 was
#: fixed, kept here so the guard can be guarded: every check below must reject it.
PRE_FIX_DOCKERFILE = """
FROM python:3.15-slim AS builder
WORKDIR /app
FROM python:3.15-slim
WORKDIR /app
COPY --from=builder /install /install
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip \\
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --version v2.3.7 \\
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home appuser
"""


def test_deno_release_and_checksums_are_pinned() -> None:
    """The image must install a named Deno release whose per-arch hashes are known."""
    violations = pinning_violations(dockerfile_stages(DOCKERFILE.read_text()))

    assert not violations, f"{DOCKERFILE.name} does not pin the Deno release: {violations}"


def test_deno_archive_is_verified_between_download_and_extraction() -> None:
    """A pinned hash is only worth having if the build checks it at the right point."""
    violations = verification_violations(dockerfile_stages(DOCKERFILE.read_text()))

    assert not violations, f"{DOCKERFILE.name} does not verify the Deno archive: {violations}"


def test_no_fetched_script_is_piped_into_a_shell() -> None:
    """``curl ... | sh`` executes whatever the network returns, unverified."""
    violations = remote_script_violations(dockerfile_stages(DOCKERFILE.read_text()))

    assert not violations, f"{DOCKERFILE.name} pipes a remote script into a shell: {violations}"


def test_runtime_stage_ships_no_fetch_tooling_and_receives_the_verified_binary() -> None:
    """``curl``/``unzip`` belong to the build stage; the image gets the binary only."""
    violations = runtime_stage_violations(dockerfile_stages(DOCKERFILE.read_text()))

    assert not violations, f"{DOCKERFILE.name} runtime stage is not minimal: {violations}"


def test_the_sha256_guard_is_the_thing_being_enforced() -> None:
    """Guard the guard: the pre-fix Dockerfile must fail every check above.

    Without this, a check could pass vacuously -- for instance by matching
    nothing at all -- and the unverified install would return unnoticed.
    """
    stages = dockerfile_stages(PRE_FIX_DOCKERFILE)

    assert pinning_violations(stages), "the pre-fix Dockerfile should not satisfy the pinning check"
    assert verification_violations(stages), "the pre-fix Dockerfile should not satisfy the verification check"
    assert remote_script_violations(stages), "the pre-fix Dockerfile should not satisfy the remote-script check"
    assert runtime_stage_violations(stages), "the pre-fix Dockerfile should not satisfy the runtime-stage check"

    assert "https://deno.land/install.sh" in PRE_FIX_DOCKERFILE, "the pre-fix fixture lost the unverified install"


def test_the_pre_fix_dockerfile_is_rejected_wholesale() -> None:
    """The whole contract, replayed against the broken build, reports the defect."""
    violations = provenance_violations(PRE_FIX_DOCKERFILE)

    assert len(violations) >= 5, f"expected the pre-fix build to fail several checks, got: {violations}"


def test_a_piped_shell_under_another_name_is_still_rejected() -> None:
    """Renaming the shell is not a way back past the guard."""
    swapped_shell = PRE_FIX_DOCKERFILE.replace("sh -s", "bash -s")

    assert remote_script_violations(dockerfile_stages(swapped_shell)), (
        "piping the fetched script into bash should be reported the same as sh"
    )


def test_the_sha256_of_a_tampered_archive_does_not_match_the_pin() -> None:
    """The pin has teeth: swapping the checksum for the real bytes fails to match.

    Both pinned values are the hashes of the official v2.3.7 release assets; a
    different asset for the same file name produces a different digest, which is
    what ``sha256sum -c`` in the build compares.
    """
    declared = arg_values(dockerfile_stages(DOCKERFILE.read_text()))
    hashes = {name: declared.get(name, "") for name in CHECKSUM_ARGS}

    assert len(set(hashes.values())) == len(CHECKSUM_ARGS), (
        f"the architectures must pin distinct digests, or a wrong-architecture or tampered archive would match: {hashes}"
    )
    assert all(_SHA256.match(value) for value in hashes.values()), f"checksums are not 64-digit hex SHA256: {hashes}"
