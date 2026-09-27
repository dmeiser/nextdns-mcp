"""Regression tests for the standardized structured error contract (issue #148).

Every tool failure must surface as a dict of the form ``{"error": <str>, "code":
<str>, ...}`` - a stable, machine-readable shape LLM callers can predict and
branch on. These tests assert the contract holds across tools and across failure
classes (validation, access control, unsupported input, and HTTP/network errors).

SPDX-License-Identifier: MIT
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import server
from nextdns_mcp.errors import (
    MAX_RESPONSE_BODY_CHARS,
    TRUNCATION_MARKER,
    ErrorCode,
    error_payload,
    http_error_payload,
)
from nextdns_mcp.tools.logs import _manage_logs_impl
from nextdns_mcp.tools.plots import _plot_analytics_series_impl
from nextdns_mcp.tools.profiles import _manage_profiles_impl
from nextdns_mcp.tools.rewrites import _manage_rewrites_impl
from nextdns_mcp.tools.settings import _manage_settings_impl


@pytest.fixture
def mock_api_client(monkeypatch):
    """Replace the module-level api_client with a mock."""
    client = AsyncMock()
    monkeypatch.setattr(client_module, "api_client", client)
    return client


@pytest.fixture(autouse=True)
def open_profile_access(monkeypatch):
    """Allow all profile read/write access for grouped-tool tests."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


def _assert_error_contract(result: Any) -> None:
    """Assert a failure has the standardized structured shape."""
    assert isinstance(result, dict)
    assert "error" in result
    assert isinstance(result["error"], str) and result["error"]
    assert "code" in result
    assert isinstance(result["code"], str) and result["code"]


def _http_error(status_code: int) -> httpx.HTTPError:
    exc = httpx.HTTPError("boom")
    exc.response = MagicMock(status_code=status_code, text=f"server said {status_code}")
    return exc


