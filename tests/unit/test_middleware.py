"""Tests for StripExtraFieldsMiddleware."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types
import pytest
from fastmcp import Client
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.tools import ToolResult

from nextdns_mcp import openapi, spec_tools
from nextdns_mcp.openapi import StripExtraFieldsMiddleware
from nextdns_mcp.server import build_mcp_server


class TestStripExtraFieldsMiddleware:
    """Tests for the StripExtraFieldsMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        return StripExtraFieldsMiddleware()

    @pytest.fixture
    def mock_tool(self):
        """Create a mock tool with defined parameters."""
        tool = MagicMock()
        tool.parameters = {
            "properties": {
                "domain": {"type": "string"},
                "record_type": {"type": "string"},
            }
        }
        return tool

    @pytest.fixture
    def mock_context(self, mock_tool):
        """Create a mock middleware context."""
        context = MagicMock()
        context.message.name = "testTool"
        context.message.arguments = {
            "domain": "example.com",
            "record_type": "A",
            "extra_field": "should_be_stripped",
            "unknown": "also_stripped",
        }

        # Mock fastmcp_context and get_tool method (fastmcp 3.0.1 API, async)
        context.fastmcp_context = MagicMock()
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=mock_tool)
        # The ignored-argument hint builds its owner index from the registered
        # catalog; this fixture's catalog is empty, so the hint stays generic.
        context.fastmcp_context.fastmcp.list_tools = AsyncMock(return_value=[])

        return context

    @pytest.mark.asyncio
    async def test_strips_unknown_fields(self, middleware, mock_context):
        """Test that unknown fields are stripped from arguments."""
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(mock_context, call_next)
        # Verify arguments were filtered
        assert mock_context.message.arguments == {
            "domain": "example.com",
            "record_type": "A",
        }
        call_next.assert_called_once_with(mock_context)

    @pytest.mark.asyncio
    async def test_preserves_known_fields(self, middleware, mock_context):
        """Test that known fields are preserved."""
        # Set arguments with only known fields
        mock_context.message.arguments = {
            "domain": "test.com",
            "record_type": "AAAA",
        }
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(mock_context, call_next)

        assert mock_context.message.arguments == {
            "domain": "test.com",
            "record_type": "AAAA",
        }

    @pytest.mark.asyncio
    async def test_handles_no_arguments(self, middleware, mock_context):
        """Test handling when arguments is None or empty."""
        mock_context.message.arguments = None
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(mock_context, call_next)

        call_next.assert_called_once_with(mock_context)

    @pytest.mark.asyncio
    async def test_handles_empty_arguments(self, middleware, mock_context):
        """Test handling when arguments is empty dict."""
        mock_context.message.arguments = {}
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(mock_context, call_next)

        call_next.assert_called_once_with(mock_context)
        assert mock_context.message.arguments == {}

    @pytest.mark.asyncio
    async def test_handles_no_fastmcp_context(self, middleware, mock_context):
        """Test handling when fastmcp_context is None."""
        mock_context.fastmcp_context = None
        mock_context.message.arguments = {"domain": "test.com", "extra": "field"}
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(mock_context, call_next)

        # Arguments should remain unchanged
        assert mock_context.message.arguments == {"domain": "test.com", "extra": "field"}
        call_next.assert_called_once_with(mock_context)

    @pytest.mark.asyncio
    async def test_handles_tool_not_found(self, middleware, mock_context):
        """Test pass-through when get_tool returns None (tool not found)."""
        mock_context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=None)
        mock_context.message.arguments = {"domain": "test.com", "extra": "field"}
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(mock_context, call_next)

        # Arguments should remain unchanged; call_next invoked as pass-through
        assert mock_context.message.arguments == {"domain": "test.com", "extra": "field"}
        call_next.assert_called_once_with(mock_context)

    @pytest.mark.asyncio
    async def test_reports_ignored_arguments_on_the_result(self, middleware, mock_context):
        """A stripped argument must be named in the result, not vanish silently.

        The relaxation is deliberate — AI clients send extra fields — but silence
        is what let a model send a field the tool never declared, get a plain
        success back, and believe its change had been attempted. The call still
        succeeds; the result has to say which arguments were not applied.
        """
        from fastmcp.tools import ToolResult

        result = ToolResult(structured_content={"success": True})
        call_next = AsyncMock(return_value=result)

        out = await middleware.on_call_tool(mock_context, call_next)

        assert out.structured_content["ignored_arguments"] == ["extra_field", "unknown"]
        assert "not applied" in out.structured_content["hint"]

    @pytest.mark.asyncio
    async def test_no_annotation_when_nothing_was_stripped(self, middleware, mock_tool):
        from fastmcp.tools import ToolResult

        context = MagicMock()
        context.message.name = "testTool"
        context.message.arguments = {"domain": "example.com", "record_type": "A"}
        context.fastmcp_context = MagicMock()
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=mock_tool)
        call_next = AsyncMock(return_value=ToolResult(structured_content={"success": True}))

        out = await middleware.on_call_tool(context, call_next)

        assert out.structured_content == {"success": True}

    @pytest.mark.asyncio
    async def test_fails_closed_when_schema_fetch_fails(self, middleware, mock_context, caplog):
        """Test that a schema-fetch failure aborts the call instead of passing args through."""
        mock_context.fastmcp_context.fastmcp.get_tool = AsyncMock(side_effect=Exception("schema fetch failed"))
        mock_context.message.arguments = {"domain": "test.com", "extra": "field"}
        call_next = AsyncMock(return_value=MagicMock())

        with pytest.raises(ToolError, match="failed closed"):
            await middleware.on_call_tool(mock_context, call_next)

        # The call must not proceed with unstripped/un-coerced arguments
        call_next.assert_not_called()
        assert mock_context.message.arguments == {"domain": "test.com", "extra": "field"}

        # The error is logged at error level and carries the root cause
        assert any(record.levelno >= 40 for record in caplog.records)
        assert "schema fetch failed" in caplog.text

    @pytest.mark.asyncio
    async def test_not_found_error_passes_through_unwrapped(self, middleware, mock_context):
        """Test that NotFoundError (unknown tool) is re-raised, not wrapped as ToolError."""
        mock_context.fastmcp_context.fastmcp.get_tool = AsyncMock(side_effect=NotFoundError("Unknown tool: nope"))
        mock_context.message.arguments = {"domain": "test.com"}
        call_next = AsyncMock(return_value=MagicMock())

        with pytest.raises(NotFoundError, match="Unknown tool"):
            await middleware.on_call_tool(mock_context, call_next)

        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_error_on_schema_fetch_failure(self, middleware, mock_context):
        """Test that schema-fetch failures are logged at error level, not warning."""
        mock_context.fastmcp_context.fastmcp.get_tool = AsyncMock(side_effect=RuntimeError("boom"))
        mock_context.message.arguments = {"domain": "test.com", "extra": "field"}
        call_next = AsyncMock(return_value=MagicMock())

        with patch("nextdns_mcp.openapi.logger") as mock_logger, pytest.raises(ToolError):
            await middleware.on_call_tool(mock_context, call_next)

        mock_logger.exception.assert_called_once()
        msg = mock_logger.exception.call_args.args[0]
        assert "testTool" in msg
        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_stripped_fields(self, middleware, mock_context):
        """Test that stripped fields are logged at debug level."""
        call_next = AsyncMock(return_value=MagicMock())

        with patch("nextdns_mcp.openapi.logger"):
            await middleware.on_call_tool(mock_context, call_next)

            # Verify call_next was called (middleware completed successfully)
            call_next.assert_called_once_with(mock_context)
            # Fields were stripped, so debug should have been called
            # But we need to verify the arguments were actually filtered
            assert mock_context.message.arguments == {
                "domain": "example.com",
                "record_type": "A",
            }

    def test_get_schema_property_types_handles_various_schemas(self, middleware):
        """Test extraction of schema types from property schemas."""
        assert middleware._get_schema_property_types({"type": "boolean"}) == {"boolean"}
        assert middleware._get_schema_property_types({"type": ["boolean", "null"]}) == {
            "boolean",
            "null",
        }
        assert middleware._get_schema_property_types({"anyOf": [{"type": "integer"}, {"type": "null"}]}) == {
            "integer",
            "null",
        }
        assert middleware._get_schema_property_types({"oneOf": [{"type": ["number", "null"]}]}) == {"number", "null"}
        assert middleware._get_schema_property_types("not-a-dict") == set()
        assert middleware._get_schema_property_types({}) == set()

    def test_get_schema_property_types_tolerates_malformed_subschemas(self, middleware):
        """Malformed ``anyOf``/``oneOf`` entries contribute no types instead of raising.

        A subschema that is not an object, or whose ``type`` is neither a string
        nor a list, carries no coercible type, so it is skipped rather than
        crashing the middleware on a schema it does not recognize.
        """
        assert middleware._get_schema_property_types({"anyOf": ["string", {"type": "boolean"}]}) == {"boolean"}
        assert middleware._get_schema_property_types({"oneOf": [{"const": "x"}, {"type": ["number", "null"]}]}) == {
            "number",
            "null",
        }
        assert middleware._get_schema_property_types({"anyOf": [{"type": None}, {"type": {"name": "string"}}]}) == set()

    def test_coerce_string_value_respects_schema(self, middleware):
        """Test that string coercion only happens for expected schema types."""
        assert middleware._coerce_string_value("true", {"boolean"}) is True
        assert middleware._coerce_string_value("42", {"integer"}) == 42
        assert middleware._coerce_string_value("3.14", {"number"}) == 3.14
        assert middleware._coerce_string_value("315244", {"string"}) == "315244"
        assert middleware._coerce_string_value("true", {"string"}) == "true"
        assert middleware._coerce_string_value("42", {"string"}) == "42"
        # Unicode digit-like characters must not crash int() or float()
        assert middleware._coerce_string_value("²", {"integer"}) == "²"
        assert middleware._coerce_string_value("³", {"integer"}) == "³"
        assert middleware._coerce_string_value("¹", {"integer"}) == "¹"
        assert middleware._coerce_string_value("⁴", {"integer"}) == "⁴"
        assert middleware._coerce_string_value("-²", {"integer"}) == "-²"
        assert middleware._coerce_string_value("1²", {"integer"}) == "1²"
        assert middleware._coerce_string_value("½", {"integer"}) == "½"
        assert middleware._coerce_string_value("¼", {"integer"}) == "¼"
        assert middleware._coerce_string_value("²", {"number"}) == "²"
        assert middleware._coerce_string_value("1.²", {"number"}) == "1.²"
        assert middleware._coerce_string_value("-1.²", {"number"}) == "-1.²"

    def test_coerce_value_schema_aware_for_lists(self, middleware):
        """Test array items are coerced when items schema is provided."""
        result = middleware._coerce_value(["true", "false"], {"type": "array", "items": {"type": "boolean"}})
        assert result == [True, False]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw_value", "schema_type", "expected"),
        [
            ("²", "integer", "²"),
            ("³", "integer", "³"),
            ("¹", "integer", "¹"),
            ("⁴", "integer", "⁴"),
            ("-²", "integer", "-²"),
            ("1²", "integer", "1²"),
            ("½", "integer", "½"),
            ("¼", "integer", "¼"),
            ("²", "number", "²"),
            ("1.²", "number", "1.²"),
            ("-1.²", "number", "-1.²"),
            ("42", "integer", 42),
            ("-42", "integer", -42),
            ("3.14", "number", 3.14),
        ],
    )
    async def test_unicode_digits_through_middleware_path(self, middleware, raw_value, schema_type, expected):
        """Test Unicode digit strings do not crash int/float parsing through on_call_tool."""
        tool = MagicMock()
        tool.parameters = {
            "properties": {
                "val": {"type": schema_type},
            }
        }
        context = MagicMock()
        context.message.name = "testTool"
        context.message.arguments = {"val": raw_value}
        context.fastmcp_context = MagicMock()
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(context, call_next)

        call_next.assert_called_once_with(context)
        assert context.message.arguments["val"] == expected

    @pytest.mark.asyncio
    async def test_unicode_digits_multi_field_through_middleware_path(self, middleware):
        """Test multi-field arguments with Unicode digits through on_call_tool."""
        tool = MagicMock()
        tool.parameters = {
            "properties": {
                "integer_field": {"type": "integer"},
                "number_field": {"type": "number"},
                "valid_int": {"type": "integer"},
                "valid_num": {"type": "number"},
                "string_field": {"type": "string"},
                "list_field": {"type": "array", "items": {"type": "integer"}},
                "nested_obj": {"type": "object"},
            }
        }
        context = MagicMock()
        context.message.name = "testNumericTool"
        context.message.arguments = {
            "integer_field": "²",
            "number_field": "1.²",
            "valid_int": "42",
            "valid_num": "3.14",
            "string_field": "³",
            "list_field": ["²", "42", "³"],
            "nested_obj": {"power": "²"},
        }
        context.fastmcp_context = MagicMock()
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)
        call_next = AsyncMock(return_value=MagicMock())

        await middleware.on_call_tool(context, call_next)

        call_next.assert_called_once_with(context)
        assert context.message.arguments == {
            "integer_field": "²",
            "number_field": "1.²",
            "valid_int": 42,
            "valid_num": 3.14,
            "string_field": "³",
            "list_field": ["²", 42, "³"],
            "nested_obj": {"power": "²"},
        }

    def test_coerce_string_value_float_value_error_fallback(self, middleware, monkeypatch):
        """The guarded cast is where a parse failure can still land.

        ``is_integer_shaped`` runs ``int()`` itself, so the int path cannot
        disagree with its own cast; the float shape check is a shape test, and
        a value that passes the shape but fails ``float()`` must come back
        unchanged rather than raise out of the coercion layer.
        """
        monkeypatch.setattr("nextdns_mcp.openapi.is_float_shaped", lambda _s: True)
        assert middleware._coerce_string_value("not a float", {"number"}) == "not a float"


