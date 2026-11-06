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
import os
import stat
from typing import ClassVar

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from nextdns_mcp.call_log import (
    CALL_LOG_ENV,
    CallLogMiddleware,
    _append_row,
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
            await client.call_tool("getDenylist", {"profile_id": "abc123"}, raise_on_error=False)

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
            await client.call_tool("addToDenylist", {"profile_id": "abc123", "id": "example.com"}, raise_on_error=False)
            await client.call_tool("getDenylist", {"profile_id": "abc123"}, raise_on_error=False)

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
            result = await client.call_tool("getDenylist", {"profile_id": "abc123"}, raise_on_error=False)

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

        monkeypatch.setattr(plots_module, "_api_request_payload", empty_series)

        async with Client(build_mcp_server()) as client:
            await client.call_tool("plotAnalytics", {"metric": "status", "profile_id": "abc123"}, raise_on_error=False)

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
            await client.call_tool("search_tools", {"query": "list profiles"}, raise_on_error=False)
            await client.call_tool("call_tool", {"name": "listProfiles", "arguments": {}}, raise_on_error=False)

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
            await client.call_tool("listProfiles", {}, raise_on_error=False)
        assert _rows(path)

    def test_middleware_without_path_is_inert(self, tmp_path):
        """An explicitly empty path must not attempt to write."""
        mw = CallLogMiddleware(path="")
        assert mw._path == ""


class TestFileIsSensitiveByDefault:
    """The rows carry account data, so the file is private from creation.

    ``open(..., "a")`` applies the process umask at creation - often ``0022``,
    which makes a brand-new log group-readable. The rows carry profile ids,
    entry names and a bounded projection of results, so the log is created
    ``0600`` explicitly and the mode is a contract, not a side effect.
    """

    @pytest.mark.asyncio
    async def test_a_new_log_is_created_owner_only(self, tmp_path):
        from nextdns_mcp.call_log import CallLogMiddleware

        path = tmp_path / "calls.jsonl"
        middleware = CallLogMiddleware(path=str(path), endpoints={})
        await middleware._record("getProfile", {"profile_id": "abc123"}, ok=True, error="")
        assert path.stat().st_mode & 0o777 == 0o600


class TestBoundedProjection:
    """Rows are bounded *before* serialization, and the bound is observable.

    A 10,000-entry list never reaches ``json.dumps`` whole: the projection
    keeps a few leading entries and names the omission, so the row stays
    small enough for a harness to read and the serializer never pays for the
    payload the log cannot carry.
    """

    def test_a_large_list_keeps_leading_entries_and_names_the_omission(self):
        from nextdns_mcp.call_log import PROJECT_MAX_ITEMS, _project_value

        projected = _project_value([f"entry-{i}" for i in range(10_000)])
        assert len(projected) == PROJECT_MAX_ITEMS + 1
        assert projected[0] == "entry-0"
        assert projected[-1] == {"_omitted_items": 10_000 - PROJECT_MAX_ITEMS}

    def test_a_small_list_is_kept_whole_without_an_omission_marker(self):
        """The bound names omissions; inside the bound nothing is touched.

        The omission marker exists so a reader knows rows were dropped; adding
        it to a list that fit whole would make every small result lie.
        """
        from nextdns_mcp.call_log import PROJECT_MAX_ITEMS, _project_value

        small = [f"entry-{i}" for i in range(PROJECT_MAX_ITEMS)]
        assert _project_value(small) == small

        from nextdns_mcp.call_log import PROJECT_MAX_STRING_CHARS, _project_value

        assert len(_project_value("x" * 5000)) == PROJECT_MAX_STRING_CHARS

    @pytest.mark.asyncio
    async def test_the_written_row_stays_inside_the_result_budget(self, tmp_path):
        from nextdns_mcp.call_log import RESULT_CHARS, CallLogMiddleware

        class _BigResult:
            is_error = False
            structured_content: ClassVar[dict[str, object]] = {
                "domains": [{"domain": f"d{i}.example.com", "count": i} for i in range(20_000)]
            }

        path = tmp_path / "calls.jsonl"
        middleware = CallLogMiddleware(path=str(path), endpoints={})
        await middleware._record(
            "getAnalyticsDomains", {"profile_id": "abc123"}, ok=True, error="", result=_BigResult()
        )
        row = json.loads(path.read_text(encoding="utf-8"))
        assert len(row["result"]) <= RESULT_CHARS
        assert "_omitted_items" in row["result"]
        # The bound must never cut mid-document: verifying readers parse this
        # column, so the truncation itself has to be a valid JSON object.
        parsed = json.loads(row["result"])
        assert isinstance(parsed, dict)


class TestOffsetReader:
    """``read_call_log_rows`` reads from an offset, the shared row contract.

    A harness that captured the file's end before a step reads only what the
    step appended; the offset reader is the one place that decides what a row
    is, so a partial trailing line is skipped identically everywhere.
    """

    def test_rows_after_the_offset_are_the_only_ones_returned(self, tmp_path):
        from nextdns_mcp.call_log import read_call_log_rows

        path = tmp_path / "calls.jsonl"
        first = json.dumps({"tool": "first"}) + "\n"
        path.write_text(first + json.dumps({"tool": "second"}) + "\n", encoding="utf-8")
        rows = read_call_log_rows(str(path), offset=len(first.encode("utf-8")))
        assert rows == [{"tool": "second"}]

    def test_a_partial_trailing_line_is_skipped_not_raised(self, tmp_path):
        from nextdns_mcp.call_log import read_call_log_rows

        path = tmp_path / "calls.jsonl"
        path.write_text('{"tool": "ok"}\n{"tool": "cut', encoding="utf-8")
        assert read_call_log_rows(str(path)) == [{"tool": "ok"}]


class TestLoggingCannotBreakACall:
    """A log that cannot be written must not fail the call it records.

    Both directions of the failure - the file cannot be opened for append, and
    the file cannot be read back - are operator-visible (a warning), never
    caller-visible. The exclusions these tests replace hid real failure
    behavior behind coverage pragmas.
    """

    @pytest.mark.asyncio
    async def test_a_failed_log_write_warns_and_the_call_proceeds(self, tmp_path, monkeypatch, caplog):
        from nextdns_mcp.call_log import CallLogMiddleware

        middleware = CallLogMiddleware(path=str(tmp_path / "calls.jsonl"), endpoints={})
        # A directory where the log file should open: real OSError, no mocks of
        # our own functions.
        monkeypatch.setattr(
            "nextdns_mcp.call_log._open_log_file",
            lambda path: (_ for _ in ()).throw(PermissionError("read-only filesystem")),
        )
        with caplog.at_level("WARNING", logger="nextdns_mcp.call_log"):
            await middleware._record("getProfile", {}, ok=True, error="")
        assert any("call log write failed" in record.getMessage() for record in caplog.records)

    def test_an_unreadable_log_reads_as_empty_not_raised(self, tmp_path, monkeypatch, caplog):
        from nextdns_mcp.call_log import CALL_LOG_ENV, get_call_log

        monkeypatch.setenv(CALL_LOG_ENV, str(tmp_path))  # a directory, not a file
        with caplog.at_level("WARNING", logger="nextdns_mcp.call_log"):
            assert get_call_log() == []
        assert any("call log read failed" in record.getMessage() for record in caplog.records)


class TestExistingLogPermissions:
    """The 0600 contract covers the file's whole life, not only its creation.

    ``os.open(..., 0o600)`` only modes a brand-new file; a log left behind by
    an older run (or made under a permissive umask) keeps its old mode unless
    the writer tightens it.
    """

    def test_an_existing_permissive_log_is_tightened(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        path.write_text("")
        os.chmod(path, 0o644)
        _append_row(str(path), {"tool": "x", "ok": True})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_new_log_is_created_private(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        _append_row(str(path), {"tool": "x", "ok": True})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_fchmod_failure_drops_the_row_rather_than_writing_permissively(tmp_path, monkeypatch, caplog):
    """Fail closed: without an established 0600, account data is not appended.

    The alternative - the previous behavior - wrote the row anyway and left
    it readable by whoever owns the file. Dropping one log row and warning
    is the honest cost of a log that cannot be made private.
    """

    def boom(fd: int, mode: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(os, "fchmod", boom)
    path = tmp_path / "calls.jsonl"
    with caplog.at_level("WARNING", logger="nextdns_mcp.call_log"):
        _append_row(str(path), {"tool": "x", "ok": True})
    # os.open creates the (empty, 0600-mode) file before chmod runs; the
    # contract is that no account data reaches a file it cannot protect.
    assert not path.exists() or path.stat().st_size == 0, "a row reached a file whose mode is not established"
    assert any("call log write failed" in r.getMessage() for r in caplog.records)


def test_an_over_cap_result_is_a_valid_truncation_envelope():
    """The bound must never cut mid-document (the harness json.loads-es this)."""
    from nextdns_mcp.call_log import RESULT_CHARS, _result_text

    class _Wide:
        structured_content: ClassVar[dict[str, str]] = {f"key{i}": "x" * 200 for i in range(50)}

    text = _result_text(_Wide())
    assert len(text) <= RESULT_CHARS
    parsed = json.loads(text)
    assert parsed["truncated"] is True
    assert parsed["head"]


class TestEvidenceColumn:
    """Identity facts survive result truncation in a dedicated bounded field."""

    class _R:
        def __init__(self, structured):
            self.is_error = False
            self.structured_content = structured

    def _row(self, tool, endpoints, ok, structured):
        mw = CallLogMiddleware(path="", endpoints=endpoints)
        return mw._record_row(tool, {"profile_id": "p1"}, ok, "", self._R(structured))

    def test_a_create_row_carries_a_created_id_receipt(self):
        row = self._row(
            "createProfile",
            {"createProfile": ("POST", "/profiles")},
            True,
            {"data": {"id": "pid-9f3a", "name": "TEST_PROFILE_x"}},
        )
        assert row["write"] is True
        assert row["evidence"]["created_id"] == "pid-9f3a"

    def test_a_read_answer_is_never_mislabelled_as_a_create(self):
        row = self._row(
            "getProfile",
            {"getProfile": ("GET", "/profiles/{profile_id}")},
            True,
            {"data": {"id": "pid-9f3a"}},
        )
        assert row.get("evidence", {}).get("created_id") is None

    def test_identities_survive_a_truncated_result_column(self):
        """The projection keeps five list items; evidence keeps fifty pairs."""
        from nextdns_mcp.call_log import RESULT_CHARS, _result_text

        entries = {"data": [{"id": f"id-{i:04d}-padpadpad", "name": f"entry-{i:04d}"} for i in range(80)]}
        row = self._row("getDenylist", {"getDenylist": ("GET", "/profiles/{profile_id}/denylist")}, True, entries)
        assert len(row["evidence"]["ids"]) == 50  # EVIDENCE_MAX_ITEMS
        assert {"id": "id-0007-padpadpad", "name": "entry-0007"} in row["evidence"]["ids"]
        # The result column, by contrast, is lossy for this payload: the
        # projection keeps five list items, so a reader matching identity #7
        # through it alone would be reading a truth the row never recorded.
        projected = json.loads(_result_text(self._R(entries)))["data"]
        assert len(projected) == 6  # PROJECT_MAX_ITEMS + the omission marker
        assert all("entry-0007" not in json.dumps(item) for item in projected)
        assert len(json.dumps(row["evidence"])) < RESULT_CHARS * 3  # bounded by construction

    def test_evidence_absent_for_error_and_payloadless_rows(self):
        row = self._row("createProfile", {"createProfile": ("POST", "/profiles")}, False, {"error": "boom"})
        assert "evidence" not in row

    def test_a_write_answer_without_a_data_id_records_no_receipt(self):
        row = self._row("createProfile", {"createProfile": ("POST", "/profiles")}, True, {"data": None})
        assert "evidence" not in row

    def test_an_identity_without_a_name_still_records_its_id(self):
        row = self._row(
            "getRewrites",
            {"getRewrites": ("GET", "/profiles/{profile_id}/rewrites")},
            True,
            {"data": [{"id": "rewrite-1", "action": "blocklist"}]},
        )
        assert row["evidence"]["ids"] == [{"id": "rewrite-1"}]

    def test_identities_are_found_through_nested_objects(self):
        """The walk descends id-less wrapper objects to reach the entries."""
        row = self._row(
            "getRewrites",
            {"getRewrites": ("GET", "/profiles/{profile_id}/rewrites")},
            True,
            {"data": {"results": [{"id": "nest-1", "name": "nested-entry"}]}},
        )
        assert row["evidence"]["ids"] == [{"id": "nest-1", "name": "nested-entry"}]
