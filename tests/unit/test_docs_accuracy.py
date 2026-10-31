"""Guards that the docs describe the surface the server actually publishes.

The grouped tool surface and the ``NEXTDNS_TOOL_SURFACE`` switch were removed;
these tests keep them out of the documentation and keep every tool name the
docs name a tool to be one the server publishes. The tool contract lives in
the shipped OpenAPI document, so a doc that names a tool the document does not
generate is the server describing something it no longer is.

SPDX-License-Identifier: MIT
"""

import re
from pathlib import Path

import pytest

from nextdns_mcp.server import published_tool_names

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The files that speak for the product to a human reader.
DOC_FILES = [
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "AGENT.md",
    _REPO_ROOT / ".env.example",
    _REPO_ROOT / "scripts" / "README.md",
    *sorted((_REPO_ROOT / "docs").glob("*.md")),
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


def _docs_text() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in DOC_FILES}


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
