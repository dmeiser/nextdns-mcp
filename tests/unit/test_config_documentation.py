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
from nextdns_mcp.surface import validate_tool_surface

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


# The tool surface is documented in one section of docs/configuration.md and one
# block of .env.example. Its description is a set of claims about the catalog —
# which values are accepted, how many tools each surface publishes, and which
# tools exist on only one of them — so the guard compares those claims against
# the catalog the server actually builds rather than against a sentence.
CONFIG_DOC = "docs/configuration.md"
SURFACE_SECTION = "## Tool Surface"
SURFACE_ASSIGNMENT = re.compile(r"NEXTDNS_TOOL_SURFACE=(?P<value>[^\s#]*)")


def _surface_section() -> str:
    """The section describing the two catalogs, up to the next heading."""
    text = _read(CONFIG_DOC)
    assert SURFACE_SECTION in text, f"{CONFIG_DOC} must document the tool surface"
    start = text.index(SURFACE_SECTION) + len(SURFACE_SECTION)
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_documented_surface_values_are_the_values_the_code_accepts(monkeypatch):
    """Every surface value the operator docs name must be one the parser accepts.

    The reverse matters just as much: a surface the code accepts but the docs
    never mention is a setting operators cannot find, and a template that
    advertises a value startup rejects documents a setting that will not boot.
    """
    from nextdns_mcp.surface import _SURFACES

    section = _surface_section()
    documented_row = re.search(r"\| NEXTDNS_TOOL_SURFACE \| string \((?P<values>[^)]*)\)", _read(CONFIG_DOC))
    assert documented_row, f"{CONFIG_DOC} must document NEXTDNS_TOOL_SURFACE in its environment table"
    row_values = documented_row.group("values").split("/")

    assert sorted(row_values) == sorted(_SURFACES), (
        f"the table row documents {row_values}, code accepts {list(_SURFACES)}"
    )
    for value in _SURFACES:
        assert value in section, f"{SURFACE_SECTION} must describe {value!r}"
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", value)
        assert validate_tool_surface() == value

    examples = [
        (n, m.group("value"))
        for n, line in enumerate(_read(ENV_EXAMPLE).splitlines(), start=1)
        for m in SURFACE_ASSIGNMENT.finditer(line)
    ]
    assert examples, f"{ENV_EXAMPLE} must show an example NEXTDNS_TOOL_SURFACE value"
    for line_number, value in examples:
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", value)
        try:
            validate_tool_surface()
        except ConfigurationError as error:
            pytest.fail(
                f"{ENV_EXAMPLE}:{line_number} documents NEXTDNS_TOOL_SURFACE={value}, which startup rejects: {error}"
            )


def test_documented_catalog_sizes_match_the_catalogs_the_server_builds():
    """The tool counts in the surface section must be the real ones.

    The atomic catalog is generated from the bundled spec, so an endpoint added
    to the spec changes the number a caller reads in the docs. Asserting the
    digits against the code means the sentence goes stale in the test, not in the
    documentation.
    """
    from nextdns_mcp.atomic_tools import atomic_tool_names, load_spec, parse_operations
    from nextdns_mcp.server import _GROUPED_TOOLS

    spec_operations = len(parse_operations(load_spec()))
    registered = len(atomic_tool_names())
    section = _surface_section()

    assert str(registered) in section, f"the section must state the {registered} registered atomic tools"
    assert str(spec_operations) in section, f"the section must state the {spec_operations} spec operations"
    assert str(len(_GROUPED_TOOLS)) in section, f"the section must state the {len(_GROUPED_TOOLS)} grouped tools"


def test_documented_tools_exist_on_the_surface_they_are_described_on():
    """Names the section attributes to a surface must be real tools of that surface.

    `dohLookup` and `plotAnalytics` are documented as grouped-only because
    neither has an API endpoint to generate a tool from; the guard derives that
    set from the catalogs instead of trusting the prose, so a tool that gains or
    loses an endpoint moves the expectation with it.
    """
    from nextdns_mcp.atomic_tools import atomic_tool_names
    from nextdns_mcp.server import _GROUPED_TOOLS

    section = _surface_section()
    grouped_names = [tool.__name__ for tool in _GROUPED_TOOLS]
    atomic_names = set(atomic_tool_names())
    grouped_only = [name for name in grouped_names if name not in atomic_names]

    assert grouped_only, "the grouped surface is expected to cover more than the API catalog"
    for name in grouped_names:
        assert name in section, f"the section must name the grouped tool {name}"
    for name in grouped_only:
        assert name in section, f"the section must call out {name} as grouped-only"
    assert "streamLogs" in section, "the section must explain the spec operation that is not published"
