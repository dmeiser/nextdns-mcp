"""Operation synonyms resolve to the verb the tool actually means.

Live runs show callers naming the intent rather than the enum: manageLists asked
for `list` 7 times, manageProfiles for `remove`, `rename`, `set`, `replace` and
`add`. Each has exactly one meaning for the tool in question, so the server
resolves it instead of returning a schema rejection the caller has to decode.
The published enum is unchanged, and an unknown verb is still rejected.

SPDX-License-Identifier: MIT
"""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from nextdns_mcp.coercion import operation_aliases
from nextdns_mcp.server import build_mcp_server


class TestOperationAliasRule:
    def test_a_known_synonym_resolves(self):
        coerce = operation_aliases(("get", "remove"), {"list": "get", "delete": "remove"})
        assert coerce("list") == "get"
        assert coerce("delete") == "remove"

    def test_a_canonical_value_passes_through(self):
        coerce = operation_aliases(("get", "remove"), {"list": "get"})
        assert coerce("get") == "get"

    def test_an_unknown_verb_is_left_for_the_schema_error(self):
        """The enum error lists the valid operations; guessing would not."""
        coerce = operation_aliases(("get", "remove"), {"list": "get"})
        assert coerce("frobnicate") == "frobnicate"

    @pytest.mark.parametrize("given", ["list ", "LIST", "List"])
    def test_case_and_surrounding_space_are_normalised(self, given):
        coerce = operation_aliases(("get", "remove"), {"list": "get"})
        assert coerce(given) == "get"

    def test_a_synonym_outside_the_table_is_left_alone(self):
        coerce = operation_aliases(("get", "remove"), {"list": "get"})
        assert coerce("update") == "update"

    def test_non_strings_are_left_alone(self):
        coerce = operation_aliases(("get", "remove"), {"list": "get"})
        assert coerce(7) == 7
        assert coerce(None) is None


class TestSynonymsReachTheTool:
    """End to end: the synonym must reach the API, not the schema."""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "operation", "extra", "reached"),
        [
            ("manageLists", "list", {"list_type": "allowlist"}, "GET /profiles/"),
            ("manageLists", "delete", {"list_type": "allowlist", "entry_id": "x"}, "DELETE /profiles/"),
            ("manageProfiles", "remove", {}, "DELETE /profiles/"),
            ("manageProfiles", "rename", {"name": "n"}, "PATCH /profiles/"),
            ("manageProfiles", "add", {"name": "n"}, "POST /profiles"),
            ("manageRewrites", "get", {}, "GET /profiles/"),
            ("manageLogs", "list", {}, "GET /profiles/"),
        ],
    )
    async def test_synonym_reaches_the_upstream_call(self, tool, operation, extra, reached):
        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(tool, {"operation": operation, "profile_id": "23228d", **extra})
        error = str((getattr(result, "structured_content", None) or {}).get("error", ""))
        assert "Input should be" not in error
        assert reached in error  # the dummy key makes it a 403, but the verb is what matters

    @pytest.mark.asyncio
    async def test_an_unknown_operation_is_still_rejected(self):
        async with Client(build_mcp_server()) as client:
            with pytest.raises(ToolError, match="Input should be"):
                await client.call_tool(
                    "manageLists", {"operation": "frobnicate", "list_type": "allowlist", "profile_id": "23228d"}
                )

    @pytest.mark.asyncio
    async def test_the_published_enums_are_unchanged(self):
        async with Client(build_mcp_server()) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["manageLists"].input_schema["properties"]["operation"]["enum"] == [
            "get",
            "add",
            "replace",
            "update",
            "remove",
        ]
        assert tools["manageProfiles"].input_schema["properties"]["operation"]["enum"] == [
            "list",
            "create",
            "get",
            "update",
            "delete",
        ]
        assert tools["manageRewrites"].input_schema["properties"]["operation"]["enum"] == ["list", "add", "delete"]
        assert tools["manageLogs"].input_schema["properties"]["operation"]["enum"] == ["get", "clear", "download"]
