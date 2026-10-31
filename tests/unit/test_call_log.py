"""Unit tests for the server-side call log (nextdns_mcp.call_log).

The log is the server's own account of which tools it executed, so a harness
can tell "the model read it" from "the model did nothing and the state was
already right" — something a REST read-back cannot distinguish.

Every published tool is generated from the OpenAPI document and bound to one
endpoint, so a record carries ``endpoint`` ("METHOD /path/template") and its
``write`` flag comes from the HTTP method: ``POST /profiles/{profile_id}/denylist``
changed state whatever the tool was called.

SPDX-License-Identifier: MIT
"""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from nextdns_mcp.call_log import (
    CALL_LOG_ENV,
    CallLogMiddleware,
    call_log_path,
    get_call_log,
    result_error,
)
from nextdns_mcp.client import is_write_operation
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


class TestEndpointRecord:
    """A generated tool has no ``operation`` argument, so the endpoint is the record.

    The tool body decides the request itself, so the tool name alone cannot say
    what the server sent upstream — and a harness scores a read on this record.
    The binding registered at startup (``call_log.register_endpoint``) is what
    makes the record verifiable, and the HTTP method is what makes it a write.
    """

    @pytest.mark.asyncio
    async def test_a_generated_record_names_the_endpoint_it_executed(self, monkeypatch, tmp_path):
        """The endpoint line is the record: a read leaves no trace in the API.

        No readable profile is configured, so the ACL layer answers locally
        without raising, and the log has to record that as a failure — a record
        that called it a success would let a harness believe a read happened
        that never left the process.
        """
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async with Client(build_mcp_server()) as client:
            await client.call_tool("getDenylist", {"profile_id": "abc123"})

        rows = _rows(path)
        assert rows[0]["tool"] == "getDenylist"
        assert rows[0]["endpoint"] == "GET /profiles/{profile_id}/denylist"
        assert rows[0]["profile_id"] == "abc123"
        assert "profile_id" in rows[0]["args"]
        assert rows[0]["ok"] is False
        assert rows[0]["error"]
        # The payload is recorded too: it is what makes a read verifiable.
        assert rows[0]["error"] in rows[0]["result"]

    @pytest.mark.asyncio
    async def test_a_write_is_derived_from_the_method_not_the_name(self, monkeypatch, tmp_path):
        """`addToDenylist` reads like a name, `POST` is the fact: state changed.

        A harness rejects later writes after a mutation it can see; on this
        surface the only way it can see one is the method of the endpoint that
        ran, so the POST has to be the write and the GET beside it not.
        """
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async with Client(build_mcp_server()) as client:
            await client.call_tool("addToDenylist", {"profile_id": "abc123", "id": "example.com"})
            await client.call_tool("getDenylist", {"profile_id": "abc123"})

        rows = _rows(path)
        assert [r["write"] for r in rows] == [True, False]
        assert rows[0]["endpoint"] == "POST /profiles/{profile_id}/denylist"
        # The flag comes from the same HTTP-method rule the access-control layer
        # uses, so a record and the request that produced it cannot disagree.
        assert is_write_operation("POST") and not is_write_operation("GET")


class TestLoggingIsOffUnlessConfigured:
    """No path means no record, and no record means no file.

    The middleware is installed either way, so the switch has to be in the writer:
    a server run without NEXTDNS_CALL_LOG must not create anything, and must not
    change what a call returns.
    """

    @pytest.mark.asyncio
    async def test_a_call_without_a_log_path_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv(CALL_LOG_ENV, raising=False)
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")

        async with Client(build_mcp_server()) as client:
            result = await client.call_tool("getDenylist", {"profile_id": "abc123"})

        assert list(tmp_path.iterdir()) == []
        # The call itself still answers in the contract's shape.
        assert (result.structured_content or {})["code"]

    async def test_a_middleware_built_without_a_path_writes_nothing(self, tmp_path):
        """The install site checks the path, and the writer does not take its word for it.

        A middleware constructed with an empty path must not try to open "" — that
        would log an OSError that reads like a failed call when nothing failed. The
        call still returns whatever the tool returned.
        """
        from types import SimpleNamespace

        middleware = CallLogMiddleware("")
        sentinel = object()

        async def call_next(context):
            return sentinel

        context = SimpleNamespace(message=SimpleNamespace(name="getDenylist", arguments={"profile_id": "abc123"}))
        assert await middleware.on_call_tool(context, call_next) is sentinel
        assert list(tmp_path.iterdir()) == []


