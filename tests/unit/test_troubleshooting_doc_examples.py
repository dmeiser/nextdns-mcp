"""Doc-sync tests for the examples in docs/troubleshooting.md (issue #276).

The "Invalid JSON or array expected" section documents the shape ``manageLists``
accepts for its bulk parameters. Issue #255 tightened ``replace`` entry
validation, and the documented array-of-strings example was left behind, so the
page told users to send a payload the code rejects with ``invalid_argument``.
These tests execute the documented examples against the real tool so the drift
is caught by CI instead of by a user hitting a validation error.
"""

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nextdns_mcp import server
from nextdns_mcp.tools import lists as lists_module

REPO_ROOT = Path(__file__).resolve().parents[2]
TROUBLESHOOTING = REPO_ROOT / "docs" / "troubleshooting.md"
SECTION_HEADING = "## Invalid JSON or array expected"

# A single-quoted JSON array string, the form the section tells users to paste
# into a shell.
_EXAMPLE_RE = re.compile(r"'(\[[^']*\])'")


def _section() -> str:
    text = TROUBLESHOOTING.read_text(encoding="utf-8")
    assert TROUBLESHOOTING.exists(), "docs/troubleshooting.md must exist in the repository"
    start = text.index(SECTION_HEADING)
    rest = text[start + len(SECTION_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _documented_example() -> list[object]:
    """Return the documented example payload, as the code would parse it."""
    match = _EXAMPLE_RE.search(_section())
    assert match, f"no single-quoted JSON array example found under {SECTION_HEADING!r}"
    return json.loads(match.group(1))


@pytest.fixture(autouse=True)
def open_profile_access(monkeypatch):
    """Allow profile access so the example reaches validation, not the ACL."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


async def test_documented_replace_example_is_accepted(monkeypatch):
    """The example the page tells users to send must not be rejected locally (#276)."""
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(lists_module, "_api_request_payload", request)

    result = await server.manageLists(
        "denylist",
        "replace",
        "abc123",
        entries=_documented_example(),
    )

    assert result == {"ok": True}, f"documented example was rejected: {result}"
    request.assert_awaited_once()
    assert request.await_args.kwargs["json_body"] == _documented_example()
