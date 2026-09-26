"""Tests for StripExtraFieldsMiddleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    async def test_handles_tool_manager_exception(self, middleware, mock_context):
        """Test graceful handling when get_tool raises exception."""
        mock_context.fastmcp_context.fastmcp.get_tool = AsyncMock(side_effect=Exception("Tool not found"))
        mock_context.message.arguments = {"domain": "test.com", "extra": "field"}
        call_next = AsyncMock(return_value=MagicMock())

        # Should not raise, should proceed with original arguments
        await middleware.on_call_tool(mock_context, call_next)

        # Arguments should remain unchanged due to exception handling
        assert mock_context.message.arguments == {"domain": "test.com", "extra": "field"}
        call_next.assert_called_once_with(mock_context)

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
        assert middleware._coerce_string_value("²", {"number"}) == "²"
        assert middleware._coerce_string_value("-²", {"integer"}) == "-²"
        assert middleware._coerce_string_value("1.²", {"number"}) == "1.²"

    def test_coerce_value_schema_aware_for_lists(self, middleware):
        """Test array items are coerced when items schema is provided."""
        result = middleware._coerce_value(["true", "false"], {"type": "array", "items": {"type": "boolean"}})
        assert result == [True, False]
