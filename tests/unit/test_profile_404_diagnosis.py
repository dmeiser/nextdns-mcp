"""A 404 under /profiles/<id> says which id it could be blaming, and the
examples the server publishes never offer an identifier that could be sent.

Live runs show the model calling manageRewrites(add, profile_id="abc123") and
manageProfiles(create, name="Home Network") - values copied out of the tool
descriptions. `abc123` is six hex characters, which is exactly the shape of a
real NextDNS profile id, so nothing about it reads as a placeholder.

SPDX-License-Identifier: MIT
"""

from unittest.mock import MagicMock

import pytest

from nextdns_mcp import utils
from nextdns_mcp.errors import ErrorCode, NextDNSError


def _not_found(method: str, url: str) -> dict:
    return {
        "error": f"HTTP error in {method} {url}: Client error '404 Not Found'",
        "code": ErrorCode.HTTP_ERROR,
        "status_code": 404,
        "upstream_code": "notFound",
    }


class TestTheTwoIdsAProfilePathCanMiss:
    def test_a_404_names_both_ids_it_could_mean(self):
        payload = utils._note_404_on_a_profile_path(
            _not_found("POST", "/profiles/abc123/rewrites"), "/profiles/abc123/rewrites"
        )
        hint = payload["hint"]
        assert "profile id or the entry id" in hint
        assert 'manageProfiles(operation="list")' in hint

    def test_a_404_on_an_entry_under_a_real_profile_gets_the_same_two_way_hint(self):
        """The server cannot tell which id missed, so the hint must not claim one."""
        payload = utils._note_404_on_a_profile_path(
            _not_found("PATCH", "/profiles/51c8a4/parentalControl/services/tiktok"),
            "/profiles/51c8a4/parentalControl/services/tiktok",
        )
        assert "51c8a4" not in payload["hint"]

    @pytest.mark.parametrize(
        ("payload", "url"),
        [
            (_not_found("GET", "/profiles"), "/profiles"),
            ({**_not_found("GET", "/x"), "upstream_code": "duplicate"}, "/profiles/abc123/rewrites"),
            ({"success": True}, "/profiles/abc123/rewrites"),
        ],
    )
    def test_anything_that_is_not_a_profile_404_is_left_alone(self, payload, url):
        assert "hint" not in utils._note_404_on_a_profile_path(payload, url)

    async def test_the_wiring_reaches_a_tool_call(self, monkeypatch):
        """The hint is added where the URL is known, not by each tool."""

        async def boom(method: str, url: str, params=None, json_body=None):
            response = MagicMock()
            response.status_code = 404
            response.json.return_value = {"errors": [{"code": "notFound"}]}
            response.text = '{"errors":[{"code":"notFound"}]}'
            error = NextDNSError(f"HTTP error in {method} {url}: Client error '404 Not Found'", status_code=404)
            error.response = response  # type: ignore[attr-defined]
            raise error

        monkeypatch.setattr(utils, "_api_request", boom)
        payload = await utils._api_request_payload("POST", "/profiles/abc123/rewrites")
        assert payload["upstream_code"] == "notFound"
        assert "profile id or the entry id" in str(payload.get("hint", ""))


class TestPublishedExamples:
    """Examples are input, not illustration: a caller copies what it is shown."""

    @pytest.mark.asyncio
    async def test_no_published_description_offers_a_mistakable_profile_id(self, monkeypatch):
        from fastmcp import Client

        from nextdns_mcp.server import build_mcp_server

        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        async with Client(build_mcp_server()) as client:
            described = {tool.name: (tool.description or "") for tool in await client.list_tools()}
        assert described, "no tools published"
        shown = 0
        for name, description in described.items():
            assert "abc123" not in description, name
            if "profile_id=" in description:
                assert 'profile_id="<profile id>"' in description, name
                shown += 1
        assert shown >= 6, f"only {shown} tools show a profile_id example"

    def test_the_usage_guide_the_server_sends_is_the_same(self):
        from nextdns_mcp.usage import nextdns_usage_guide

        guide = nextdns_usage_guide()
        assert "abc123" not in guide
        assert 'profile_id="<profile id>"' in guide
