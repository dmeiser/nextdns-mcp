"""Guards that the docs describe the surface the server actually publishes.

The grouped tool surface and the ``NEXTDNS_TOOL_SURFACE`` switch were removed;
these tests keep them out of the documentation and keep every tool name the
docs name a tool to be one the server publishes. The tool contract lives in
the shipped OpenAPI document, so a doc that names a tool the document does not
generate is the server describing something it no longer is.

The same rule covers the values and files the docs name: the documented
``NEXTDNS_TOOL_SEARCH`` mode set and default are compared against the runtime
constants, every ``src/``/``tests/``/``scripts/`` python path named in AGENT.md
has to exist on disk, and no doc may advertise a tool-search mode the runtime
rejects or a profile-id length the client does not accept.

SPDX-License-Identifier: MIT
"""

import re
from pathlib import Path

import pytest

from nextdns_mcp.config import DEFAULT_TOOL_SEARCH_MODE, TOOL_SEARCH_MODES
from nextdns_mcp.server import published_tool_names

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The files that speak for the product to a human reader.
DOC_FILES = [
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "AGENT.md",
    _REPO_ROOT / ".env.example",
    _REPO_ROOT / "scripts" / "README.md",
    *sorted((_REPO_ROOT / "docs").glob("*.md")),
    *sorted((_REPO_ROOT / "docs" / "architecture").glob("*.md")),
]

# Names that existed only on the removed surface. Any appearance in the docs -
# as a tool, an environment value, or an argument form - means the doc is
# describing the server that was torn out.
REMOVED_SURFACE = (
    "NEXTDNS_TOOL_SURFACE",
    "--tool-surface",
    "manageProfiles",
    "manageSettings",
    "manageLists",
    "manageRewrites",
    "manageLogs",
    "queryAnalytics",
    'operation="',
)

# Synthetic discovery tools and the withheld operation appear in the docs as
# themselves, not as published operation tools.
DOC_NAMED_EXCUSES = {"search_tools", "call_tool", "streamLogs"}

# A camelCase token starting with one of these verbs reads as a tool name.
_TOOL_NAME_PREFIXES = (
    "get",
    "add",
    "update",
    "remove",
    "replace",
    "delete",
    "list",
    "create",
    "clear",
    "download",
    "plot",
    "doh",
)


def _strip_migration_table(text: str) -> str:
    # Narrow exemption: the 4.0.0 migration table is the one place the
    # removed (old) tool names are allowed to appear, so users can map them
    # to their replacements. Only the fenced migration-table section is
    # stripped; the ban still applies everywhere else.
    import re as _re

    return _re.sub(r"<!-- migration-table:start -->.*?<!-- migration-table:end -->", "", text, flags=_re.S)


def _docs_text() -> dict[str, str]:
    return {path.name: _strip_migration_table(path.read_text(encoding="utf-8")) for path in DOC_FILES}


class TestRemovedSurfaceStaysOutOfDocs:
    @pytest.mark.parametrize("name", sorted(_docs_text()))
    def test_no_reference_to_the_removed_tool_surface(self, name):
        text = _docs_text()[name]
        found = [token for token in REMOVED_SURFACE if token in text]
        assert not found, f"{name} still references the removed surface: {found}"

    def test_readme_states_the_document_is_the_tool_contract(self):
        text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "tool contract" in text
        assert "does not generate tools from an OpenAPI spec" not in text


