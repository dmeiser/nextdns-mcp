"""Read-only gating and execution audit for ``generate_prefab_ui``.

The generator executes submitted code, so it is not a read: ``NEXTDNS_READ_ONLY``
has to withhold it, and every call it does make has to be auditable — the call
log records it as a write with a parseable argument column and the digest of the
code it ran, and the guard paths leave a log line behind.

SPDX-License-Identifier: MIT
"""

import hashlib
import json
import logging
from types import SimpleNamespace

import pytest
from fastmcp import Client

from nextdns_mcp.call_log import CallLogMiddleware
from nextdns_mcp.call_log.evidence import _result_evidence
from nextdns_mcp.call_log.projection import ARGS_CHARS, _args_text, _code_sha256
from nextdns_mcp.catalog import enabled_generative_ui_tool_names, register_generative_ui_provider
from nextdns_mcp.generative import (
    MAX_CODE_BYTES,
    MAX_DATA_BYTES,
    PrefabGenerativeUI,
    _execute_within_budget,
    _validated_payload,
)
from nextdns_mcp.generative_sandbox import DenoPyodideSandbox, _BoundedStdout, _reap_process
from nextdns_mcp.server import build_mcp_server

CODE = "app = PrefabApp(view=Label(text='hi'))"
CODE_SHA256 = hashlib.sha256(CODE.encode("utf-8")).hexdigest()


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestReadOnlyWithholdsTheGenerator:
    def test_read_only_publishes_only_the_component_search(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        assert enabled_generative_ui_tool_names() == ("search_prefab_components",)

    def test_writable_mode_publishes_both(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
        monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
        assert enabled_generative_ui_tool_names() == ("generate_prefab_ui", "search_prefab_components")

    def test_the_feature_gate_still_decides_first(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_GENERATIVE_UI", raising=False)
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        assert enabled_generative_ui_tool_names() == ()

    @pytest.mark.asyncio
    async def test_a_read_only_server_never_lists_the_generator(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

        async with Client(build_mcp_server()) as client:
            names = [tool.name for tool in await client.list_tools()]
            refused = await client.call_tool("generate_prefab_ui", {"code": CODE}, raise_on_error=False)

        assert "search_prefab_components" in names
        assert "generate_prefab_ui" not in names
        assert refused.is_error

    def test_read_only_registers_the_provider_without_the_generate_tool(self, monkeypatch):
        """The provider the catalog gate leaves registered carries no code execution."""
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        server = SimpleNamespace(add_provider=lambda provider: setattr(server, "provider", provider))
        register_generative_ui_provider(server)  # type: ignore[arg-type]
        assert [tool.name for tool in server.provider._tools] == ["search_prefab_components"]

    def test_a_writable_server_registers_both_tools(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
        monkeypatch.delenv("NEXTDNS_READ_ONLY", raising=False)
        server = SimpleNamespace(add_provider=lambda provider: setattr(server, "provider", provider))
        register_generative_ui_provider(server)  # type: ignore[arg-type]
        assert [tool.name for tool in server.provider._tools] == [
            "generate_prefab_ui",
            "search_prefab_components",
        ]

    @pytest.mark.asyncio
    async def test_a_generator_built_anywhere_refuses_in_read_only_mode(self, monkeypatch, caplog):
        """Defense in depth: the tool body checks the ACL, not only the catalog."""
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        generate = PrefabGenerativeUI()._tools[0]

        with (
            caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative"),
            pytest.raises(ValueError, match="NEXTDNS_READ_ONLY"),
        ):
            await generate.fn(code=CODE)

        assert "read-only" in caplog.text
        assert CODE not in caplog.text


class TestExecutionAuditLogs:
    @pytest.mark.asyncio
    async def test_an_execution_logs_the_code_digest_and_no_code(self, caplog):
        from fastmcp.apps.config import AppConfig

        from nextdns_mcp.generative import _build_generate_tool

        async def fake_run(code, data):
            return SimpleNamespace(to_json=lambda: {"$prefab": {"version": "0.3"}, "view": {}})

        tool = _build_generate_tool("generate_prefab_ui", AppConfig(), fake_run)
        with caplog.at_level(logging.INFO, logger="nextdns_mcp.generative"):
            await tool.fn(code=CODE)

        assert CODE_SHA256 in caplog.text
        assert CODE not in caplog.text

    @pytest.mark.asyncio
    async def test_the_code_cap_rejection_is_logged(self, caplog):
        with (
            caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative"),
            pytest.raises(ValueError, match="code exceeds"),
        ):
            _validated_payload("x" * (MAX_CODE_BYTES + 1), None)
        assert "refused submitted code" in caplog.text

    @pytest.mark.asyncio
    async def test_both_data_caps_are_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative"):
            for data in ('{"k": "' + "y" * (MAX_DATA_BYTES + 1) + '"}', {"blob": "y" * (MAX_DATA_BYTES + 1)}):
                with pytest.raises(ValueError, match="data exceeds"):
                    _validated_payload(CODE, data)
        assert caplog.text.count("refused the data argument") == 2

    @pytest.mark.asyncio
    async def test_an_execution_timeout_is_logged(self, monkeypatch, caplog):
        import asyncio

        import prefab_ui.generative as prefab_generative

        async def slow(code, data=None, sandbox=None):
            await asyncio.sleep(5)

        monkeypatch.setattr(prefab_generative, "execute", slow)
        monkeypatch.setattr("nextdns_mcp.generative.EXECUTION_TIMEOUT", 0.01)
        with (
            caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative"),
            pytest.raises(TimeoutError, match="execution exceeded"),
        ):
            await _execute_within_budget(CODE, None, DenoPyodideSandbox())
        assert "execution exceeded" in caplog.text

    def test_a_wire_cap_rejection_is_logged(self, caplog):
        overflowed = b"z" * 400
        stream = SimpleNamespace(
            read1=lambda size: overflowed[:size], read=lambda size: b"", closed=False, fileno=lambda: -1
        )
        with (
            caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative_sandbox"),
            pytest.raises(ValueError, match="result exceeds"),
        ):
            _BoundedStdout(stream, 100, lambda: None).readline()
        assert "wire cap" in caplog.text

    def test_a_worker_that_refuses_to_exit_is_logged_as_killed(self, caplog):
        import subprocess

        class Stubborn:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                pass

            def communicate(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(cmd="deno", timeout=5)
                return b"", b""

            def kill(self):
                self.killed = True

            stdin = None
            stdout = None
            stderr = None

        process = Stubborn()
        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.generative_sandbox"):
            _reap_process(process)  # type: ignore[arg-type]
        assert process.killed
        assert "was killed" in caplog.text

    @pytest.mark.asyncio
    async def test_the_sandbox_logs_the_request_it_runs(self, monkeypatch, caplog):
        worker = DenoPyodideSandbox()

        async def exchange(process, request_id, code, data):
            return f'{{"id": "{request_id}", "result": {{}}}}'.encode()

        async def started():
            return None

        monkeypatch.setattr(worker, "_exchange", exchange)
        monkeypatch.setattr(worker, "_ensure_worker", started)

        with caplog.at_level(logging.INFO, logger="nextdns_mcp.generative_sandbox"):
            assert await worker._execute(CODE, None) == {}
        assert "running request" in caplog.text
        assert CODE not in caplog.text


class TestCallLogRecordsTheGenerator:
    def _middleware_row(self, tmp_path, tool, args, annotations, result=None):
        from fastmcp.tools import Tool
        from fastmcp.tools.base import ToolAnnotations

        def probe(code: str) -> str:
            return code

        registered = Tool.from_function(probe, name=tool, annotations=ToolAnnotations(**annotations))

        class _FastMCP:
            async def get_tool(self, name):
                return registered

        context = SimpleNamespace(
            message=SimpleNamespace(name=tool, arguments=args),
            fastmcp_context=SimpleNamespace(fastmcp=_FastMCP()),
        )
        path = tmp_path / "calls.jsonl"
        middleware = CallLogMiddleware(path=str(path), endpoints={})

        async def call_next(_context):
            return result or SimpleNamespace(is_error=False, structured_content={"ok": True})

        return middleware, call_next, context, path

    @pytest.mark.asyncio
    async def test_code_execution_records_as_a_write(self, tmp_path):
        middleware, call_next, context, path = TestCallLogRecordsTheGenerator._middleware_row(
            self, tmp_path, "generate_prefab_ui", {"code": CODE}, {"read_only_hint": False}
        )
        await middleware.on_call_tool(context, call_next)
        row = _rows(path)[0]
        assert row["write"] is True
        assert "endpoint" not in row
        assert row["code_sha256"] == CODE_SHA256

    @pytest.mark.asyncio
    async def test_a_read_annotated_local_tool_stays_a_read(self, tmp_path):
        middleware, call_next, context, path = self._middleware_row(
            tmp_path, "plotAnalytics", {"metric": "status"}, {"read_only_hint": True}
        )
        await middleware.on_call_tool(context, call_next)
        assert _rows(path)[0]["write"] is False

    @pytest.mark.asyncio
    async def test_no_context_leaves_the_old_reading(self, tmp_path):
        middleware, call_next, context, path = TestCallLogRecordsTheGenerator._middleware_row(
            self, tmp_path, "generate_prefab_ui", {"code": CODE}, {"read_only_hint": False}
        )
        context.fastmcp_context = None
        await middleware.on_call_tool(context, call_next)
        assert _rows(path)[0]["write"] is False

    @pytest.mark.asyncio
    async def test_a_failing_catalog_read_never_fails_the_call(self, tmp_path, caplog):
        middleware, call_next, context, path = TestCallLogRecordsTheGenerator._middleware_row(
            self, tmp_path, "generate_prefab_ui", {"code": CODE}, {"read_only_hint": False}
        )

        async def broken(name):
            raise RuntimeError("catalog unavailable")

        context.fastmcp_context.fastmcp.get_tool = broken
        with caplog.at_level(logging.DEBUG, logger="nextdns_mcp.call_log"):
            await middleware.on_call_tool(context, call_next)
        assert "could not read the tool catalog" in caplog.text
        assert _rows(path)[0]["write"] is False


class TestArgumentColumnStaysParseable:
    def test_the_digest_of_submitted_code(self):
        assert _code_sha256({"code": CODE}) == CODE_SHA256

    def test_no_code_means_no_digest(self):
        assert _code_sha256({"code": 7, "profile_id": "abc"}) == ""

    def test_a_long_code_argument_still_parses(self):
        arguments = {"code": "a" * 400, "note": "b" * 400, "filter": "c" * 400, "extra": "d" * 400}
        digest = _code_sha256(arguments)
        row_args = _args_text(arguments, digest)
        assert len(row_args) <= ARGS_CHARS
        parsed = json.loads(row_args)
        assert parsed["truncated"] is True
        assert digest in parsed["head"]  # the digest leads, so the cut keeps it

    def test_the_digest_leads_so_a_cut_keeps_it(self):
        long_code = "y = 'b'\n" * 400
        digest = _code_sha256({"code": long_code})
        parsed = json.loads(_args_text({"code": long_code}, digest))
        assert parsed["code_sha256"] == digest

    def test_arguments_under_the_cap_are_recorded_whole(self):
        assert json.loads(_args_text({"profile_id": "abc"}, "")) == {"profile_id": "abc"}


class TestUiActionEvidence:
    class _R:
        def __init__(self, structured):
            self.structured_content = structured

    def test_a_generated_view_that_acts_records_the_action(self):
        document = {
            "$prefab": {"version": "0.3"},
            "onMount": {"action": "callTool", "tool": "getDenylist"},
            "view": {
                "type": "Div",
                "onMount": None,
                "children": [
                    {"type": "Button", "label": "Block", "onClick": {"action": "callTool"}},
                    {"type": "Form", "actions": [{"action": "submit"}, {"action": "showToast", "m": "x"}]},
                ],
            },
        }
        evidence = _result_evidence(self._R(document), write=True)
        assert evidence["components"] == ["Button", "Div", "Form"]
        assert evidence["actions"] == ["callTool", "showToast", "submit"]

    def test_a_view_with_no_actions_records_none(self):
        document = {"$prefab": {"version": "0.3"}, "view": {"type": "Div", "children": [{"type": "Label"}]}}
        assert "actions" not in _result_evidence(self._R(document), write=False)

    def test_data_rows_are_not_mistaken_for_ui_actions(self):
        document = {"$prefab": {"version": "0.3"}, "view": {"type": "Div", "data": [{"action": "blocklist"}]}}
        assert "actions" not in _result_evidence(self._R(document), write=False)

    def test_a_non_string_key_never_reads_as_a_handler(self):
        document = {"$prefab": {"version": "0.3"}, "view": {7: "onClick", 1: {"action": "nope"}}}
        assert "actions" not in _result_evidence(self._R(document), write=False)


class TestReviewFindings:
    def test_non_handler_on_words_are_not_actions(self):
        document = {
            "$prefab": {"version": "0.3"},
            "view": {"type": "Div", "once": {"action": "not-an-action"}, "onClick": {"action": "showToast"}},
        }
        assert _result_evidence(TestUiActionEvidence._R(document), write=False)["actions"] == ["showToast"]

    def test_a_caller_cannot_override_the_code_digest_in_args(self):
        digest = _code_sha256({"code": CODE})
        parsed = json.loads(_args_text({"code": CODE, "code_sha256": "forged"}, digest))
        assert parsed["code_sha256"] == digest
        assert "forged" not in json.dumps(parsed)

    @pytest.mark.asyncio
    async def test_a_known_write_tool_skips_a_second_catalog_lookup(self, tmp_path):
        middleware, call_next, context, _path = TestCallLogRecordsTheGenerator._middleware_row(
            self, tmp_path, "generate_prefab_ui", {"code": CODE}, {"read_only_hint": False}
        )
        calls = []
        registered = context.fastmcp_context.fastmcp.get_tool

        async def counted(name):
            calls.append(name)
            return await registered(name)

        context.fastmcp_context.fastmcp.get_tool = counted
        await middleware.on_call_tool(context, call_next)
        context.message = SimpleNamespace(name="generate_prefab_ui", arguments={"code": CODE})
        await middleware.on_call_tool(context, call_next)
        assert calls == ["generate_prefab_ui"]