class TestErrorPayloadShape:
    """The contract: a dict with a human message and a typed code."""

    def test_payload_has_error_and_code(self):
        payload = error_payload(ErrorCode.HTTP_ERROR, "HTTP error 500 in GET /x", status_code=500)
        assert payload["error"] == "HTTP error 500 in GET /x"
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["status_code"] == 500

    def test_payload_codes_are_stable_strings(self):
        assert ErrorCode.INVALID_PROFILE_ID == "invalid_profile_id"
        assert ErrorCode.READ_ACCESS_DENIED == "read_access_denied"
        assert ErrorCode.WRITE_ACCESS_DENIED == "write_access_denied"
        assert ErrorCode.HTTP_ERROR == "http_error"
        assert ErrorCode.INTERNAL_ERROR == "internal_error"
        assert ErrorCode.UNSUPPORTED_OPERATION == "unsupported_operation"

    def test_http_error_payload_surfaces_structured_json_body(self):
        # An ACL denial body: the typed code and reason must be preserved.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 403
        exc.response.json.return_value = {
            "error": "Read access denied for profile: abc123",
            "code": ErrorCode.READ_ACCESS_DENIED,
            "profile_id": "abc123",
        }
        payload = http_error_payload("msg", exc)
        assert payload["code"] == ErrorCode.READ_ACCESS_DENIED
        assert payload["error"] == "Read access denied for profile: abc123"
        assert payload["status_code"] == 403

    def test_http_error_payload_non_json_body(self):
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 500
        exc.response.json.side_effect = ValueError("not json")
        exc.response.text = "<html>500 Bad Gateway</html>"
        payload = http_error_payload("msg", exc)
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["status_code"] == 500
        assert payload["response_body"] == "<html>500 Bad Gateway</html>"
        assert payload["response_body_truncated"] is False

    def test_http_error_payload_bounds_oversized_structured_fields(self):
        # Issue #297: the structured-dict branch must not inline an unbounded
        # upstream document into the tool result the LLM reads. An oversized
        # string field is cut at MAX_RESPONSE_BODY_CHARS with the shared
        # truncation marker, and the payload is flagged so a caller can tell a
        # truncated document from a complete one.
        details = "d" * (100 * 1024)
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 400
        exc.response.json.return_value = {"error": "Bad Request", "details": details}
        exc.response.text = '{"error": "Bad Request", "details": "' + details + '"}'
        payload = http_error_payload("msg", exc)
        assert payload["error"] == "Bad Request"
        assert payload["details"] == "d" * MAX_RESPONSE_BODY_CHARS + TRUNCATION_MARKER
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["status_code"] == 400
        assert payload["response_body_truncated"] is True
        assert "response_body" not in payload

    def test_http_error_payload_structured_typed_fields_are_exempt(self):
        # Issue #148 contract: callers branch on the typed fields, so ``code``,
        # ``error`` and ``status_code`` are never bounded - only free-form
        # upstream string fields are.
        big_error = "e" * (100 * 1024)
        big_code = "c" * (100 * 1024)
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 400
        exc.response.json.return_value = {
            "error": big_error,
            "code": big_code,
            "details": "d" * (100 * 1024),
        }
        payload = http_error_payload("msg", exc)
        assert payload["error"] == big_error
        assert payload["code"] == big_code
        assert payload["details"] == "d" * MAX_RESPONSE_BODY_CHARS + TRUNCATION_MARKER
        assert payload["response_body_truncated"] is True

    def test_http_error_payload_bounds_nested_structured_values(self):
        # Issue #297: nested containers are bounded too - a huge list or nested
        # object of free-form strings is cut just like a top-level field, and
        # the payload is flagged. Non-string scalars pass through unchanged.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 429
        exc.response.json.return_value = {
            "error": "rate limit exceeded",
            "retryAfter": 10**9,
            "context": ["x" * (100 * 1024)],
            "meta": {"reason": "y" * (100 * 1024), "ok": True},
        }
        payload = http_error_payload("msg", exc)
        assert payload["error"] == "rate limit exceeded"
        assert payload["retryAfter"] == 10**9
        assert payload["context"] == ["x" * MAX_RESPONSE_BODY_CHARS + TRUNCATION_MARKER]
        assert payload["meta"] == {"reason": "y" * MAX_RESPONSE_BODY_CHARS + TRUNCATION_MARKER, "ok": True}
        assert payload["response_body_truncated"] is True

    def test_http_error_payload_nested_small_document_is_complete(self):
        # Nested content within the cap is surfaced whole, with no flag.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 429
        exc.response.json.return_value = {
            "error": "rate limit exceeded",
            "context": ["quota exceeded", "retry in 10s"],
        }
        payload = http_error_payload("msg", exc)
        assert payload["context"] == ["quota exceeded", "retry in 10s"]
        assert "response_body_truncated" not in payload

    def test_http_error_payload_structured_small_document_is_complete(self):
        # A structured document within the cap is surfaced whole (issue #148)
        # and carries no truncation flag, so callers can tell it is complete.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 403
        exc.response.json.return_value = {
            "error": "Read access denied for profile: abc123",
            "code": ErrorCode.READ_ACCESS_DENIED,
            "profile_id": "abc123",
        }
        payload = http_error_payload("msg", exc)
        assert payload["error"] == "Read access denied for profile: abc123"
        assert payload["code"] == ErrorCode.READ_ACCESS_DENIED
        assert payload["profile_id"] == "abc123"
        assert payload["status_code"] == 403
        assert "response_body" not in payload
        assert "response_body_truncated" not in payload

    def test_http_error_payload_truncates_oversized_body(self):
        # A huge non-JSON body (HTML error page, captive portal) must not be
        # inlined into the tool result the LLM reads.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 502
        exc.response.json.side_effect = ValueError("not json")
        exc.response.text = "<html>" + ("x" * (1024 * 1024)) + "</html>"
        payload = http_error_payload("msg", exc)
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["response_body_truncated"] is True
        assert payload["response_body"].startswith("<html>")
        assert payload["response_body"].endswith("... [truncated]")
        assert len(payload["response_body"]) <= MAX_RESPONSE_BODY_CHARS + len("... [truncated]")

    def test_http_error_payload_json_without_error_field(self):
        # A JSON body that is not a structured error falls back to the generic shape.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 400
        exc.response.json.return_value = {"data": []}
        exc.response.text = ""
        payload = http_error_payload("fallback message", exc)
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["error"] == "fallback message"
        assert "response_body" not in payload


