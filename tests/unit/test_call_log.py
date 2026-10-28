"""Unit tests for the server-side call log (nextdns_mcp.call_log).

The log is the server's own account of which tools it executed, so a harness
can tell "the model read it" from "the model did nothing and the state was
already right" — something a REST read-back cannot distinguish.

SPDX-License-Identifier: MIT
"""

import contextlib
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from nextdns_mcp.call_log import (
    CALL_LOG_ENV,
    CallLogMiddleware,
    call_log_path,
    get_call_log,
    is_write,
    result_error,
)
from nextdns_mcp.server import build_mcp_server


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestCallLogConfig:
    def test_unset_env_means_no_log(self, monkeypatch):
        monkeypatch.delenv(CALL_LOG_ENV, raising=False)
        assert call_log_path() == ""
        assert get_call_log() == []

    def test_get_call_log_tolerates_missing_and_malformed(self, monkeypatch, tmp_path):
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))
        assert get_call_log() == []
        path.write_text('{"tool": "x"}\nnot json\n\n[]\n', encoding="utf-8")
        assert get_call_log() == [{"tool": "x"}]


class TestResultError:
    class _R:
        def __init__(self, is_error=False, structured=None):
            self.is_error = is_error
            self.structured_content = structured

    def test_error_payload_counts_as_failure(self):
        assert result_error(self._R(structured={"error": "notFound"})) == "notFound"

    def test_raised_error_counts_as_failure(self):
        assert result_error(self._R(is_error=True))

    def test_success_has_no_error(self):
        assert result_error(self._R(structured={"data": []})) == ""


class TestCallLogMiddleware:
    @pytest.mark.asyncio
    async def test_records_the_real_tool_behind_the_disclosure_proxy(self, monkeypatch, tmp_path):
        """search_tools/call_tool are not logged; the tool they dispatch to is."""
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async with Client(build_mcp_server()) as client:
            await client.call_tool("search_tools", {"query": "list profiles"})
            await client.call_tool("call_tool", {"name": "manageProfiles", "arguments": {"operation": "list"}})

        rows = _rows(path)
        assert [r["tool"] for r in rows] == ["manageProfiles"]
        assert rows[0]["operation"] == "list"
        assert rows[0]["write"] is False
        # No readable-profile ACL is set, so the server's own denial comes back
        # as an error payload rather than a raised exception — and the log has
        # to record it as a failure, not a success.
        assert rows[0]["ok"] is False
        assert rows[0]["error"]
        # The payload is recorded too: it is what makes a read verifiable.
        assert rows[0]["error"] in rows[0]["result"]

    @pytest.mark.asyncio
    async def test_write_operations_are_flagged_so_later_writes_can_be_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async with Client(build_mcp_server()) as client:
            for call in (
                {"operation": "add", "list_type": "denylist", "profile_id": "x"},
                {"operation": "get", "list_type": "denylist", "profile_id": "x"},
            ):
                with contextlib.suppress(Exception):  # denied upstream; still recorded
                    await client.call_tool("manageLists", call)

        rows = _rows(path)
        assert [r["write"] for r in rows] == [True, False]
        assert is_write("add") and is_write("clear") and not is_write("get")

    @pytest.mark.asyncio
    async def test_schema_validation_failure_is_recorded_and_still_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async with Client(build_mcp_server()) as client:
            with pytest.raises(ToolError):
                await client.call_tool("manageLists", {"operation": "get", "list_type": "bogus", "profile_id": "x"})

        rows = _rows(path)
        assert rows and rows[0]["tool"] == "manageLists" and rows[0]["ok"] is False

    @pytest.mark.asyncio
    async def test_server_logs_only_when_the_env_var_is_set(self, monkeypatch, tmp_path):
        from nextdns_mcp.openapi import create_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.delenv(CALL_LOG_ENV, raising=False)
        path = tmp_path / "calls.jsonl"
        monkeypatch.setattr("nextdns_mcp.openapi.call_log_path", lambda: str(path))
        create_mcp_server()  # must not raise, and must not write
        assert not path.exists()

        monkeypatch.setenv(CALL_LOG_ENV, str(path))
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("manageProfiles", {"operation": "list"})
        assert _rows(path)

    def test_middleware_without_path_is_inert(self, tmp_path):
        """An explicitly empty path must not attempt to write."""
        mw = CallLogMiddleware(path="")
        assert mw._path == ""
