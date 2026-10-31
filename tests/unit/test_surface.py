"""Unit tests for the tool-surface switch and the guidance that follows it.

Two surfaces (``grouped``, ``atomic``) can be published, and everything the server
says about its own catalog has to agree with the one it published: the
``initialize`` instructions, the hint in a 404 payload, and whether the usage guide
is offered at all. These tests pin the switch itself and those three pointers; the
surfaces' own behaviour is tested in ``test_spec_tools.py`` and the grouped suites.

SPDX-License-Identifier: MIT
"""

import pytest
from fastmcp import Client

from nextdns_mcp.surface import list_profiles_call, validate_tool_surface
from nextdns_mcp.utils import _note_404_on_a_profile_path


class TestSurfaceValue:
    def test_an_unset_or_blank_surface_is_grouped(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_TOOL_SURFACE", raising=False)
        assert validate_tool_surface() == "grouped"
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "   ")
        assert validate_tool_surface() == "grouped"

    def test_the_surface_name_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "ATOMIC")
        assert validate_tool_surface() == "atomic"

    def test_the_surface_is_validated_where_the_server_is_built(self, monkeypatch):
        """An unknown value fails startup rather than silently serving the default.

        ``NEXTDNS_TOOL_SEARCH`` rejects an unknown mode for the same reason: the
        tool contract a caller is handed is not something to guess at.
        """
        from nextdns_mcp.config import ConfigurationError
        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atmic")
        with pytest.raises(ConfigurationError, match="Invalid NEXTDNS_TOOL_SURFACE"):
            build_mcp_server()


class TestGuidanceFollowsTheSurface:
    """Error hints name a call the caller could actually make."""

    def test_the_profile_pointer_names_the_surface_own_tool(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        assert list_profiles_call() == "listProfiles()"
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "grouped")
        assert list_profiles_call() == 'manageProfiles(operation="list")'

    def test_an_unrecognised_surface_falls_back_to_grouped(self, monkeypatch):
        """The guidance path must never raise: it runs while answering a call."""
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "whichever")
        assert list_profiles_call() == 'manageProfiles(operation="list")'

    def test_the_404_hint_points_at_the_tool_that_exists(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        payload = _note_404_on_a_profile_path({"upstream_code": "notFound"}, "/profiles/abc123/denylist")
        assert "listProfiles() returns the profile ids" in payload["hint"]

    def test_the_grouped_hint_is_unchanged(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_TOOL_SURFACE", raising=False)
        payload = _note_404_on_a_profile_path({"upstream_code": "notFound"}, "/profiles/abc123/denylist")
        assert 'manageProfiles(operation="list") returns the profile ids' in payload["hint"]

    def test_the_instructions_describe_the_surface_that_is_published(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        from nextdns_mcp.server import build_mcp_server

        instructions = build_mcp_server().instructions
        assert "one tool per endpoint" in instructions
        # The grouped surface's contract: there is no operation to choose here.
        assert "an `operation` (get, list, add" not in instructions

    def test_the_grouped_instructions_are_unchanged(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        from nextdns_mcp.server import build_mcp_server

        assert "an `operation` (get, list, add" in build_mcp_server().instructions

    async def test_the_usage_guide_is_not_published_on_the_spec_surface(self, monkeypatch):
        """The guide walks the grouped tools; on this surface they do not exist."""
        monkeypatch.setenv("NEXTDNS_API_KEY", "k")
        monkeypatch.setenv("NEXTDNS_TOOL_SURFACE", "atomic")
        from nextdns_mcp.server import build_mcp_server

        async with Client(build_mcp_server()) as client:
            assert await client.list_prompts() == []
