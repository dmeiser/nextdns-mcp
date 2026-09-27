"""Unit tests that the documented configuration matches the code (issue #275).

These guard the three documentation defects reported in issue #275:

* ``NEXTDNS_TEST_PROFILE`` was documented in ``.env.example`` and ``README.md``
  but read by no code, so an operator who set it believed they had a guard they
  did not have.
* ``.env.example`` stated a default HTTP timeout of 10 while the code's
  ``DEFAULT_HTTP_TIMEOUT`` is 30.
* ``docs/index.md`` claimed the transport was stdio only, although the
  streamable-HTTP transport is supported and documented.

SPDX-License-Identifier: MIT
"""

import re
from pathlib import Path

from nextdns_mcp.config import DEFAULT_HTTP_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[2]

# Docs that an operator reads when looking for a configuration key.
DOC_PATHS = (".env.example", "README.md", "docs/index.md", "docs/configuration.md")


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"{relative_path} must exist in the repository"
    return path.read_text(encoding="utf-8")


def test_no_documented_env_var_that_no_code_reads():
    """No operator-facing doc may document NEXTDNS_TEST_PROFILE.

    The real write guards are NEXTDNS_WRITABLE_PROFILES and NEXTDNS_READ_ONLY;
    an operator who sets NEXTDNS_TEST_PROFILE gets no behaviour and no warning.
    """
    offenders = [path for path in DOC_PATHS if "NEXTDNS_TEST_PROFILE" in _read(path)]
    assert offenders == [], (
        f"NEXTDNS_TEST_PROFILE is read by no code but is documented in {offenders}. "
        "Remove it and point operators at NEXTDNS_WRITABLE_PROFILES / NEXTDNS_READ_ONLY."
    )


def test_env_example_timeout_default_matches_code():
    """.env.example must document the code's DEFAULT_HTTP_TIMEOUT, both as a
    stated default and as the example value."""
    env_example = _read(".env.example")
    default = int(DEFAULT_HTTP_TIMEOUT)

    stated = re.search(
        rf"HTTP request timeout in seconds \(default:\s*(\d+)\)",
        env_example,
    )
    assert stated is not None, ".env.example must state the HTTP timeout default"
    assert int(stated.group(1)) == default, (
        f".env.example states an HTTP timeout default of {stated.group(1)} but "
        f"config.DEFAULT_HTTP_TIMEOUT is {default}"
    )

    example = re.search(r"^# NEXTDNS_HTTP_TIMEOUT=(\S+)\s*$", env_example, re.MULTILINE)
    assert example is not None, ".env.example must show an example NEXTDNS_HTTP_TIMEOUT value"
    assert int(example.group(1)) == default, (
        f".env.example shows NEXTDNS_HTTP_TIMEOUT={example.group(1)} but "
        f"config.DEFAULT_HTTP_TIMEOUT is {default}"
    )


def test_docs_index_does_not_claim_stdio_only():
    """docs/index.md must not tell operators the server is stdio-only.

    The streamable-HTTP transport is a supported, documented feature
    (docs/configuration.md, the README's HTTP Transport section, EXPOSE 8000 in
    both Dockerfiles).
    """
    index = _read("docs/index.md")
    notes = index.split("## Notes", 1)[1]
    assert "no HTTP port" not in notes, (
        "docs/index.md still claims 'no HTTP port'; the streamable-HTTP transport is supported. "
        "See the README's HTTP Transport section and docs/configuration.md."
    )
    assert "streamable-HTTP" in notes, (
        "docs/index.md's Notes must point at the opt-in streamable-HTTP transport"
    )