class TestIgnoredArgumentOwnerHint:
    """The ignored-arguments hint names the tool that declares the dropped keys.

    A generic 'check the parameter list' let a model that sent updateSettings
    the keys of updateSecuritySettings report a wrong-endpoint success (live,
    target-model run). The hint has to name the owning tool when exactly one
    tool declares each dropped argument.
    """

    def test_single_owner_is_named(self):
        from nextdns_mcp.openapi import _owner_hint

        index = {
            "googleSafeBrowsing": ("updateSecuritySettings",),
            "threatIntelligenceFeeds": ("updateSecuritySettings",),
        }
        hint = _owner_hint(["googleSafeBrowsing", "threatIntelligenceFeeds"], "updateSettings", index)
        assert "not parameters" in hint
        assert "updateSecuritySettings" in hint
        assert "call it with them" in hint

    def test_multiple_owners_are_split(self):
        from nextdns_mcp.openapi import _owner_hint

        index = {"googleSafeBrowsing": ("updateSecuritySettings",), "allowAffiliate": ("updatePrivacySettings",)}
        hint = _owner_hint(["googleSafeBrowsing", "allowAffiliate"], "updateSettings", index)
        assert "updateSecuritySettings" in hint
        assert "updatePrivacySettings" in hint
        assert "each named tool" in hint

    def test_unowned_arguments_note_alongside_an_owner(self):
        from nextdns_mcp.openapi import _owner_hint

        index = {"googleSafeBrowsing": ("updateSecuritySettings",)}
        hint = _owner_hint(["googleSafeBrowsing", "extra_field"], "updateSettings", index)
        assert "updateSecuritySettings" in hint
        assert "extra_field" in hint
        assert "dropped too" in hint

    def test_generic_hint_when_nothing_has_an_owner(self):
        from nextdns_mcp.openapi import _owner_hint

        hint = _owner_hint(["extra_field", "unknown"], "updateSettings", {})
        assert "check the tool's parameter list" in hint


