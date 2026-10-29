"""A rewrite hostname in `entry_id` is resolved instead of 404ing.

The endpoint keys deletes on the opaque id it assigns; the caller's task names
the rewrite by hostname, and live runs show that hostname sent as entry_id.
The tool already resolved a name when entry_id was absent - this covers the
caller that put the name in entry_id, which is what the model actually does.

SPDX-License-Identifier: MIT
"""

import pytest

from nextdns_mcp.tools import rewrites


def _fake_api(log):
    """Stand in for the transport: ids are opaque, names are hostnames."""

    async def fake(method, url, params=None, json_body=None):
        log.append((method, url))
        if method == "GET":
            return {"data": [{"id": "j2k3zl", "name": "router.home", "content": "192.168.1.10"}]}
        if method == "DELETE" and url.endswith("/router.home"):
            return {
                "error": "HTTP error in DELETE: Client error '404 Not Found'",
                "code": "http_error",
                "status_code": 404,
                "upstream_code": "notFound",
            }
        return {"success": True}

    return fake


class TestDeleteByTheNameTheCallerHas:
    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    async def test_a_hostname_in_entry_id_is_resolved_and_deleted(self, monkeypatch):
        log = []
        monkeypatch.setattr(rewrites, "_api_request_payload", _fake_api(log))
        result = await rewrites.manageRewrites(operation="delete", profile_id="abc123", entry_id="router.home")
        assert result.get("success") is True, result
        assert result.get("resolved_from_name") == "router.home"
        assert ("DELETE", "/profiles/abc123/rewrites/j2k3zl") in log

    async def test_a_name_that_matches_nothing_keeps_the_original_404(self, monkeypatch):
        """No second guess, and no loop: one lookup, then the upstream answer."""

        async def fake(method, url, params=None, json_body=None):
            if method == "GET":
                return {"data": []}
            return {
                "error": "HTTP error in DELETE: Client error '404 Not Found'",
                "code": "http_error",
                "status_code": 404,
                "upstream_code": "notFound",
            }

        monkeypatch.setattr(rewrites, "_api_request_payload", fake)
        result = await rewrites.manageRewrites(operation="delete", profile_id="abc123", entry_id="gone.home")
        assert result.get("upstream_code") == "notFound"
        assert "resolved_from_name" not in result

    async def test_a_successful_delete_by_id_does_not_pay_for_the_lookup(self, monkeypatch):
        log = []
        monkeypatch.setattr(rewrites, "_api_request_payload", _fake_api(log))
        result = await rewrites.manageRewrites(operation="delete", profile_id="abc123", entry_id="j2k3zl")
        assert result.get("success") is True
        assert [method for method, _ in log] == ["DELETE"]


class TestAddNamesWhatItNeeds:
    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    async def test_a_missing_rewrite_says_what_name_and_content_are(self):
        result = await rewrites.manageRewrites(operation="add", profile_id="abc123")
        assert "name and content are required" in str(result.get("error"))
        hint = str(result.get("hint", ""))
        assert "hostname to answer for" in hint
        assert "CNAME" in hint
