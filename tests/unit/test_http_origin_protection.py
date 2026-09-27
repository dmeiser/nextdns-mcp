"""Regression tests for FastMCP HTTP host/origin protection (issue #269).

The documented local setup binds the streamable-HTTP transport to 127.0.0.1,
but FastMCP's ``http_host_origin_protection`` defaults to ``False`` -- the one
free control that mitigates DNS rebinding against that unauthenticated loopback
endpoint is switched off. Both container images must ship the ``auto`` mode,
which enforces origin checks when the bind is loopback, and the variable must
be documented alongside the other environment variables.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VAR = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"
DOCKERFILES = ("Dockerfile", "Dockerfile.alpine")


def _env_block(text: str) -> str:
    """Return the single ``ENV`` block (key=value lines only) of a Dockerfile."""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("ENV "):
            block = [line[len("ENV ") :].strip()]
            while block[-1].endswith("\\"):
                block[-1] = block[-1][:-1].strip()
                index += 1
                block.append(lines[index].strip())
            blocks.append(" ".join(block))
        index += 1
    assert blocks, "no ENV instruction found"
    assert len(blocks) == 1, f"expected one ENV block, found {len(blocks)}"
    return blocks[0]


def test_dockerfiles_enable_auto_origin_protection() -> None:
    """Both images must set the origin protection to auto next to the other FastMCP env."""
    for name in DOCKERFILES:
        path = REPO_ROOT / name
        assert path.exists(), f"{name} must exist in the repository"
        block = _env_block(path.read_text(encoding="utf-8"))
        assert f"{ENV_VAR}=auto" in block.split(), f"{name} must set {ENV_VAR}=auto in its ENV block, got: {block}"


def test_configuration_docs_document_origin_protection() -> None:
    """The env-var table in docs/configuration.md must document the variable."""
    docs = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    row = next((line for line in docs.splitlines() if line.startswith(f"| {ENV_VAR} ")), None)
    assert row is not None, f"docs/configuration.md must document {ENV_VAR} in its env-var table"
    assert "auto" in row, f"the docs row must name the default mode: {row}"


def test_env_example_documents_origin_protection() -> None:
    """.env.example must tell operators about the variable and its opt-out."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert ENV_VAR in example, f".env.example must mention {ENV_VAR}"
    assert "false" in example, ".env.example must document the reverse-proxy opt-out"


def test_env_var_enables_auto_mode_in_fastmcp() -> None:
    """Setting the variable must actually resolve FastMCP's setting to "auto"."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fastmcp import settings; print(settings.http_host_origin_protection)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, ENV_VAR: "auto"},
        check=True,
    )
    assert result.stdout.strip() == "auto"