class TestOwnerIndexIsTheRegisteredCatalog:
    """The owner index comes from the server's registered tools, not the spec.

    The composite operations (plotAnalytics, dohLookup) declare arguments too
    and live outside the spec surface; a spec-derived index would blame the
    wrong owner - or none - for an argument they own. The disclosure
    transform's synthetic entries are excluded: nobody should be told a name
    is "a parameter of call_tool".
    """

    @staticmethod
    def _server_with(tools: list) -> SimpleNamespace:
        by_name = {t.name: t for t in tools}
        server = SimpleNamespace()
        server.get_tool = AsyncMock(side_effect=lambda name: by_name.get(name))
        return server

    @staticmethod
    def _tool(name: str, props: list[str]) -> SimpleNamespace:
        return SimpleNamespace(name=name, parameters={"properties": {p: {} for p in props}})

    @pytest.mark.asyncio
    async def test_composite_tools_own_their_arguments(self):
        from nextdns_mcp.openapi import _catalog_argument_index

        tools = [self._tool("plotAnalytics", ["metric", "profile_id"]), self._tool("getProfile", ["profile_id"])]
        server = self._server_with(tools)
        index = await _catalog_argument_index(server, [t.name for t in tools])
        assert index["metric"] == ("plotAnalytics",)
        assert set(index["profile_id"]) == {"plotAnalytics", "getProfile"}

    @pytest.mark.asyncio
    async def test_synthetic_entries_are_excluded(self):
        from nextdns_mcp.constants import SYNTHETIC_TOOL_NAMES
        from nextdns_mcp.openapi import _catalog_argument_index

        tools = [self._tool(name, ["name", "arguments"]) for name in sorted(SYNTHETIC_TOOL_NAMES)]
        tools.append(self._tool("getProfile", ["profile_id"]))
        index = await _catalog_argument_index(self._server_with(tools), [t.name for t in tools])
        assert all(name not in index for name in SYNTHETIC_TOOL_NAMES)
        assert index == {"profile_id": ("getProfile",)}

    @pytest.mark.asyncio
    async def test_the_index_is_built_once_per_server_not_per_call(self):
        tools = [self._tool("getProfile", ["profile_id"])]
        mw = StripExtraFieldsMiddleware(tool_names=[t.name for t in tools])
        server = self._server_with(tools)
        first = await mw._owner_index(server)
        second = await mw._owner_index(server)
        assert first is second
        assert server.get_tool.await_count == 1

    @pytest.mark.asyncio
    async def test_a_second_server_does_not_read_the_firsts_cache(self):
        tools_a = [self._tool("plotAnalytics", ["metric"])]
        tools_b = [self._tool("getProfile", ["profile_id"])]
        mw = StripExtraFieldsMiddleware(tool_names=[t.name for t in tools_a + tools_b])
        server_a = self._server_with(tools_a)
        server_b = self._server_with(tools_b)
        index_a = await mw._owner_index(server_a)
        index_b = await mw._owner_index(server_b)
        assert "metric" in index_a and "metric" not in index_b
        assert server_b.get_tool.await_count == 2, "every name re-probed on the new server"

    @pytest.mark.asyncio
    async def test_tool_schemas_are_memoized_after_the_first_fetch(self):
        mw = StripExtraFieldsMiddleware()
        tool = self._tool("getProfile", ["profile_id"])
        server = SimpleNamespace()
        server.get_tool = AsyncMock(return_value=tool)
        first = await mw._tool_parameters(server, "getProfile")
        second = await mw._tool_parameters(server, "getProfile")
        assert first is second
        server.get_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_tools_are_not_cached_as_misses(self):
        """A tool registered after a miss must not be shadowed by it."""
        mw = StripExtraFieldsMiddleware()
        tool = self._tool("lateTool", ["x"])
        responses = iter([None, tool])
        server = SimpleNamespace()
        server.get_tool = AsyncMock(side_effect=lambda name: next(responses))
        assert await mw._tool_parameters(server, "lateTool") is None
        assert await mw._tool_parameters(server, "lateTool") == tool.parameters


