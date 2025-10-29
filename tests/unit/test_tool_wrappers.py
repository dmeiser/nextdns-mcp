"""Integration-style tests for MCP tool wrappers in server.py.

These tests verify that the MCP tool decorators properly expose the underlying
implementation functions. The actual logic is tested in test_server_functions.py.
"""
import pytest

from nextdns_mcp.server import (
    dohLookup,
    updateAllowlist,
    updateDenylist,
    updateParentalControlCategories,
    updateParentalControlServices,
    updatePrivacyBlocklists,
    updatePrivacyNatives,
    updateSecurityTlds,
)


class TestDohLookupWrapper:
    """Tests for dohLookup MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_returns_error_without_profile(self):
        """Test wrapper properly calls implementation without default profile."""
        result = await dohLookup.fn("example.com", None, "A")
        
        # Should return error dict when no profile provided
        assert "error" in result
        assert "No profile_id provided" in result["error"]

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_record_type(self, monkeypatch):
        """Test wrapper properly calls implementation with invalid record type."""
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        
        result = await dohLookup.fn("example.com", "abc123", "INVALID")
        
        # Should return error dict for invalid record type
        assert "error" in result
        assert "Invalid record type" in result["error"]


class TestUpdateDenylistWrapper:
    """Tests for updateDenylist MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updateDenylist.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]

    @pytest.mark.asyncio
    async def test_wrapper_handles_non_array(self):
        """Test wrapper properly calls bulk helper with non-array data."""
        result = await updateDenylist.fn("abc123", '{"key": "value"}')
        
        assert "error" in result
        assert "must be a JSON array" in result["error"]


class TestUpdateAllowlistWrapper:
    """Tests for updateAllowlist MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updateAllowlist.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]


class TestUpdateParentalControlServicesWrapper:
    """Tests for updateParentalControlServices MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updateParentalControlServices.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]


class TestUpdateParentalControlCategoriesWrapper:
    """Tests for updateParentalControlCategories MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updateParentalControlCategories.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]


class TestUpdateSecurityTldsWrapper:
    """Tests for updateSecurityTlds MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updateSecurityTlds.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]


class TestUpdatePrivacyBlocklistsWrapper:
    """Tests for updatePrivacyBlocklists MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updatePrivacyBlocklists.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]


class TestUpdatePrivacyNativesWrapper:
    """Tests for updatePrivacyNatives MCP tool wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_handles_invalid_json(self):
        """Test wrapper properly calls bulk helper with invalid JSON."""
        result = await updatePrivacyNatives.fn("abc123", "not valid json")
        
        assert "error" in result
        assert "Invalid JSON" in result["error"]
