"""Unit tests for the grouped CRUD tools in server.py."""

import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import server
from nextdns_mcp.tools import logs as logs_module
from nextdns_mcp.tools import profiles as profiles_module


@pytest.fixture
def mock_api_client(monkeypatch):
    """Replace the module-level api_client with a mock."""
    client = AsyncMock()
    monkeypatch.setattr(client_module, "api_client", client)
    return client


@pytest.fixture(autouse=True)
def open_profile_access(monkeypatch):
    """Allow all profile read/write access for grouped-tool tests."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


class _stream_ctx:
    """Async context manager mimicking httpx.AsyncClient.stream()."""

    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


def _async_chunks(chunks):
    """Async iterator yielding the given text chunks (for streamed responses)."""

    async def _gen():
        for chunk in chunks:
            yield chunk

    return _gen()


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

    def test_returns_malformed_json_object_string_unchanged(self):
        # Starts with '{' but is invalid JSON: must fall through the
        # except clause instead of raising.
        assert server._coerce_json_arg('{"enabled": true') == '{"enabled": true'


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
        exc.response.text = "internal server error"
        mock_api_client.request.side_effect = exc
        result = await server._api_request("GET", "/profiles")
        assert "error" in result
        assert result["code"] == "http_error"
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_http_error_status_distinguishable(self, mock_api_client):
        """401/403/429/5xx failures all surface as http_error with distinct status_code."""
        for status in (401, 403, 429, 503):
            exc = httpx.HTTPError("boom")
            exc.response = MagicMock()
            exc.response.status_code = status
            exc.response.text = None
            mock_api_client.request.side_effect = exc
            result = await server._api_request("GET", "/profiles")
            assert result["code"] == "http_error"
            assert result["status_code"] == status

    @pytest.mark.asyncio
    async def test_http_error_no_response(self, mock_api_client):
        mock_api_client.request.side_effect = httpx.ConnectError("network down")
        result = await server._api_request("GET", "/profiles")
        assert result["code"] == "http_error"
        assert result["status_code"] is None
        assert "response_body" not in result

    @pytest.mark.asyncio
    async def test_unexpected_error(self, mock_api_client):
        mock_api_client.request.side_effect = RuntimeError("unexpected")
        result = await server._api_request("GET", "/profiles")
        assert "error" in result
        assert result["code"] == "internal_error"


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
    async def test_profile_id_coerced_from_int(self, mock_api_client):
        """All-numeric profile IDs sent as integers are coerced to strings."""
        mock_api_client.request.return_value = _make_response({"enabled": True})
        result = await server.manageSettings("get", "general", 315244)
        assert result == {"enabled": True}
        mock_api_client.request.assert_called_once_with("GET", "/profiles/315244/settings", params=None, json=None)

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
            "get", "abc123", from_time="1", to_time="2", limit=10, user="x", device="d", raw=True
        )
        assert result == {"data": []}
        mock_api_client.request.assert_called_once_with(
            "GET",
            "/profiles/abc123/logs",
            params={"from": "1", "to": "2", "limit": 10, "device": "d", "search": "x", "raw": "true"},
            json=None,
        )

    @pytest.mark.asyncio
    async def test_clear(self, mock_api_client):
        mock_api_client.request.return_value = _make_response(status_code=204, content=b"")
        result = await server.manageLogs("clear", "abc123")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_download(self, mock_api_client):
        """Download streams to a temp file and returns a capped preview, not inline data."""
        csv_text = "date,time,question,answer\n2024-01-01,12:00:00,example.com,A\n"
        response = MagicMock()
        response.status_code = 200
        response.has_redirect_location = False
        response.headers = {"content-type": "text/csv"}
        response.raise_for_status.return_value = None
        response.aiter_text = lambda: _async_chunks([csv_text])
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        mock_api_client.stream.assert_called_once_with("GET", "/profiles/abc123/logs/download")
        assert result["content_type"] == "text/csv"
        assert result["size"] == len(csv_text.encode("utf-8"))
        assert result["row_count"] == 2
        assert "data" not in result
        assert "text" not in result
        assert os.path.isfile(result["file_path"])
        assert await asyncio.to_thread(Path(result["file_path"]).read_text, encoding="utf-8") == csv_text
        os.unlink(result["file_path"])

    @pytest.mark.asyncio
    async def test_download_large_is_capped_not_inline(self, mock_api_client):
        """A large download is not double-buffered or inlined: only a bounded preview is returned."""
        row = "x" * 1000 + "\n"
        # ~10k rows, multi-MB — far beyond any sane inline payload.
        chunks = [row * 500 for _ in range(20)]
        total_text = "".join(chunks)
        response = MagicMock()
        response.status_code = 200
        response.has_redirect_location = False
        response.headers = {"content-type": "text/csv"}
        response.raise_for_status.return_value = None
        response.aiter_text = lambda: _async_chunks(chunks)
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        assert result["size"] == len(total_text.encode("utf-8"))
        assert result["row_count"] == 10_000
        # The preview is bounded and flagged truncated; the full body never appears inline.
        assert len(result["preview"]["text"]) < len(total_text)
        assert result["preview"]["truncated"] is True
        assert result["preview"]["line_count"] <= 20
        assert result["preview"]["bytes"] <= 256 * 1024
        assert "x" * 1000 * 2 not in result["preview"]["text"]
        await asyncio.to_thread(os.unlink, result["file_path"])

    @pytest.mark.asyncio
    async def test_download_empty_chunk_is_skipped(self, mock_api_client):
        """An empty text chunk from the stream does not corrupt counts or preview."""
        csv_text = "date,time,question,answer\n2024-01-01,12:00:00,example.com,A\n"
        response = MagicMock()
        response.has_redirect_location = False
        response.headers = {"content-type": "text/csv"}
        response.raise_for_status.return_value = None
        response.aiter_text = lambda: _async_chunks(["", csv_text, ""])
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        assert result["row_count"] == 2
        assert result["preview"]["line_count"] == 2
        assert result["preview"]["truncated"] is False
        await asyncio.to_thread(os.unlink, result["file_path"])

    @pytest.mark.asyncio
    async def test_download_preview_capped_by_bytes(self, mock_api_client):
        """A single line larger than the byte cap yields a truncated preview."""
        header = "date,time,question,answer\n"
        huge_line = "x" * (300 * 1024) + "\n"
        response = MagicMock()
        response.has_redirect_location = False
        response.headers = {"content-type": "text/csv"}
        response.raise_for_status.return_value = None
        response.aiter_text = lambda: _async_chunks([header + huge_line])
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        assert result["preview"]["bytes"] <= 256 * 1024
        assert result["preview"]["text"] == header
        assert result["preview"]["truncated"] is True
        assert result["row_count"] == 2
        await asyncio.to_thread(os.unlink, result["file_path"])

    @pytest.mark.asyncio
    async def test_download_final_line_without_newline_counts_as_row(self, mock_api_client):
        """A CSV whose last line has no trailing newline still counts that row."""
        csv_text = "date,time,question,answer\n2024-01-01,12:00:00,example.com,A"
        response = MagicMock()
        response.has_redirect_location = False
        response.headers = {"content-type": "text/csv"}
        response.raise_for_status.return_value = None
        response.aiter_text = lambda: _async_chunks([csv_text])
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        assert result["row_count"] == 2
        assert result["preview"]["line_count"] == 2
        await asyncio.to_thread(os.unlink, result["file_path"])

    def test_unlink_temp_file_missing_path_logs_warning(self, caplog):
        """A failed temp-file removal logs a warning and never raises."""
        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tools.logs"):
            logs_module._unlink_temp_file("/nonexistent/nextdns_logs_missing.csv")
        assert not os.path.exists("/nonexistent/nextdns_logs_missing.csv")
        assert any("Failed to remove temp log file" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_download_does_not_pass_follow_redirects_to_authenticated_client(self, mock_api_client):
        """The authenticated client must never follow redirects (issue #130)."""
        response = MagicMock()
        response.has_redirect_location = False
        response.headers = {"content-type": "text/csv"}
        response.raise_for_status.return_value = None
        response.aiter_text = lambda: _async_chunks(["csv,data\n"])
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        args, kwargs = mock_api_client.stream.call_args
        assert kwargs.get("follow_redirects") is not True
        assert all(arg is not True for arg in args)
        os.unlink(result["file_path"])


class _FakeFinalClient:
    """Unauthenticated stand-in whose streamed fetch returns the final CSV body."""

    def __init__(self, *args, **kwargs):
        self.last_request = None

    def stream(self, method, url, **kwargs):
        request = httpx.Request("GET", str(url))
        self.last_request = request
        response = httpx.Response(200, request=request, headers={"content-type": "text/csv"}, content=b"csv,data")

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def aclose(self) -> None:
        pass


class TestManageLogsDownloadRedirects:
    """Regression tests for issue #130: the API key must not leak on log-download redirects."""

    def _redirect_response(self, location: str) -> httpx.Response:
        """Build a 302 redirect response for the authenticated download request."""
        request = httpx.Request("GET", "https://api.nextdns.io/profiles/abc123/logs/download")
        return httpx.Response(302, request=request, headers={"location": location})

    def _route_through_redirect(self, mock_api_client, location: str, fake):
        """Route the authenticated stream through a 302 and the unauthenticated client to ``fake``."""
        redirect = self._redirect_response(location)

        class _RedirectCtx:
            async def __aenter__(self):
                return redirect

            async def __aexit__(self, *exc):
                return False

        mock_api_client.stream = MagicMock(return_value=_RedirectCtx())

        def fake_client(*args, **kwargs):
            return fake

        return fake_client

    @pytest.mark.asyncio
    async def test_api_key_absent_on_redirected_request(self, mock_api_client, monkeypatch):
        """Mock-302 key-leak scenario: the X-Api-Key must be absent on the redirected request."""
        fake = _FakeFinalClient()
        monkeypatch.setattr(
            logs_module.httpx,
            "AsyncClient",
            self._route_through_redirect(
                mock_api_client, "https://cdn.example.com/profiles/abc123/logs.csv?sig=1", fake
            ),
        )

        result = await server.manageLogs("download", "abc123")
        assert result["content_type"] == "text/csv"
        assert result["size"] == 8
        assert result["row_count"] == 1
        assert "data" not in result

        # The authenticated client was not asked to follow the redirect.
        mock_api_client.stream.assert_called_once_with("GET", "/profiles/abc123/logs/download")
        assert mock_api_client.stream.call_args.kwargs.get("follow_redirects") is not True

        # The redirected request went to the third-party host...
        assert str(fake.last_request.url) == "https://cdn.example.com/profiles/abc123/logs.csv?sig=1"
        # ...and it carried no API key header.
        assert "x-api-key" not in {k.lower() for k in fake.last_request.headers}
        os.unlink(result["file_path"])

    @pytest.mark.asyncio
    async def test_relative_location_resolved_against_origin(self, mock_api_client, monkeypatch):
        """A relative Location is resolved against the API origin and still sent unauthenticated."""
        fake = _FakeFinalClient()
        monkeypatch.setattr(
            logs_module.httpx,
            "AsyncClient",
            self._route_through_redirect(mock_api_client, "/redirects/abc123.csv", fake),
        )

        result = await server.manageLogs("download", "abc123")
        assert result["size"] == 8
        assert str(fake.last_request.url) == "https://api.nextdns.io/redirects/abc123.csv"
        assert "x-api-key" not in {k.lower() for k in fake.last_request.headers}
        os.unlink(result["file_path"])

    @pytest.mark.asyncio
    async def test_redirect_loop_is_bounded(self, mock_api_client, monkeypatch):
        """A redirect loop must raise a bounded HTTP error instead of looping forever."""

        class _LoopClient:
            def __init__(self, *args, **kwargs):
                pass

            def stream(self, method, url, **kwargs):
                request = httpx.Request("GET", str(url))
                response = httpx.Response(
                    302, request=request, headers={"location": "https://cdn.example.com/again.csv"}
                )

                class _Ctx:
                    async def __aenter__(self):
                        return response

                    async def __aexit__(self, *exc):
                        return False

                return _Ctx()

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(logs_module.httpx, "AsyncClient", _LoopClient)

        class _RedirectCtx:
            async def __aenter__(self):
                return self._redirect_response("https://cdn.example.com/logs.csv")

            async def __aexit__(self, *exc):
                return False

        loop_redirect = self._redirect_response("https://cdn.example.com/logs.csv")

        class _RedirectCtx:
            async def __aenter__(self):
                return loop_redirect

            async def __aexit__(self, *exc):
                return False

        mock_api_client.stream = MagicMock(return_value=_RedirectCtx())

        result = await server.manageLogs("download", "abc123")
        assert "error" in result
        assert "Exceeded 5 redirects" in result["error"]

    @pytest.mark.asyncio
    async def test_download_http_error(self, mock_api_client):
        response = MagicMock()
        response.has_redirect_location = False
        response.headers = {}
        exc = httpx.HTTPStatusError("boom", request=MagicMock(), response=httpx.Response(500, headers={}))
        response.raise_for_status.side_effect = exc
        mock_api_client.stream = MagicMock(return_value=_stream_ctx(response))
        result = await server.manageLogs("download", "abc123")
        assert "error" in result
        assert result["code"] == "http_error"
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_download_unexpected_error(self, mock_api_client):
        mock_api_client.stream = MagicMock(side_effect=RuntimeError("boom"))
        result = await server.manageLogs("download", "abc123")
        assert result["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_download_denied_profile_returns_403(self, monkeypatch):
        """A profile outside NEXTDNS_READABLE_PROFILES gets a 403 on download, not a file."""
        # Restrict both read and write ACLs: the autouse fixture sets
        # WRITABLE_PROFILES=ALL, which would otherwise make abc123 implicitly
        # readable (write implies read), so it must be narrowed too.
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "xyz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "xyz999")
        real_client = client_module.AccessControlledClient(base_url="https://api.nextdns.io")
        monkeypatch.setattr(logs_module.client, "api_client", real_client)
        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            result = await server.manageLogs("download", "abc123")
        await real_client.aclose()
        mock_send.assert_not_called()
        assert result["status_code"] == 403
        assert "error" in result
        assert "file_path" not in result

    @pytest.mark.asyncio
    async def test_download_denied_in_read_only_mode(self, monkeypatch):
        """In read-only mode the write-implies-read fallback is gone, so download is denied.

        Without read-only, NEXTDNS_WRITABLE_PROFILES=abc123 makes abc123 implicitly
        readable; with read-only enabled the download must get a 403 instead.
        """
        monkeypatch.delenv("NEXTDNS_READABLE_PROFILES")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        real_client = client_module.AccessControlledClient(base_url="https://api.nextdns.io")
        monkeypatch.setattr(logs_module.client, "api_client", real_client)
        with patch.object(httpx.AsyncClient, "send", new_callable=AsyncMock) as mock_send:
            result = await server.manageLogs("download", "abc123")
        await real_client.aclose()
        mock_send.assert_not_called()
        assert result["status_code"] == 403
        assert "file_path" not in result

    @pytest.mark.asyncio
    async def test_get_limit_capped(self, mock_api_client):
        """A limit above the server-side cap is clamped before reaching the API."""
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.manageLogs("get", "abc123", limit=10_000)
        assert mock_api_client.request.call_args.kwargs["params"]["limit"] == 1000

    @pytest.mark.asyncio
    async def test_get_limit_unchanged_under_cap(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.manageLogs("get", "abc123", limit=10)
        assert mock_api_client.request.call_args.kwargs["params"]["limit"] == 10

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
    async def test_limit_capped(self, mock_api_client):
        """A limit above the server-side cap is clamped before reaching the API."""
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.queryAnalytics("status", "abc123", limit=10_000)
        assert mock_api_client.request.call_args.kwargs["params"]["limit"] == 500

    @pytest.mark.asyncio
    async def test_limit_at_cap_unchanged(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.queryAnalytics("status", "abc123", limit=500)
        assert mock_api_client.request.call_args.kwargs["params"]["limit"] == 500

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

    @pytest.mark.asyncio
    async def test_optional_filters_forwarded(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.queryAnalytics(
            "status",
            "abc123",
            from_time="-1d",
            cursor="abc",
            device="device1",
        )
        mock_api_client.request.assert_called_once_with(
            "GET",
            "/profiles/abc123/analytics/status",
            params={"from": "-1d", "cursor": "abc", "device": "device1"},
            json=None,
        )

    @pytest.mark.asyncio
    async def test_domains_filters_forwarded(self, mock_api_client):
        mock_api_client.request.return_value = _make_response({"data": []})
        await server.queryAnalytics(
            "domains",
            "abc123",
            from_time="-1d",
            status="blocked",
            root=True,
        )
        mock_api_client.request.assert_called_once_with(
            "GET",
            "/profiles/abc123/analytics/domains",
            params={"from": "-1d", "status": "blocked", "root": "true"},
            json=None,
        )

    @pytest.mark.asyncio
    async def test_domains_series_rejected(self, mock_api_client):
        result = await server.queryAnalytics("domains", "abc123", series=True)
        assert result["code"] == "unsupported_parameter"
        assert "series=true is not supported" in result["error"]


class TestManageProfilesAccessAndValidation:
    """Tests for manageProfiles access control and ID validation."""

    @pytest.mark.asyncio
    async def test_list_denied_when_no_readable_profiles(self, mock_api_client, monkeypatch):
        monkeypatch.setattr(profiles_module, "get_readable_profiles_set", lambda: None)
        result = await server.manageProfiles("list")
        assert "error" in result
        assert "no profiles are readable" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_create_denied_when_no_writable_profiles(self, mock_api_client, monkeypatch):
        monkeypatch.setattr(profiles_module, "get_writable_profiles_set", lambda: None)
        result = await server.manageProfiles("create", name="Test")
        assert "error" in result
        assert "no profiles are writable" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_create_denied_in_read_only_mode(self, mock_api_client, monkeypatch):
        monkeypatch.setattr(profiles_module, "is_read_only", lambda: True)
        result = await server.manageProfiles("create", name="Test")
        assert "error" in result
        assert "read-only" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.manageProfiles("get", profile_id="abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]


class TestManageSettingsValidation:
    """Tests for manageSettings ID validation."""

    @pytest.mark.asyncio
    async def test_get_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.manageSettings("get", "general", "abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]


class TestManageListsValidation:
    """Tests for manageLists ID validation."""

    @pytest.mark.asyncio
    async def test_get_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.manageLists("allowlist", "get", "abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]

    @pytest.mark.asyncio
    async def test_remove_rejects_invalid_entry_id(self, mock_api_client):
        result = await server.manageLists("allowlist", "remove", "abc123", entry_id="../settings")
        assert "error" in result
        assert "Invalid entry_id format" in result["error"]


class TestManageRewritesValidation:
    """Tests for manageRewrites ID validation."""

    @pytest.mark.asyncio
    async def test_list_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.manageRewrites("list", "abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_rejects_invalid_entry_id(self, mock_api_client):
        result = await server.manageRewrites("delete", "abc123", entry_id="../settings")
        assert "error" in result
        assert "Invalid entry_id format" in result["error"]


class TestManageLogsValidation:
    """Tests for manageLogs ID validation."""

    @pytest.mark.asyncio
    async def test_get_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.manageLogs("get", "abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]


class TestQueryAnalyticsValidation:
    """Tests for queryAnalytics ID validation."""

    @pytest.mark.asyncio
    async def test_base_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.queryAnalytics("status", "abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]


class TestPlotAnalyticsValidation:
    """Tests for plotAnalytics ID validation."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_profile_id(self, mock_api_client):
        result = await server.plotAnalytics("status", profile_id="abc/def")
        assert "error" in result
        assert "Invalid profile_id format" in result["error"]


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
        assert "parental_categories" in guide
        assert "parental_services" in guide
        assert 'entry_id="<id-from-list>"' in guide
        assert "Delete a DNS rewrite" in guide
        assert "router.home" in guide