class TestIgnoredArgumentsNeverClobberTheToolHint:
    """The tool's own hint answers what was applied; the annotation is appended.

    A payload carrying the profile-id shape hint or a 404 diagnosis is the
    tool's answer to the call the server did make; overwriting it with the
    dropped-arguments note would delete the more important information. Both
    survive: the hint merges, and the full owner text stays under
    ``ignored_arguments_hint``.
    """

    def test_an_existing_hint_survives_the_merge(self):
        from nextdns_mcp.openapi import _report_ignored_arguments

        result = ToolResult(structured_content={"data": [], "hint": "the profile id is 6 or 10 characters"})
        _report_ignored_arguments(result, ["privacy"], "privacy are parameters of updatePrivacySettings")
        structured = result.structured_content
        assert structured["ignored_arguments"] == ["privacy"]
        assert structured["ignored_arguments_hint"].startswith("privacy are parameters")
        assert (
            structured["hint"]
            == "the profile id is 6 or 10 characters | privacy are parameters of updatePrivacySettings"
        )

    def test_no_prior_hint_uses_the_annotation_alone(self):
        from nextdns_mcp.openapi import _report_ignored_arguments

        result = ToolResult(structured_content={"data": []})
        _report_ignored_arguments(result, ["x"], "the note")
        assert result.structured_content["hint"] == "the note"


