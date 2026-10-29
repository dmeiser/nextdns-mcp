"""The upstream diagnosis belongs in the message the caller reads first.

NextDNS already says what it rejected - "`partials` must be equal to one of the
allowed values", with ``source.parameter`` set - and ``http_error_payload``
already surfaces it, but as ``response_body``: a JSON string beside the typed
codes. The ``error`` string, which is what a model reads, carried only the
transport's boilerplate and a link to the MDN status page.

SPDX-License-Identifier: MIT
"""

import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastmcp import Client

from nextdns_mcp.errors import http_error_payload, upstream_error_details
from nextdns_mcp.server import build_mcp_server


def _response(status_code: int, document: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = document
    response.text = json.dumps(document)
    return response


class TestUpstreamDiagnosis:
    def test_the_detail_and_its_parameter_are_extracted(self):
        document = {
            "errors": [
                {
                    "code": "enum",
                    "source": {"parameter": "partials"},
                    "detail": "`partials` must be equal to one of the allowed values.",
                }
            ]
        }
        assert upstream_error_details(document) == [
            "`partials` must be equal to one of the allowed values. (parameter: partials)"
        ]

    @pytest.mark.parametrize("body", [None, [], {"errors": "no"}, {"errors": [{"code": "x"}]}, {"error": "own"}])
    def test_a_body_without_a_useful_document_yields_nothing(self, body):
        assert upstream_error_details(body) == []

    def test_the_message_the_caller_reads_carries_it(self):
        exc = httpx.HTTPError("boom")
        exc.response = _response(
            400,
            {
                "errors": [
                    {
                        "code": "required",
                        "source": {"parameter": "from"},
                        "detail": "`from` must be number.",
                    }
                ]
            },
        )
        payload = http_error_payload("HTTP error in GET /x: Client error '400 Bad Request'", exc)
        assert payload["error"].endswith("upstream: `from` must be number. (parameter: from)")
        assert payload["upstream_code"] == "required"

    def test_a_failure_without_an_upstream_document_is_left_as_it_was(self):
        exc = httpx.HTTPError("boom")
        exc.response = _response(500, {"not": "an error document"})
        payload = http_error_payload("HTTP error in GET /x", exc)
        assert payload["error"] == "HTTP error in GET /x"

    def test_only_the_first_few_details_are_inlined(self):
        exc = httpx.HTTPError("boom")
        exc.response = _response(
            400,
            {"errors": [{"code": "type", "detail": f"detail {index}"} for index in range(6)]},
        )
        payload = http_error_payload("msg", exc)
        assert "detail 0" in payload["error"]
        assert "detail 3" not in payload["error"]


class TestTheTwoLogToolsPointAtEachOther:
    """`settings.logs.read` failed because the model read the query log when the
    task was the logging setting. Nothing said the two are different."""

    @pytest.fixture(autouse=True)
    def _server_env(self, monkeypatch):
        monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
        monkeypatch.setenv("NEXTDNS_API_KEY", "sk-test")
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")

    @pytest.mark.asyncio
    async def test_the_query_log_tool_names_the_setting_that_controls_it(self):
        async with Client(build_mcp_server()) as client:
            described = {tool.name: (tool.description or "") for tool in await client.list_tools()}
        logs = " ".join(described["manageLogs"].split())
        assert "manageSettings" in logs
        assert 'category="logs"' in logs

    @pytest.mark.asyncio
    async def test_the_settings_tool_names_the_log_entries_it_does_not_read(self):
        async with Client(build_mcp_server()) as client:
            described = {tool.name: (tool.description or "") for tool in await client.list_tools()}
        settings = " ".join(described["manageSettings"].split())
        assert "manageLogs" in settings


class TestTheMessageFitsWhatIsRecorded:
    """The call log keeps 300 characters of the error. httpx's pointer to the MDN
    status page was what survived them; the diagnosis was what got cut."""

    def test_the_transport_boilerplate_is_gone(self):
        exc = httpx.HTTPError("boom")
        exc.response = _response(404, {"errors": [{"code": "notFound", "detail": "Entry not found."}]})
        payload = http_error_payload(
            "HTTP error in PATCH /profiles/abc/allowlist/x.example.com: "
            "Client error '404 Not Found' for url 'https://api.nextdns.io/profiles/abc/allowlist/x.example.com' "
            "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404",
            exc,
        )
        assert "developer.mozilla.org" not in payload["error"]
        assert "upstream: Entry not found." in payload["error"]
        assert len(payload["error"]) < 300, "the call log keeps this many characters"