class TestValidationErrorContract:
    """Validation failures return the structured shape (not an exception)."""

    @pytest.mark.asyncio
    async def test_query_analytics_invalid_profile(self):
        result = await server.queryAnalytics("status", "abc/def")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_manage_lists_invalid_profile(self):
        result = await server.manageLists("allowlist", "get", "abc/def")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_manage_settings_invalid_profile(self):
        result = await server.manageSettings("get", "general", "abc/def")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_manage_rewrites_invalid_profile(self):
        result = await server.manageRewrites("list", "abc/def")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_manage_logs_invalid_profile(self):
        result = await server.manageLogs("get", "abc/def")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_query_analytics_unsupported_series_is_payload_not_exception(self):
        # Regression: this previously raised ValueError.
        result = await server.queryAnalytics("domains", "abc123", series=True)
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.UNSUPPORTED_PARAMETER

    @pytest.mark.asyncio
    async def test_query_analytics_missing_destination_type(self):
        result = await server.queryAnalytics("destinations", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.MISSING_REQUIRED_ARGUMENT

    @pytest.mark.asyncio
    async def test_unsupported_operations_across_tools(self):
        for coro in (
            _manage_settings_impl("nope", "general", "abc123"),
            _manage_rewrites_impl("nope", "abc123"),
            _manage_logs_impl("nope", "abc123"),
            _manage_profiles_impl("nope", profile_id="abc123"),
        ):
            result = await coro
            _assert_error_contract(result)
            assert result["code"] == ErrorCode.UNSUPPORTED_OPERATION


class TestAccessControlErrorContract:
    """ACL denials carry a typed code and preserve the denial reason."""

    @pytest.mark.asyncio
    async def test_read_denial_is_typed_with_reason(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "xyz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        result = await server.queryAnalytics("status", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.READ_ACCESS_DENIED
        assert result["status_code"] == 403
        # The denial reason must survive the typed AccessDeniedError (issue #178).
        assert "Read access denied for profile: abc123" in result["error"]

    @pytest.mark.asyncio
    async def test_plot_read_denial_is_typed_with_reason(self, monkeypatch):
        # The plot fetch must surface the denial payload the ACL layer returns
        # rather than reading it as an empty time series.
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "xyz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        result = await server.plotAnalytics("status", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.READ_ACCESS_DENIED
        assert result["status_code"] == 403
        assert "Read access denied for profile: abc123" in result["error"]

    @pytest.mark.asyncio
    async def test_write_denial_is_typed_with_reason(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "xyz999")
        result = await server.manageLists("allowlist", "add", "abc123", entry={"id": "x.com"})
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.WRITE_ACCESS_DENIED
        assert result["status_code"] == 403
        assert "Write access denied for profile: abc123" in result["error"]

    @pytest.mark.asyncio
    async def test_upstream_403_is_http_error_not_acl_denial(self, mock_api_client):
        """A genuine upstream 403 (raise_for_status) must NOT be mapped to an
        ACL denial: it keeps the existing http_error path (issue #178)."""
        mock_api_client.request.side_effect = _http_error(403)
        result = await server.manageSettings("get", "general", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 403


class TestHttpErrorContract:
    """HTTP/network failures are structured and distinguishable by status_code."""

    @pytest.mark.asyncio
    async def test_get_tool_http_error_distinguishable(self, mock_api_client):
        for status in (401, 403, 429, 500):
            mock_api_client.request.side_effect = _http_error(status)
            result = await server.manageSettings("get", "general", "abc123")
            _assert_error_contract(result)
            assert result["code"] == ErrorCode.HTTP_ERROR
            assert result["status_code"] == status

    @pytest.mark.asyncio
    async def test_query_analytics_http_error(self, mock_api_client):
        mock_api_client.request.side_effect = _http_error(429)
        result = await server.queryAnalytics("status", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 429

    @pytest.mark.asyncio
    async def test_manage_logs_download_http_error(self, mock_api_client):
        mock_api_client.stream = MagicMock(side_effect=_http_error(503))
        result = await server.manageLogs("download", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 503
        assert "response_body" in result

    @pytest.mark.asyncio
    async def test_network_error_without_status(self, mock_api_client):
        mock_api_client.request.side_effect = httpx.ConnectError("network down")
        result = await server.manageSettings("get", "general", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] is None
        assert "response_body" not in result

    @pytest.mark.asyncio
    async def test_unexpected_error_is_internal(self, mock_api_client):
        mock_api_client.request.side_effect = RuntimeError("boom")
        result = await server.manageSettings("get", "general", "abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INTERNAL_ERROR


class TestPlotAndDohErrorContract:
    """plotAnalytics and dohLookup also honor the contract."""

    @pytest.mark.asyncio
    async def test_plot_unsupported_metric(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_DEFAULT_PROFILE", raising=False)
        result = await _plot_analytics_series_impl("domains")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.UNSUPPORTED_METRIC

    @pytest.mark.asyncio
    async def test_plot_invalid_interval(self, monkeypatch):
        monkeypatch.delenv("NEXTDNS_DEFAULT_PROFILE", raising=False)
        result = await _plot_analytics_series_impl("status", interval=30)
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_plot_no_data(self, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        response = MagicMock()
        response.json.return_value = {"meta": {"series": {"times": []}}, "data": []}
        mock_api_client.request.return_value = response
        result = await _plot_analytics_series_impl("status")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.NO_DATA

    @pytest.mark.asyncio
    async def test_plot_http_error(self, mock_api_client, monkeypatch):
        monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "abc123")
        mock_api_client.request.side_effect = _http_error(500)
        result = await server.plotAnalytics("status")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_doh_unsupported_metric_style_codes(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "zzz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "zzz999")
        result = await server.dohLookup("example.com", "abc123", "A")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.READ_ACCESS_DENIED