class TestDocsNameRealTools:
    def test_every_tool_named_in_the_docs_is_published(self):
        published = set(published_tool_names())
        pattern = re.compile(r"`([A-Za-z][A-Za-z0-9]*)`")
        for name, text in _docs_text().items():
            for token in pattern.findall(text):
                if token in DOC_NAMED_EXCUSES:
                    continue
                if not token.startswith(_TOOL_NAME_PREFIXES):
                    continue
                if not re.search(r"[a-z][A-Z]", token):  # snake_case and plain words are not tool names
                    continue
                assert token in published, f"{name} names {token!r}, which the server does not publish"

    def test_usage_doc_covers_both_local_operations(self):
        text = (_REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
        assert "plotAnalytics" in text and "dohLookup" in text

    def test_configuration_doc_documents_the_switches_that_exist(self):
        text = (_REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        assert "NEXTDNS_TOOL_SEARCH" in text
        assert "Tool catalog" in text


# A tool-search mode the runtime no longer accepts. `regex` was dropped before this
# server shipped: it ranks nothing (registration order inside a pattern match) and no
# live-model validation of the disclosure contract ran under it. A doc that still
# names it sends an operator to a value that fails startup with ConfigurationError.
STALE_TOOL_SEARCH_MODES = ("regex",)

# A NextDNS profile id is 6 OR 10 lowercase alphanumeric characters
# (client.SAFE_PROFILE_ID_PATTERN). A doc that names one length tells a caller holding
# a valid 10-character id that the id it was just given is invalid.
PROFILE_ID_LENGTH_CLAIM = re.compile(r"(?i)\b(?:6|six)[ -]?characters?\b|\bsix hexadecimal\b|\bexactly (?:6|six)\b")

# A python file named by path inside a doc, e.g. `tests/unit/test_mcp_server.py`.
PYTHON_PATH = re.compile(r"(?:src|tests|scripts)/[A-Za-z0-9_./-]*\.py")

_CONFIGURATION_DOC = _REPO_ROOT / "docs" / "configuration.md"
_AGENT_DOC = _REPO_ROOT / "AGENT.md"


def _table_row(text: str, first_cell: str) -> list[str]:
    """The cells of a markdown table row, found by its first cell."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"| {first_cell} |"):
            return [cell.strip() for cell in stripped.strip("|").split("|")]
    raise AssertionError(f"no table row for {first_cell!r} in the configuration reference")


class TestDocumentedValuesMatchRuntime:
    def test_documented_tool_search_modes_and_default_are_the_code_constants(self):
        """The reference table is the operator's source for this switch."""
        row = _table_row(_CONFIGURATION_DOC.read_text(encoding="utf-8"), "NEXTDNS_TOOL_SEARCH")
        assert row[1] == f"string ({'/'.join(TOOL_SEARCH_MODES)})", "documented modes are not the accepted ones"
        assert row[2] == DEFAULT_TOOL_SEARCH_MODE, "documented default is not the runtime default"
        # The row also has to say what the listing looks like in that mode, or the
        # default reads as a full listing to whoever sets nothing. Columns are
        # Variable | Type | Default | Required | Description.
        assert "search_tools" in row[4] and "call_tool" in row[4], "the row must name the synthetic disclosure pair"

    def test_no_doc_advertises_a_tool_search_mode_the_runtime_rejects(self):
        for name, text in _docs_text().items():
            found = [mode for mode in STALE_TOOL_SEARCH_MODES if mode in text]
            assert not found, f"{name} advertises a tool-search mode the runtime rejects: {found}"

    def test_no_doc_claims_profile_ids_are_one_fixed_length(self):
        for name, text in _docs_text().items():
            found = sorted({match.group(0) for match in PROFILE_ID_LENGTH_CLAIM.finditer(text)})
            assert not found, f"{name} states a single profile-id length: {found}; ids are 6 or 10 characters"


class TestDocsNameFilesThatExist:
    def test_every_python_path_named_in_agent_md_exists(self):
        """AGENT.md is what contributors are pointed at; a deleted file named in it is a
        guard that no longer exists wearing the name of a guard."""
        text = _AGENT_DOC.read_text(encoding="utf-8")
        missing = sorted({token for token in PYTHON_PATH.findall(text) if not (_REPO_ROOT / token).exists()})
        assert not missing, f"AGENT.md names python files that do not exist: {missing}"