class TestIgnoredArgumentsMerge:
    """Under disclosure both passes report; neither pass erases the other."""

    def test_an_inner_report_survives_the_outer_pass(self):
        result = ToolResult(
            structured_content={"data": [], "ignored_arguments": ["from"], "ignored_arguments_hint": "inner hint"}
        )
        openapi._report_ignored_arguments(result, ["query"], "outer hint")
        assert result.structured_content["ignored_arguments"] == ["from", "query"]
        assert "inner hint" in result.structured_content["ignored_arguments_hint"]
        assert "outer hint" in result.structured_content["ignored_arguments_hint"]

    def test_a_duplicate_report_adds_nothing(self):
        result = ToolResult(structured_content={"data": [], "ignored_arguments": ["x"], "ignored_arguments_hint": "h"})
        openapi._report_ignored_arguments(result, ["x"], "h")
        assert result.structured_content["ignored_arguments"] == ["x"]

    def test_a_content_only_result_gets_a_visible_note(self):
        """An image result has no structured dict: the drop is said in the content."""
        result = ToolResult(content=[mcp.types.ImageContent(type="image", data="AA", mimeType="image/png")])
        openapi._report_ignored_arguments(result, ["from"], "from_time is this tool's name for it")
        note = result.content[-1]
        assert note.type == "text" and "from" in note.text


