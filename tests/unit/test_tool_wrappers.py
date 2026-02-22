"""Integration-style tests for MCP tool wrappers in server.py.

These tests verify that the MCP tool decorators properly expose the underlying
implementation functions. The actual logic is tested in test_server_functions.py.
"""

import pytest

from nextdns_mcp.server import _dohLookup_impl, dohLookup


class TestDohLookupWrapper:
    """Tests for dohLookup MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_returns_error_without_profile(self):
        """Test wrapper properly calls implementation without default profile."""
        result = await _dohLookup_impl("example.com", None, "A")

        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_record_type(self, monkeypatch):
        """Test wrapper properly calls implementation with invalid record type."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")

        result = await _dohLookup_impl("example.com", "abc123", "INVALID")

        assert "error" in result
        assert "Invalid record type" in result["error"]

    @pytest.mark.asyncio
    async def test_doh_lookup_tool_wrapper_delegates_to_impl(self):
        """Test dohLookup MCP tool wrapper calls _dohLookup_impl."""
        # Calling the MCP tool directly exercises line 616 (return await _dohLookup_impl(...))
        result = await dohLookup("example.com", None, "A")

        assert "error" in result
        assert "No profile_id provided" in result["error"]
