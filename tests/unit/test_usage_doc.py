"""Unit tests for usage documentation generation and synchronization.

SPDX-License-Identifier: MIT
"""

from pathlib import Path

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
