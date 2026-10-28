"""The list tool accepts the synonyms real callers reach for.

Two live E2E runs showed where the schema was rejecting calls that named the
right thing under the wrong key: `list_type="blocklists"` (38 calls), `entry`
where `entries` is taken (7 calls), and `entry` where `entry_id` is taken. The
point is that the server works with the arguments clients actually send, so the
tolerance is narrow: a synonym, never a guess between two candidates.

SPDX-License-Identifier: MIT
"""

import pytest
from fastmcp import Client

from nextdns_mcp.server import build_mcp_server
from nextdns_mcp.tools.lists import _list_type_alias


class TestListTypeAlias:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("blocklists", "privacy_blocklists"),
            ("natives", "privacy_natives"),
            ("tlds", "security_tlds"),
            ("categories", "parental_categories"),
            ("services", "parental_services"),
            ("allowlist", "allowlist"),
            ("privacy-blocklists", "privacy_blocklists"),
        ],
    )
    def test_an_unambiguous_short_name_resolves(self, given, expected):
        assert _list_type_alias(given) == expected

    @pytest.mark.parametrize("given", ["privacy", "parental", "lists", "bogus", ""])
    def test_ambiguous_or_unknown_names_are_left_alone(self, given):
        """Left unchanged so the schema error can list the valid values."""
        assert _list_type_alias(given) == given

    def test_the_schema_still_advertises_the_canonical_names(self):
        """Tolerance must not narrow or extend what the schema promises."""
        import asyncio

        async def _schema():
            async with Client(build_mcp_server()) as client:
                tools = {t.name: t for t in await client.list_tools()}
            return tools["manageLists"].input_schema["properties"]["list_type"]["enum"]

        enum = asyncio.run(_schema())
        assert enum == [
            "allowlist",
            "denylist",
            "privacy_blocklists",
            "privacy_natives",
            "security_tlds",
            "parental_categories",
            "parental_services",
        ]


class TestSynonymArguments:
    """`entry` is the singular of `entries`, and of `entry_id` for these types."""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    async def _call(self, arguments: dict) -> str:
        async with Client(build_mcp_server()) as client:
            result = await client.call_tool("manageLists", arguments)
        payload = getattr(result, "structured_content", None) or {}
        return str(payload.get("error", ""))

    @pytest.mark.asyncio
    async def test_short_list_type_reaches_the_api_instead_of_the_schema(self):
        error = await self._call({"operation": "get", "list_type": "blocklists", "profile_id": "23228d"})
        assert "Input should be" not in error
        assert "privacy/blocklists" in error  # the upstream path it resolved to

    @pytest.mark.asyncio
    async def test_replace_accepts_a_single_entry_under_the_singular_key(self):
        error = await self._call(
            {"operation": "replace", "list_type": "security_tlds", "profile_id": "23228d", "entry": "zip"}
        )
        assert "entries is required" not in error
        assert "entries must be a JSON array" not in error

    @pytest.mark.asyncio
    async def test_replace_accepts_a_json_array_under_the_singular_key(self):
        error = await self._call(
            {"operation": "replace", "list_type": "privacy_natives", "profile_id": "23228d", "entry": '["alexa"]'}
        )
        assert "entries is required" not in error
        assert "entries must be a JSON array" not in error

    @pytest.mark.asyncio
    async def test_remove_accepts_the_value_under_the_singular_key(self):
        error = await self._call(
            {"operation": "remove", "list_type": "denylist", "profile_id": "23228d", "entry": "ads.example.com"}
        )
        assert "entry_id is required" not in error

    @pytest.mark.asyncio
    async def test_replace_still_rejects_when_nothing_names_the_entries(self):
        error = await self._call({"operation": "replace", "list_type": "denylist", "profile_id": "23228d"})
        assert "entries is required" in error
