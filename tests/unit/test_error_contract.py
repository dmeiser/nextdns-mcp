"""Regression tests for the standardized structured error contract (issue #148).

Every tool failure must surface as a dict of the form ``{"error": <str>, "code":
<str>, ...}`` - a stable, machine-readable shape LLM callers can predict and
branch on. These tests assert the contract holds across tools and across failure
classes (validation, access control, unsupported input, and HTTP/network errors).

The vehicle is a generated tool: :func:`spec_call` builds the :class:`SpecApiTool`
the server registers for one ``operationId`` and returns what a caller would
receive. That is what makes these contract tests rather than per-tool tests - the
contract has to hold for a tool nobody hand-wrote, so it is asserted against the
generator's own path through the access-controlled client.

SPDX-License-Identifier: MIT
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import server, spec_tools
from nextdns_mcp.errors import (
    MAX_RESPONSE_BODY_CHARS,
    TRUNCATION_MARKER,
    ErrorCode,
    error_payload,
    http_error_payload,
    upstream_error_codes,
)
from nextdns_mcp.tools.plots import _plot_analytics_series_impl


@pytest.fixture
def mock_api_client(monkeypatch):
    """Replace the module-level api_client with a mock."""
    client = AsyncMock()
    monkeypatch.setattr(client_module, "api_client", client)
    return client


@pytest.fixture
def spec_call() -> Callable[..., Any]:
    """Run one generated tool and return its payload, as a caller sees it."""
    routes = {route.operation_id: route for route in spec_tools.spec_routes()}

    async def _call(operation_id: str, **arguments: Any) -> Any:
        result = await spec_tools.SpecApiTool(routes[operation_id]).run(arguments)
        return result.structured_content

    return _call


@pytest.fixture(autouse=True)
def open_profile_access(monkeypatch):
    """Allow all profile read/write access unless a test narrows it."""
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
        # The two codes a generated tool can produce before any request is sent.
        assert ErrorCode.INVALID_ENTRY_ID == "invalid_entry_id"
        assert ErrorCode.MISSING_REQUIRED_ARGUMENT == "missing_required_argument"

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

    def test_http_error_payload_bounds_containers_under_typed_error_field(self):
        # The #148 exemption covers a top-level ``error`` *string* message, not a
        # container: when the upstream document puts a huge blob inside
        # ``{"error": {...}}``, the exemption must not let the whole 1 MB
        # document into the tool result. Contents are bounded and flagged.
        exc = httpx.HTTPError("boom")
        exc.response = MagicMock()
        exc.response.status_code = 400
        exc.response.json.return_value = {
            "error": {"message": "z" * (100 * 1024), "code": "upstream_boom"},
        }
        payload = http_error_payload("msg", exc)
        assert payload["error"] == {
            "message": "z" * MAX_RESPONSE_BODY_CHARS + TRUNCATION_MARKER,
            "code": "upstream_boom",
        }
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
    """Validation failures return the structured shape (not an exception).

    On this surface the only validation a tool does locally is on the arguments it
    substitutes into the request path: those decide *which* endpoint is called, so a
    malformed one is refused here rather than sent upstream. Everything else is the
    API's answer, covered by the HTTP classes below.
    """

    @pytest.mark.asyncio
    async def test_profile_id_with_a_path_separator(self, spec_call):
        result = await spec_call("getAnalyticsStatus", profile_id="abc/def")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_profile_id_with_parent_traversal(self, spec_call):
        result = await spec_call("getDenylist", profile_id="../abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID

    @pytest.mark.asyncio
    async def test_entry_id_with_a_query_separator(self, spec_call, mock_api_client):
        """An entry id is one path segment; a ``?`` would change the request's query."""
        result = await spec_call("deleteRewrite", profile_id="abc123", entry_id="x?id=1")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INVALID_ENTRY_ID
        mock_api_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_blank_path_argument_names_the_endpoint(self, spec_call, mock_api_client):
        result = await spec_call("getLogs", profile_id="")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.MISSING_REQUIRED_ARGUMENT
        assert "GET /profiles/{profile_id}/logs" in result["error"]
        mock_api_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_entry_id_says_where_entry_ids_come_from(self, spec_call, mock_api_client):
        result = await spec_call("deleteRewrite", profile_id="abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.MISSING_REQUIRED_ARGUMENT
        assert "entry id" in result["hint"]
        mock_api_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_delegated_download_still_needs_a_profile(self, spec_call, mock_api_client):
        """``downloadLogs`` is not a plain request, and still refuses a blank id."""
        result = await spec_call("downloadLogs", profile_id="")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.MISSING_REQUIRED_ARGUMENT
        mock_api_client.stream.assert_not_called()


class TestAccessControlErrorContract:
    """ACL denials carry a typed code and preserve the denial reason."""

    @pytest.mark.asyncio
    async def test_read_denial_is_typed_with_reason(self, monkeypatch, spec_call):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "xyz999")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        result = await spec_call("getAnalyticsStatus", profile_id="abc123")
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
    async def test_write_denial_is_typed_with_reason(self, monkeypatch, spec_call):
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "xyz999")
        result = await spec_call("addToDenylist", profile_id="abc123", id="x.com")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.WRITE_ACCESS_DENIED
        assert result["status_code"] == 403
        assert "Write access denied for profile: abc123" in result["error"]

    @pytest.mark.asyncio
    async def test_upstream_403_is_http_error_not_acl_denial(self, mock_api_client, spec_call):
        """A genuine upstream 403 (raise_for_status) must NOT be mapped to an
        ACL denial: it keeps the existing http_error path (issue #178)."""
        mock_api_client.request.side_effect = _http_error(403)
        result = await spec_call("getPrivacySettings", profile_id="abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 403


class TestHttpErrorContract:
    """HTTP/network failures are structured and distinguishable by status_code."""

    @pytest.mark.asyncio
    async def test_get_tool_http_error_distinguishable(self, mock_api_client, spec_call):
        for status in (401, 403, 429, 500):
            mock_api_client.request.side_effect = _http_error(status)
            result = await spec_call("getPrivacySettings", profile_id="abc123")
            _assert_error_contract(result)
            assert result["code"] == ErrorCode.HTTP_ERROR
            assert result["status_code"] == status

    @pytest.mark.asyncio
    async def test_analytics_endpoint_http_error(self, mock_api_client, spec_call):
        mock_api_client.request.side_effect = _http_error(429)
        result = await spec_call("getAnalyticsStatus", profile_id="abc123", **{"from": "-1d"})
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 429

    @pytest.mark.asyncio
    async def test_logs_download_http_error(self, mock_api_client, spec_call):
        mock_api_client.stream = MagicMock(side_effect=_http_error(503))
        result = await spec_call("downloadLogs", profile_id="abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 503
        assert "response_body" in result

    @pytest.mark.asyncio
    async def test_network_error_without_status(self, mock_api_client, spec_call):
        mock_api_client.request.side_effect = httpx.ConnectError("network down")
        result = await spec_call("getPrivacySettings", profile_id="abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] is None
        assert "response_body" not in result

    @pytest.mark.asyncio
    async def test_unexpected_error_is_internal(self, mock_api_client, spec_call):
        mock_api_client.request.side_effect = RuntimeError("boom")
        result = await spec_call("getPrivacySettings", profile_id="abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.INTERNAL_ERROR


class TestEmptyResponseContract:
    """An endpoint that answers with nothing still answers in the contract's shape."""

    @pytest.mark.asyncio
    async def test_a_204_answers_success_without_a_body(self, mock_api_client, spec_call):
        """``DELETE`` returns 204 with no body, and there is no JSON to hand back.

        Inventing ``{}`` would leave a caller unable to tell a deleted entry from an
        empty list, so the transport names the outcome.
        """
        response = MagicMock()
        response.status_code = 204
        response.content = b""
        mock_api_client.request.return_value = response
        result = await spec_call("deleteRewrite", profile_id="abc123", entry_id="example.com")
        assert result == {"success": True}


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


class TestUpstreamErrorDocument:
    """The NextDNS plural ``errors`` document is surfaced as typed values.

    Upstream reports failures as ``{"errors": [{"code": ...}]}``, and does so
    behind either a 4xx or (for some endpoints) a 2xx status. Callers need the
    upstream code as a value to branch on, and a 2xx error document must not be
    handed back as if it were a data payload.
    """

    def test_extracts_codes_in_order(self):
        assert upstream_error_codes({"errors": [{"code": "duplicate"}]}) == ["duplicate"]
        assert upstream_error_codes({"errors": [{"code": "format"}, {"code": "extraneous"}]}) == [
            "format",
            "extraneous",
        ]

    def test_ignores_non_error_documents(self):
        assert upstream_error_codes({"data": []}) == []
        assert upstream_error_codes({"errors": []}) == []
        assert upstream_error_codes({"errors": "nope"}) == []
        assert upstream_error_codes(None) == []

    def test_http_error_payload_surfaces_codes(self):
        exc = httpx.HTTPError("dup")
        body = '{"errors":[{"code":"duplicate"}]}'
        exc.response = MagicMock(status_code=400, text=body)
        exc.response.json.return_value = {"errors": [{"code": "duplicate"}]}
        payload = http_error_payload("boom", exc)
        assert payload["code"] == ErrorCode.HTTP_ERROR
        assert payload["upstream_code"] == "duplicate"
        assert payload["upstream_codes"] == ["duplicate"]

    @pytest.mark.asyncio
    async def test_2xx_error_document_becomes_a_typed_error(self, mock_api_client, spec_call):
        """An upstream 2xx carrying an error document must not look like data."""
        response = MagicMock()
        response.status_code = 200
        response.content = b'{"errors":[{"code":"notFound"}]}'
        response.json.return_value = {"errors": [{"code": "notFound"}]}
        response.text = '{"errors":[{"code":"notFound"}]}'
        mock_api_client.request.return_value = response
        result = await spec_call("getProfile", profile_id="abc123")
        _assert_error_contract(result)
        assert result["code"] == ErrorCode.HTTP_ERROR
        assert result["status_code"] == 200
        assert result["upstream_code"] == "notFound"

    @pytest.mark.asyncio
    async def test_2xx_data_payload_still_passes_through(self, mock_api_client, spec_call):
        """Normalization is limited to error documents: real data is untouched."""
        response = MagicMock()
        response.status_code = 200
        response.content = b'{"data": [{"id": "x.com"}]}'
        response.json.return_value = {"data": [{"id": "x.com"}]}
        mock_api_client.request.return_value = response
        result = await spec_call("listProfiles")
        assert result == {"data": [{"id": "x.com"}]}