class TestALocalOperationHasNoEndpoint:
    """``plotAnalytics`` renders a chart, so there is no endpoint to record.

    Every generated tool is bound to one, and the record's ``write`` flag comes from
    that binding. A local operation has none, so it must record as a read: the
    harness's pass rule rejects a state-changing call after the expected one, and a
    chart changes nothing in the account.
    """

    @pytest.mark.asyncio
    async def test_a_chart_records_no_endpoint_and_is_not_a_write(self, monkeypatch, tmp_path):
        from nextdns_mcp.tools import plots as plots_module

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        # An empty series: the fetch is not what this test is about, and the tool
        # returns a typed no-data payload without rendering anything.
        async def empty_series(method, url, params=None, json_body=None):
            return {"meta": {"series": {"times": []}}, "data": []}

        monkeypatch.setattr(plots_module, "_api_request", empty_series)

        async with Client(build_mcp_server()) as client:
            await client.call_tool("plotAnalytics", {"metric": "status", "profile_id": "abc123"})

        rows = _rows(path)
        assert rows[0]["tool"] == "plotAnalytics"
        assert "endpoint" not in rows[0]
        assert rows[0]["write"] is False


class TestCallLogMiddleware:
    @pytest.mark.asyncio
    async def test_records_the_real_tool_behind_the_disclosure_proxy(self, monkeypatch, tmp_path):
        """search_tools/call_tool are not logged; the tool they dispatch to is.

        Logging the synthetic pair would double-count discovery and, worse, hide
        which endpoint the call actually executed — the generated tool's record
        is the one that carries it.
        """
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async with Client(build_mcp_server()) as client:
            await client.call_tool("search_tools", {"query": "list profiles"})
            await client.call_tool("call_tool", {"name": "listProfiles", "arguments": {}})

        rows = _rows(path)
        assert [r["tool"] for r in rows] == ["listProfiles"]
        assert rows[0]["endpoint"] == "GET /profiles"
        assert rows[0]["write"] is False
        # No readable-profile ACL is set, so the server's own denial comes back
        # as an error payload rather than a raised exception — and the log has
        # to record it as a failure, not a success.
        assert rows[0]["ok"] is False
        assert rows[0]["error"]

    @pytest.mark.asyncio
    async def test_a_raised_failure_is_recorded_and_still_raises(self, monkeypatch, tmp_path):
        """A tool that crashes rather than reporting a payload still leaves a record.

        The server's contract is an error payload, but a tool that raises (a bug,
        a transport fault below the error handling) must not vanish from the log:
        a harness that sees no record would conclude the model never tried, when
        in fact the call blew up. The raise itself must reach the caller too.
        """
        from nextdns_mcp import spec_tools

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        path = tmp_path / "calls.jsonl"
        monkeypatch.setenv(CALL_LOG_ENV, str(path))

        async def crash(method: str, url: str, params=None, json_body=None):
            raise RuntimeError("transport blew up")

        monkeypatch.setattr(spec_tools, "_api_request_payload", crash)
        async with Client(build_mcp_server()) as client:
            with pytest.raises(ToolError):
                await client.call_tool("getDenylist", {"profile_id": "abc123"})

        rows = _rows(path)
        assert rows and rows[0]["tool"] == "getDenylist" and rows[0]["ok"] is False
        # FastMCP turns the crash into a ToolError inside the middleware chain, and the
        # record keeps that type name plus the cause, so a reader can tell a crash from
        # an ordinary error payload.
        assert rows[0]["error"].startswith("ToolError:")
        assert "transport blew up" in rows[0]["error"]

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
            await client.call_tool("listProfiles", {})
        assert _rows(path)

    def test_middleware_without_path_is_inert(self, tmp_path):
        """An explicitly empty path must not attempt to write."""
        mw = CallLogMiddleware(path="")
        assert mw._path == ""
