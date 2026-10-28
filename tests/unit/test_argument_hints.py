"""An argument the caller forgot is explained, not just refused.

FastMCP rejects a missing parameter with pydantic's text, which names no valid
values and no meaning - so a caller that guessed wrong guesses the same way
again. Live runs: manageSettings without `category` 13 times, dohLookup without
`domain` 25 times. The server now appends what its own published schema says
about the parameter at fault. It does not guess the caller's intent.

SPDX-License-Identifier: MIT
"""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from nextdns_mcp.server import build_mcp_server


class TestMissingArguments:
    """The gap this closes: a missing argument said nothing about the argument."""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    async def _error(self, tool: str, arguments: dict) -> str:
        async with Client(build_mcp_server()) as client:
            with pytest.raises(ToolError) as raised:
                await client.call_tool(tool, arguments)
        return str(raised.value)

    @pytest.mark.asyncio
    async def test_a_missing_choice_names_the_choices(self):
        error = await self._error("manageSettings", {"operation": "update", "profile_id": "23228d", "settings": {}})
        assert "category is required for manageSettings" in error
        assert "general, privacy, security, parental, performance, logs, blockpage" in error

    @pytest.mark.asyncio
    async def test_a_missing_value_says_what_it_is_for(self):
        error = await self._error("dohLookup", {"profile_id": "23228d", "record_type": "A"})
        assert "domain is required for dohLookup" in error
        assert "domain name to look up" in error

    @pytest.mark.asyncio
    async def test_the_required_set_is_listed_when_the_caller_supplied_some(self):
        error = await self._error("queryAnalytics", {"profile_id": "23228d"})
        assert "Required arguments for queryAnalytics: metric, profile_id." in error

    @pytest.mark.asyncio
    async def test_the_original_message_survives(self):
        """Enrichment, not replacement: anything keyed on the old text still works."""
        error = await self._error("manageSettings", {"operation": "update", "profile_id": "23228d", "settings": {}})
        assert "Missing required argument" in error
        assert "Input should be" not in error  # still a missing value, not a bad one

    @pytest.mark.asyncio
    async def test_an_invalid_value_is_not_padded_with_values_it_already_has(self):
        """Pydantic lists the accepted values for a bad one; repeating them is noise."""
        error = await self._error("queryAnalytics", {"metric": "bogus", "profile_id": "23228d"})
        assert "Input should be" in error
        assert "It must be one of" not in error

    @pytest.mark.asyncio
    async def test_a_call_the_tool_can_answer_is_left_alone(self):
        """The list_type synonym must still reach the API, not the schema."""
        async with Client(build_mcp_server()) as client:
            result = await client.call_tool(
                "manageLists", {"operation": "get", "list_type": "blocklists", "profile_id": "23228d"}
            )
        error = str((getattr(result, "structured_content", None) or {}).get("error", ""))
        assert "Input should be" not in error
        assert "privacy/blocklists" in error


class TestCallLogSeesTheExplanation:
    """Ordering matters: the server's own record has to carry what the caller was told."""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        self.log = tmp_path / "calls.jsonl"
        monkeypatch.setenv("NEXTDNS_CALL_LOG", str(self.log))

    @pytest.mark.asyncio
    async def test_the_recorded_error_carries_the_hint(self):
        async with Client(build_mcp_server()) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "manageSettings", {"operation": "update", "profile_id": "23228d", "settings": {}}
                )
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        record = next(row for row in rows if row["tool"] == "manageSettings")
        assert record["ok"] is False
        assert "category is required" in record["error"]
