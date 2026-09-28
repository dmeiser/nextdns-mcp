"""Regression tests for the configuration documentation drift of issue #275.

Three defects were reported, all of them in operator-facing text:

* ``NEXTDNS_TEST_PROFILE`` was documented in ``.env.example`` and ``README.md``
  but read by no code, so an operator who set it believed they had a guard they
  did not have.
* ``.env.example`` showed ``NEXTDNS_HTTP_TIMEOUT=10`` as the example value
  directly beneath a stated default of 30 seconds, which is the value the code
  uses.
* ``docs/index.md`` claimed the transport was stdio only, although the
  streamable-HTTP transport is supported and documented.

Why reading text is legitimate here, and when it is not: #275 is a
documentation-only defect -- there is no interface to execute, so the operator
facing text *is* the behaviour, and a narrow assertion on that text is the only
thing that can hold the line. A test that greps documentation in order to prove
a *behavioural* claim guards nothing instead, because a reword flips it while
behaviour is unchanged; the configuration behaviour itself is covered by
``tests/unit/test_config.py`` and friends, not here. So every test below either
executes the configuration surface for the values the template documents, or
asserts one specific fact about a specific sentence of prose.

SPDX-License-Identifier: MIT
"""

import re
from pathlib import Path

from nextdns_mcp.config import DEFAULT_HTTP_TIMEOUT, get_http_timeout

REPO_ROOT = Path(__file__).resolve().parents[2]

# The operator-facing files that document environment variables.
ENV_EXAMPLE = ".env.example"
README = "README.md"

# ``# NEXTDNS_HTTP_TIMEOUT=30`` and the copy-paste-ready ``NEXTDNS_HTTP_TIMEOUT=30``.
TIMEOUT_EXAMPLE = re.compile(r"^#?\s*NEXTDNS_HTTP_TIMEOUT=(?P<value>\S+)\s*$", re.MULTILINE)

# The default the template promises the operator: "(default: 30)".
TIMEOUT_STATED_DEFAULT = re.compile(r"HTTP request timeout in seconds \(default:\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\)")


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"{relative_path} must exist in the repository"
    return path.read_text(encoding="utf-8")


def test_env_example_states_the_configured_http_timeout_default():
    """The default ``.env.example`` promises must be the default the code uses.

    The reported defect was the template's ``NEXTDNS_HTTP_TIMEOUT=10`` example
    contradicting the default stated right above it and used by the code; this
    guard keeps the stated default and ``config.DEFAULT_HTTP_TIMEOUT`` in step
    whichever of the two is edited.
    """
    env_example = _read(ENV_EXAMPLE)
    stated = TIMEOUT_STATED_DEFAULT.search(env_example)
    assert stated is not None, (
        f'{ENV_EXAMPLE} must state the HTTP timeout default as "HTTP request timeout in seconds (default: <seconds>)"'
    )
    assert float(stated.group("value")) == DEFAULT_HTTP_TIMEOUT, (
        f"{ENV_EXAMPLE} states an HTTP timeout default of {stated.group('value')} "
        f"but config.DEFAULT_HTTP_TIMEOUT is {DEFAULT_HTTP_TIMEOUT}"
    )


def test_documented_http_timeout_examples_are_valid_configuration_values(monkeypatch):
    """Every ``NEXTDNS_HTTP_TIMEOUT`` example the template shows must be accepted.

    The example is documentation of how to set the variable, not a claim that it
    equals the default, so it only has to be a legal, parseable value: the
    default itself is checked by the test above. This asserts the claim its own
    name makes -- the example is a real configuration value -- rather than
    implying the example must be a no-op.
    """
    examples = [match.group("value") for match in TIMEOUT_EXAMPLE.finditer(_read(ENV_EXAMPLE))]
    assert examples, f"{ENV_EXAMPLE} must show an example NEXTDNS_HTTP_TIMEOUT value"

    for value in examples:
        monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", value)
        # An undocumented, unparseable value raises ConfigurationError here,
        # which fails the test just as an assertion would.
        configured = get_http_timeout()
        assert configured == float(value), (
            f"{ENV_EXAMPLE} documents NEXTDNS_HTTP_TIMEOUT={value} but the configuration reads it back as {configured}"
        )


def test_removed_test_profile_variable_is_absent_from_operator_docs():
    """``NEXTDNS_TEST_PROFILE`` must stay out of the operator-facing docs.

    No code reads it: the e2e runner creates a throwaway profile or takes
    ``--plot-profile`` instead. It is checked in exactly these two files, by
    name, because those are the two that advertised it; a broader search of
    documentation prose would guard nothing.
    """
    offenders = [path for path in (ENV_EXAMPLE, README) if "NEXTDNS_TEST_PROFILE" in _read(path)]
    assert offenders == [], (
        f"NEXTDNS_TEST_PROFILE is read by no code but is documented in {offenders}. "
        "Remove it and point operators at NEXTDNS_WRITABLE_PROFILES / NEXTDNS_READ_ONLY, "
        "which are the real write guards."
    )
