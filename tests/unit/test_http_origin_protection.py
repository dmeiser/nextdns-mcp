"""Regression tests for FastMCP HTTP host/origin protection (issue #269).

The documented local setup binds the streamable-HTTP transport to 127.0.0.1,
but FastMCP's ``http_host_origin_protection`` defaults to ``False`` -- the one
free control that mitigates DNS rebinding against that unauthenticated loopback
endpoint is switched off. Both container images must ship the ``auto`` mode,
which enforces origin checks when the bind is loopback, and the shipped value
must stay in sync with what the documentation tells operators to expect.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VAR = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"
SHIPPED_VALUE = "auto"
DOCKERFILES = ("Dockerfile", "Dockerfile.alpine")


def _dockerfile_env(path: Path) -> dict[str, str]:
    """Return the effective ``KEY=VALUE`` environment a built image inherits."""
    pairs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("ENV "):
            entries = [line[len("ENV ") :].strip()]
            while entries[-1].endswith("\\"):
                entries[-1] = entries[-1][:-1].strip()
                index += 1
                entries.append(lines[index].strip())
            for entry in " ".join(entries).split():
                key, _, value = entry.partition("=")
                pairs[key] = value
        index += 1
    return pairs


def test_dockerfiles_ship_auto_origin_protection() -> None:
    """Both images must set the origin protection to auto in their build environment."""
    for name in DOCKERFILES:
        env = _dockerfile_env(REPO_ROOT / name)
        assert env.get(ENV_VAR) == SHIPPED_VALUE, (
            f"{name} must set {ENV_VAR}={SHIPPED_VALUE}, got: {env.get(ENV_VAR)!r}"
        )


def test_env_var_enables_auto_mode_in_fastmcp() -> None:
    """The shipped value must actually resolve FastMCP's setting to the auto mode."""
    result = subprocess.run(
        [sys.executable, "-c", "from fastmcp import settings; print(settings.http_host_origin_protection)"],
        capture_output=True,
        text=True,
        env={**os.environ, ENV_VAR: SHIPPED_VALUE},
        check=True,
    )
    assert result.stdout.strip() == SHIPPED_VALUE


def test_documented_default_matches_the_shipped_value() -> None:
    """The env-var reference must document the variable with the value the images ship."""
    table = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    row = next((line for line in table.splitlines() if line.startswith(f"| {ENV_VAR} |")), None)
    assert row is not None, f"docs/configuration.md must document {ENV_VAR} in its env-var table"
    columns = [cell.strip() for cell in row.strip("|").split("|")]
    assert SHIPPED_VALUE in columns[2], f"the documented default must be {SHIPPED_VALUE}: {row}"


def _documented_env(path: Path) -> dict[str, str]:
    """Return the variables a sample env file offers, commented or not, as a mapping."""
    documented: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip().lstrip("#").strip()
        if "=" not in entry or entry.startswith("#"):
            continue
        key, _, value = entry.partition("=")
        if key and key.replace("_", "").isalnum():
            documented[key] = value
    return documented


def test_env_example_documents_origin_protection() -> None:
    """.env.example must offer the same value the container images ship."""
    documented = _documented_env(REPO_ROOT / ".env.example")
    assert ENV_VAR in documented, f".env.example must document {ENV_VAR}"
    assert documented[ENV_VAR] == SHIPPED_VALUE
