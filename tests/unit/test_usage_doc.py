"""Unit tests for usage documentation generation and synchronization.

SPDX-License-Identifier: MIT
"""

from pathlib import Path

import pytest

from nextdns_mcp.usage import server_instructions
from scripts.generate_usage_doc import HEADER, generate_usage_doc, main


def test_generate_usage_doc_content():
    """generate_usage_doc should produce header plus nextdns_usage_guide()."""
    content = generate_usage_doc()
    assert content.startswith(HEADER)
    assert "# NextDNS MCP Server Usage Guide" in content
    assert "parental_categories" in content
    assert "parental_services" in content
    assert 'entry_id="<id-from-list>"' in content
    assert "Delete a DNS rewrite" in content
    assert content.endswith("\n")


def test_docs_usage_md_in_sync():
    """Verify that docs/usage.md matches generate_usage_doc() output."""
    repo_root = Path(__file__).resolve().parents[2]
    docs_usage = repo_root / "docs" / "usage.md"
    assert docs_usage.exists(), "docs/usage.md must exist in the repository"
    actual = docs_usage.read_text(encoding="utf-8")
    expected = generate_usage_doc()
    assert actual == expected, (
        "docs/usage.md is out of sync with src/nextdns_mcp/usage.py! "
        "Run `uv run python scripts/generate_usage_doc.py` to regenerate."
    )


def test_main_check_flag_success():
    """main(['--check']) should exit 0 when docs/usage.md is up to date."""
    assert main(["--check"]) == 0


def test_main_check_flag_out_of_date(tmp_path, monkeypatch, capsys):
    """main(['--check']) should exit 1 when target file content differs."""
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()
    fake_target = fake_docs / "usage.md"
    fake_target.write_text("stale content", encoding="utf-8")

    monkeypatch.setattr("scripts.generate_usage_doc.Path.resolve", lambda self: tmp_path / "scripts" / "dummy.py")
    result = main(["--check"])
    assert result == 1
    err = capsys.readouterr().err
    assert "is out of date" in err


def test_main_check_flag_missing_file(tmp_path, monkeypatch, capsys):
    """main(['--check']) should exit 1 when target file does not exist."""
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()

    monkeypatch.setattr("scripts.generate_usage_doc.Path.resolve", lambda self: tmp_path / "scripts" / "dummy.py")
    result = main(["--check"])
    assert result == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_main_generate_writes_file(tmp_path, monkeypatch, capsys):
    """main([]) should write the generated content to target file."""
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()

    monkeypatch.setattr("scripts.generate_usage_doc.Path.resolve", lambda self: tmp_path / "scripts" / "dummy.py")
    result = main([])
    assert result == 0
    fake_target = fake_docs / "usage.md"
    assert fake_target.exists()
    assert fake_target.read_text(encoding="utf-8") == generate_usage_doc()
    out = capsys.readouterr().out
    assert "Updated" in out


def test_main_uses_sys_argv(monkeypatch):
    """main() with no arguments should fall back to sys.argv."""
    monkeypatch.setattr("sys.argv", ["generate_usage_doc.py", "--check"])
    assert main() == 0


# =========================================================================== #
# MCP initialize instructions (the server's own injection channel)
# =========================================================================== #


class TestServerInstructions:
    """Instructions the server sends at initialize, not text a harness injects."""

    @pytest.mark.parametrize("mode", ["regex", "bm25"])
    def test_a_small_catalog_is_described_by_name(self, mode):
        """Eight tools cost less to name than every caller costs to search for them.

        Full run 6: 19 of 36 failures died between searching and calling - the
        caller's own chained expression threw before `call_tool` ran. Naming the
        catalog removes the hop, and the names are the server's own contract.
        """
        from nextdns_mcp.server import _GROUPED_TOOLS
        from nextdns_mcp.usage import ADVERTISE_CATALOG_MAX_TOOLS

        names = [tool.__name__ for tool in _GROUPED_TOOLS]
        assert len(names) <= ADVERTISE_CATALOG_MAX_TOOLS, "this server's own catalog must fit the rule"
        text = server_instructions(mode, names)
        for name in names:
            assert f"`{name}`" in text, f"instructions do not name {name}"
        assert "call_tool" in text

    @pytest.mark.parametrize("mode", ["regex", "bm25"])
    def test_a_large_catalog_is_pointed_at_search_instead(self, mode):
        """The rule is size, not secrecy: a big catalog still hides its names."""
        from nextdns_mcp.usage import ADVERTISE_CATALOG_MAX_TOOLS

        many = [f"tool{index}" for index in range(ADVERTISE_CATALOG_MAX_TOOLS + 1)]
        text = server_instructions(mode, many)
        assert "does not advertise tool names" in text
        assert "tool0" not in text

    @pytest.mark.parametrize("mode", ["off", "regex", "bm25"])
    def test_nothing_is_named_when_the_caller_gives_no_names(self, mode):
        """The default stays silent, so nothing depends on the caller passing them."""
        from nextdns_mcp.server import _GROUPED_TOOLS

        text = server_instructions(mode)
        for tool in _GROUPED_TOOLS:
            assert tool.__name__ not in text, f"instructions leaked tool name {tool.__name__}"

    def test_the_built_server_publishes_its_own_names(self, monkeypatch):
        """The names come from the registered tools, not a second list to drift."""
        from nextdns_mcp.server import _GROUPED_TOOLS, build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        text = build_mcp_server().instructions
        for tool in _GROUPED_TOOLS:
            assert f"`{tool.__name__}`" in text, f"{tool.__name__} missing from the published instructions"

    @pytest.mark.parametrize("mode", ["off", "regex", "bm25"])
    def test_says_nothing_about_any_agent_cli(self, mode):
        text = server_instructions(mode).casefold()
        for marker in ("opencode", "claude", "code mode", "execute tool", "system prompt"):
            assert marker not in text, f"instructions mention {marker!r}"

    @pytest.mark.parametrize("mode", ["off", "regex", "bm25"])
    def test_describes_itself_and_never_instructs_behaviour(self, mode):
        """Facts about this server only — no behaviour coaching.

        A line like "do exactly what was asked, and nothing beyond it" was a
        harness failure mode (one step's model deleted its own write) speaking
        through the product. It belongs nowhere the server talks.
        """
        text = server_instructions(mode)
        assert "hint" in text
        for coaching in ("undo", "revert", "tidy up", "nothing beyond it", "stop", "never guess"):
            assert coaching not in text.casefold(), f"instructions coach behaviour with {coaching!r}"

    def test_disclosure_mechanism_only_when_disclosure_is_on(self):
        assert "advertise" not in server_instructions("off").casefold()
        assert "advertise" in server_instructions("bm25").casefold()

    def test_create_mcp_server_publishes_them(self, monkeypatch):
        from nextdns_mcp.openapi import create_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        assert "# NextDNS MCP server" in create_mcp_server().instructions