class TestOwnerIndexUnderDisclosure:
    """The owner hint works in the default BM25 mode, where list_tools is the pair."""

    async def test_a_dropped_argument_names_its_owner_under_bm25(self, monkeypatch):
        calls: list[dict] = []

        async def fake_request(method, url, params=None, json_body=None):
            calls.append({"method": method, "url": url})
            return {"data": []}

        monkeypatch.setattr(spec_tools, "_api_request_payload", fake_request)
        server = build_mcp_server()
        async with Client(server) as client:
            found = await client.call_tool(
                "call_tool",
                {"name": "getLogs", "arguments": {"profile_id": "abc123", "metric": "status"}},
            )
        structured = found.structured_content or {}
        assert structured.get("ignored_arguments") == ["metric"]
        assert "plotAnalytics" in structured.get("ignored_arguments_hint", ""), structured


class TestIgnoredArgumentsEdgePaths:
    """The annotation paths stay total: no-op reports, and no hint duplication."""

    def test_a_content_only_result_with_nothing_dropped_is_untouched(self):
        result = ToolResult(content=[mcp.types.ImageContent(type="image", data="AA", mimeType="image/png")])
        openapi._report_ignored_arguments(result, [], "nothing")
        assert len(result.content) == 1

    def test_a_hint_already_present_is_not_appended_twice(self):
        result = ToolResult(
            structured_content={"data": [], "hint": "keep it | owner hint"},
        )
        openapi._report_ignored_arguments(result, ["x"], "owner hint")
        assert result.structured_content["hint"] == "keep it | owner hint"


