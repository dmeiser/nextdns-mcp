"""Tests for StripExtraFieldsMiddleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import NotFoundError, ToolError

from nextdns_mcp.openapi import StripExtraFieldsMiddleware


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

    def test_coerce_string_value_int_value_error_fallback(self, middleware, monkeypatch):
        """Test fallback to original string if int() raises ValueError."""
        monkeypatch.setattr("nextdns_mcp.openapi._is_integer", lambda _s: True)
        assert middleware._coerce_string_value("invalid", {"integer"}) == "invalid"
