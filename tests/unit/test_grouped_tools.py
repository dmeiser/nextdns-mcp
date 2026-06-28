"""Unit tests for the grouped CRUD tools in server.py."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextdns_mcp import server


@pytest.fixture
def mock_api_client(monkeypatch):
    """Replace the module-level api_client with a mock."""
    client = AsyncMock()
    monkeypatch.setattr(server, "api_client", client)
    return client


def _make_response(json_data=None, status_code=200, content=None):
    """Build a mock httpx.Response."""
    response = MagicMock()
    response.status_code = status_code
    if content is None:
        response.content = b'{"ok": true}' if json_data is None else b'{"dummy": true}'
    else:
        response.content = content
    response.json.return_value = json_data if json_data is not None else {"ok": True}
    return response


class TestCoerceJsonArg:
    """Tests for the _coerce_json_arg helper."""

    def test_parses_json_object_string(self):
        assert server._coerce_json_arg('{"enabled": true}') == {"enabled": True}

    def test_parses_json_array_string(self):
        assert server._coerce_json_arg('[{"id":"x"}]') == [{"id": "x"}]

    def test_returns_non_strings_unchanged(self):
        assert server._coerce_json_arg({"a": 1}) == {"a": 1}
        assert server._coerce_json_arg([1, 2]) == [1, 2]

    def test_returns_invalid_json_string_unchanged(self):
        assert server._coerce_json_arg("not-json") == "not-json"


class TestApiRequest:
    """Tests for the shared _api_request helper."""

    @pytest.mark.asyncio
    async def test_success_json(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": [1, 2, 3]})
        result = await server._api_request("GET", "/profiles")
        assert result == {"data": [1, 2, 3]}
        mock_api_client.request.assert_called_once_with("GET", "/profiles", params=None, json=None)

    @pytest.mark.asyncio
    async def test_success_204(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server._api_request("DELETE", "/profiles/abc")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_http_error(self, mock_api_client):
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 500
        mock_api_client.request.side_effect = exc
        result = await server._api_request("GET", "/profiles")
        assert "error" in result
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_unexpected_error(self, mock_api_client):
        mock_api_client.request.side_effect = RuntimeError("unexpected")
        result = await server._api_request("GET", "/profiles")
        assert "error" in result


class TestGetOpenapiToolNames:
    """Tests for get_openapi_tool_names."""

    def test_extracts_operation_ids(self):
        spec = {
            "paths": {
                "/profiles": {
                    "get": {"operationId": "listProfiles"},
                    "post": {"operationId": "createProfile"},
                },
                "/profiles/{id}": {
                    "get": {"operationId": "getProfile"},
                    "patch": {"operationId": "updateProfile"},
                    "delete": {"operationId": "deleteProfile"},
                },
            }
        }
        assert server.get_openapi_tool_names(spec) == {
            "listProfiles",
            "createProfile",
            "getProfile",
            "updateProfile",
            "deleteProfile",
        }

    def test_ignores_non_operations(self):
        spec = {"paths": {"/x": {"parameters": []}}}
        assert server.get_openapi_tool_names(spec) == set()


class TestManageProfiles:
    """Tests for manageProfiles grouped tool."""

    @pytest.mark.asyncio
    async def test_list(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        result = await server.manageProfiles("list")
        assert result == {"data": []}

    @pytest.mark.asyncio
    async def test_create(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": {"id": "abc123"}})
        result = await server.manageProfiles("create", name="Test")
        assert result == {"data": {"id": "abc123"}}
        mock_api_client.request.assert_called_once_with("POST", "/profiles", params=None, json={"name": "Test"})

    @pytest.mark.asyncio
    async def test_create_missing_name(self, mock_api_client):
        result = await server.manageProfiles("create")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": {"id": "abc123"}})
        result = await server.manageProfiles("get", profile_id="abc123")
        assert result == {"data": {"id": "abc123"}}

    @pytest.mark.asyncio
    async def test_update(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageProfiles("update", profile_id="abc123", name="New")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_update_missing_name(self, mock_api_client):
        result = await server.manageProfiles("update", profile_id="abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageProfiles("delete", profile_id="abc123")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_missing_profile_id(self, mock_api_client):
        result = await server.manageProfiles("get")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unsupported_operation(self):
        result = await server._manage_profiles_impl("nope", profile_id="abc123")
        assert "Unsupported operation" in result["error"]


class TestManageSettings:
    """Tests for manageSettings grouped tool."""

    @pytest.mark.parametrize(
        "category", ["general", "privacy", "security", "parental", "performance", "logs", "blockpage"]
    )
    @pytest.mark.asyncio
    async def test_get_each_category(self, category, mock_api_client):
        mock_api_client.request.return_value = _make_response({"enabled": True})
        result = await server.manageSettings("get", category, "abc123")
        assert result == {"enabled": True}
        path = server._SETTINGS_PATHS[category]
        mock_api_client.request.assert_called_once_with("GET", f"/profiles/abc123/{path}", params=None, json=None)

    @pytest.mark.asyncio
    async def test_update(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageSettings("update", "general", "abc123", settings={"web3": True})
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_update_with_json_string(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageSettings("update", "general", "abc123", settings='{"web3":true}')
        mock_api_client.request.assert_called_once_with(
            "PATCH", "/profiles/abc123/settings", params=None, json={"web3": True}
        )
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_update_with_invalid_settings_string(self, mock_api_client):
        result = await server.manageSettings("update", "general", "abc123", settings="not-json")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_missing_settings(self, mock_api_client):
        result = await server.manageSettings("update", "general", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unsupported_operation(self):
        result = await server._manage_settings_impl("nope", "general", "abc123")
        assert "Unsupported operation" in result["error"]


class TestManageLists:
    """Tests for manageLists grouped tool."""

    @pytest.mark.parametrize(
        "list_type",
        [
            "allowlist",
            "denylist",
            "privacy_blocklists",
            "privacy_natives",
            "security_tlds",
            "parental_categories",
            "parental_services",
        ],
    )
    @pytest.mark.asyncio
    async def test_get_each_list(self, list_type, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        result = await server.manageLists(list_type, "get", "abc123")
        assert result == {"data": []}

    @pytest.mark.asyncio
    async def test_add_with_dict(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("allowlist", "add", "abc123", entry={"id": "example.com"})
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_add_with_string(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("denylist", "add", "abc123", entry="bad.com")
        mock_api_client.request.assert_called_once_with(
            "POST", "/profiles/abc123/denylist", params=None, json={"id": "bad.com"}
        )
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_add_with_json_string(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("allowlist", "add", "abc123", entry='{"id":"example.com"}')
        mock_api_client.request.assert_called_once_with(
            "POST", "/profiles/abc123/allowlist", params=None, json={"id": "example.com"}
        )
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_add_missing_entry(self, mock_api_client):
        result = await server.manageLists("allowlist", "add", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_replace(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("allowlist", "replace", "abc123", entries=[{"id": "x"}])
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_replace_with_json_string(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("allowlist", "replace", "abc123", entries='[{"id":"x"}]')
        mock_api_client.request.assert_called_once_with(
            "PUT", "/profiles/abc123/allowlist", params=None, json=[{"id": "x"}]
        )
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_replace_with_invalid_entries_string(self, mock_api_client):
        result = await server.manageLists("allowlist", "replace", "abc123", entries="not-array")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_replace_missing_entries(self, mock_api_client):
        result = await server.manageLists("allowlist", "replace", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("denylist", "update", "abc123", entry_id="bad.com", entry={"active": False})
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_update_missing_entry_id(self, mock_api_client):
        result = await server.manageLists("denylist", "update", "abc123", entry={"active": False})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_entry_not_dict(self, mock_api_client):
        result = await server.manageLists("denylist", "update", "abc123", entry_id="bad.com", entry="true")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_unsupported_list_type(self, mock_api_client):
        result = await server.manageLists("security_tlds", "update", "abc123", entry_id="zip", entry={"active": False})
        assert "error" in result
        assert "not supported" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_remove(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLists("allowlist", "remove", "abc123", entry_id="example.com")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_remove_missing_entry_id(self, mock_api_client):
        result = await server.manageLists("allowlist", "remove", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unsupported_operation(self):
        result = await server._manage_lists_impl("allowlist", "nope", "abc123")
        assert "Unsupported operation" in result["error"]


class TestManageRewrites:
    """Tests for manageRewrites grouped tool."""

    @pytest.mark.asyncio
    async def test_list(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        result = await server.manageRewrites("list", "abc123")
        assert result == {"data": []}

    @pytest.mark.asyncio
    async def test_add(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": {"id": "rew1"}})
        result = await server.manageRewrites("add", "abc123", name="x.com", content="1.2.3.4")
        assert result == {"data": {"id": "rew1"}}

    @pytest.mark.asyncio
    async def test_add_missing_name(self, mock_api_client):
        result = await server.manageRewrites("add", "abc123", content="1.2.3.4")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageRewrites("delete", "abc123", entry_id="rew1")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_delete_missing_entry_id(self, mock_api_client):
        result = await server.manageRewrites("delete", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unsupported_operation(self):
        result = await server._manage_rewrites_impl("nope", "abc123")
        assert "Unsupported operation" in result["error"]


class TestManageLogs:
    """Tests for manageLogs grouped tool."""

    @pytest.mark.asyncio
    async def test_get(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        result = await server.manageLogs(
            "get", "abc123", from_time="1", to_time="2", limit=10, user="x", device="d", format="raw"
        )
        assert result == {"data": []}
        mock_api_client.request.assert_called_once_with(
            "GET",
            "/profiles/abc123/logs",
            params={"from": "1", "to": "2", "limit": 10, "device": "d", "search": "x", "raw": True},
            json=None,
        )

    @pytest.mark.asyncio
    async def test_clear(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLogs("clear", "abc123")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_download(self, mock_api_client):
        response = MagicMock()
        response.status_code = 200
        response.content = b"csv,data"
        response.headers = {"content-type": "text/csv"}
        response.text = "csv,data"
        mock_api_client.get.return_value = response
        result = await server.manageLogs("download", "abc123")
        assert result["content_type"] == "text/csv"
        assert result["size"] == 8

    @pytest.mark.asyncio
    async def test_download_http_error(self, mock_api_client):
        mock_api_client.get.side_effect = httpx.HTTPError("boom")
        result = await server.manageLogs("download", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_download_unexpected_error(self, mock_api_client):
        mock_api_client.get.side_effect = RuntimeError("boom")
        result = await server.manageLogs("download", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unsupported_operation(self):
        result = await server._manage_logs_impl("nope", "abc123")
        assert "Unsupported operation" in result["error"]


class TestQueryAnalytics:
    """Tests for queryAnalytics grouped tool."""

    @pytest.mark.asyncio
    async def test_base(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        result = await server.queryAnalytics("status", "abc123", from_time="-1d", limit=5)
        assert result == {"data": []}
        mock_api_client.request.assert_called_once_with(
            "GET", "/profiles/abc123/analytics/status", params={"from": "-1d", "limit": 5}, json=None
        )

    @pytest.mark.asyncio
    async def test_series(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.queryAnalytics("devices", "abc123", from_time="-1d", series=True, interval=3600)
        mock_api_client.request.assert_called_once_with(
            "GET",
            "/profiles/abc123/analytics/devices;series",
            params={"from": "-1d", "interval": 3600},
            json=None,
        )

    @pytest.mark.asyncio
    async def test_destinations_missing_type(self, mock_api_client):
        result = await server.queryAnalytics("destinations", "abc123")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_destinations_with_type(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.queryAnalytics("destinations", "abc123", destination_type="countries")
        mock_api_client.request.assert_called_once_with(
            "GET",
            "/profiles/abc123/analytics/destinations",
            params={"type": "countries"},
            json=None,
        )


class TestUsageGuidePrompt:
    """Tests for the nextdns-usage-guide MCP prompt."""

    def test_prompt_contains_tool_names_and_workflows(self):
        guide = server.nextdns_usage_guide()
        assert "# NextDNS MCP Server Usage Guide" in guide
        assert "manageProfiles" in guide
        assert "manageSettings" in guide
        assert "manageLists" in guide
        assert "plotAnalytics" in guide
        assert "Block a domain" in guide
        assert "View blocked query trends" in guide