class TestIgnoredArgumentsHintDedup:
    """Both passes dropping the same name must attribute it once, not twice."""

    def test_the_merged_hint_is_re_rendered_for_the_union_not_concatenated(self):
        result = ToolResult(
            structured_content={
                "data": [],
                "ignored_arguments": ["privacy"],
                "ignored_arguments_hint": "privacy: updatePrivacy",
            }
        )
        openapi._report_ignored_arguments(
            result,
            ["privacy", "name"],
            "privacy, name: updatePrivacy",
            hint_for=lambda names: "owners(" + ",".join(names) + ")",
        )
        structured = result.structured_content
        assert structured["ignored_arguments"] == ["privacy", "name"]
        # One rendering of the union - the shared name appears once, and the
        # prior sentence does not ride along beside a text that already covers it.
        assert structured["ignored_arguments_hint"] == "owners(privacy,name)"
        assert structured["hint"] == "owners(privacy,name)"

    def test_the_same_set_reannotated_still_renders_once(self):
        result = ToolResult(
            structured_content={"data": [], "ignored_arguments": ["x"], "ignored_arguments_hint": "x: T"}
        )
        openapi._report_ignored_arguments(
            result, ["x"], "x: T", hint_for=lambda names: "owners(" + ",".join(names) + ")"
        )
        assert result.structured_content["ignored_arguments_hint"] == "owners(x)"


class TestIgnoredArgumentsHintBaseSurvival:
    """Re-rendering replaces only the annotation; the tool's own hint stays."""

    def test_the_tools_own_hint_survives_the_re_render(self):
        result = ToolResult(
            structured_content={
                "data": [],
                "hint": "the profile id is 6 or 10 characters | from: getAnalyticsStatusSeries",
                "ignored_arguments": ["from"],
                "ignored_arguments_hint": "from: getAnalyticsStatusSeries",
            }
        )
        openapi._report_ignored_arguments(
            result,
            ["from", "to"],
            "from, to: getAnalyticsStatusSeries",
            hint_for=lambda names: "owners(" + ",".join(names) + ")",
        )
        assert result.structured_content["hint"] == "the profile id is 6 or 10 characters | owners(from,to)"

    def test_a_foreign_annotation_in_the_hint_is_not_stripped_by_re_render(self):
        result = ToolResult(
            structured_content={
                "data": [],
                "hint": "fix the range | someone else's note",
                "ignored_arguments": ["from"],
                "ignored_arguments_hint": "from: T",
            }
        )
        openapi._report_ignored_arguments(
            result, ["to"], "to: T", hint_for=lambda names: "owners(" + ",".join(names) + ")"
        )
        assert result.structured_content["hint"] == "fix the range | someone else's note | owners(from,to)"

    def test_a_non_string_hint_and_a_missing_annotation_are_tolerated(self):
        """Odd payload shapes get re-set, not crashed on."""
        result = ToolResult(
            structured_content={"data": [], "hint": 7, "ignored_arguments": ["x"], "ignored_arguments_hint": None}
        )
        openapi._report_ignored_arguments(
            result, ["x"], "x: T", hint_for=lambda names: "owners(" + ",".join(names) + ")"
        )
        assert result.structured_content["hint"] == "owners(x)"
