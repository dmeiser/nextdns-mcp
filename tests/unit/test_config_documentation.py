"""Regression tests for the configuration documentation drift of issue #275.

Three defects were reported, all of them in operator-facing text:

* ``NEXTDNS_TEST_PROFILE`` was documented in ``.env.example`` and ``README.md``
  but read by no code, so an operator who set it believed they had a guard they
  did not have. ``test_removed_test_profile_variable_is_absent_from_operator_docs``
  reproduces this: the variable name is still present in the operator
  documentation, and no code reads it.
* ``.env.example`` showed ``NEXTDNS_HTTP_TIMEOUT=10`` as the example value
  directly beneath a stated default of 30 seconds, which is the value the code
  uses. The stated ``(default: 30)`` prose was already correct at the base
  commit, so this file cannot reproduce that half by itself; what it holds is
  that a value the template shows is a value the parser accepts, whatever
  syntax the example is written in.
* ``docs/index.md`` claimed the transport was stdio only, although the
  streamable-HTTP transport is supported and documented. Nothing here guards
  that sentence: it has no executable surface, and the transport itself is
  covered by ``tests/unit/test_transport_config.py`` and
  ``tests/unit/test_mcp_run_options.py``.

Why reading text is legitimate here, and when it is not: for the first defect
the text is the behaviour -- no code reads the variable, so the only thing a
test can execute is its absence from the documentation that misleads operators.
The timeout test is different: it feeds the documented values to the real
configuration parser, so it proves the template documents settings that work
rather than that a sentence has a particular wording. A test that greps
documentation in order to prove a *behavioural* claim guards nothing instead,
because a reword flips it while behaviour is unchanged; the configuration
behaviour itself is covered by ``tests/unit/test_config.py`` and friends.

SPDX-License-Identifier: MIT
"""

import re
from pathlib import Path

import pytest

from nextdns_mcp.config import ConfigurationError, get_http_timeout

REPO_ROOT = Path(__file__).resolve().parents[2]

# The operator-facing files that document environment variables.
ENV_EXAMPLE = ".env.example"
README = "README.md"
# AGENT.md documents the same environment variables as the operator docs, so it
# is searched too.
AGENT_GUIDE = "AGENT.md"

# Every way a documented timeout is written: ``NEXTDNS_HTTP_TIMEOUT=45``,
# ``# Example: NEXTDNS_HTTP_TIMEOUT=45``, ``NEXTDNS_HTTP_TIMEOUT=45 # seconds``.
# The value is captured with ``*`` rather than ``+`` so an empty documented value
# (``# NEXTDNS_HTTP_TIMEOUT=``) is fed to the parser and fails the guard, instead
# of being skipped - the empty string is a rejected value, not an absent one.
TIMEOUT_ASSIGNMENT = re.compile(r"NEXTDNS_HTTP_TIMEOUT=(?P<value>[^\s#]*)")


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"{relative_path} must exist in the repository"
    return path.read_text(encoding="utf-8")


def _documented_timeout_examples() -> list[tuple[int, str]]:
    """Every timeout value the template shows, as (line number, value)."""
    examples: list[tuple[int, str]] = []
    for line_number, line in enumerate(_read(ENV_EXAMPLE).splitlines(), start=1):
        examples.extend((line_number, match.group("value")) for match in TIMEOUT_ASSIGNMENT.finditer(line))
    return examples


def test_documented_http_timeout_examples_are_valid_configuration_values(monkeypatch):
    """Every ``NEXTDNS_HTTP_TIMEOUT`` example the template shows must be accepted.

    This guard scans ``.env.example`` only. The example documents how to set the
    variable, not that it equals the default, so it only has to be a value the
    parser accepts: an unparseable or non-positive one raises
    ``ConfigurationError`` on startup, which would make the template document a
    setting that cannot be used. That is also why the guard does not require the
    example to equal the code default: deliberate override examples elsewhere in
    the documentation (``docs/configuration.md`` shows 45) are legal by design
    and are not scanned here.
    """
    examples = _documented_timeout_examples()
    assert examples, f"{ENV_EXAMPLE} must show an example NEXTDNS_HTTP_TIMEOUT value"

    for line_number, value in examples:
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", value)
        try:
            get_http_timeout()
        except ConfigurationError as error:
            pytest.fail(
                f"{ENV_EXAMPLE}:{line_number} documents NEXTDNS_HTTP_TIMEOUT={value}, which the server rejects: {error}"
            )


def test_removed_test_profile_variable_is_absent_from_operator_docs():
    """``NEXTDNS_TEST_PROFILE`` must stay out of the operator-facing docs.

    No code reads it: the e2e runner creates a throwaway profile or takes
    ``--plot-profile`` instead. The search is literal and covers every operator
    documentation surface, so a new doc page advertising the variable is caught
    without anyone remembering to add it to a list.
    """
    docs = sorted(str(path.relative_to(REPO_ROOT)) for path in (REPO_ROOT / "docs").rglob("*.md"))
    surfaces = [ENV_EXAMPLE, README, AGENT_GUIDE, *docs]

    offenders = [path for path in surfaces if "NEXTDNS_TEST_PROFILE" in _read(path)]
    assert offenders == [], (
        f"NEXTDNS_TEST_PROFILE is read by no code but is documented in {offenders}. "
        "Remove it and point operators at NEXTDNS_WRITABLE_PROFILES / NEXTDNS_READ_ONLY, "
        "which are the real write guards."
    )
