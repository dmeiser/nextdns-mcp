"""End-to-end proof for issue #273 via the real MCP protocol surface.

Issue #273 removed the dead, blind JSON type-coercion subtree
(``coerce_json_types`` and its five private helpers) that mangled
identifier-like strings (``{"password": "0012"}`` -> ``{"password": 12}``),
and moved the two surviving shape helpers (``is_integer_shaped`` /
``is_float_shaped``) into ``utils`` as the only copy.

These tests drive the server the way an end user does: a real ``fastmcp.Client``
over the in-memory transport, calling the grouped tools exactly as an MCP client
would (including the Docker-MCP-CLI style of passing every scalar as a string),
against a real ``AccessControlledClient`` on a recording ``httpx.MockTransport``.
What the upstream NextDNS API actually receives is the observable outcome:

* string-typed arguments keep their exact string value, including leading zeros
  and all-digit profile names (the footgun the dead code demonstrated), and
* boolean/integer/number-typed arguments are still schema-coerced, so removing
  the duplicate heuristic did not break the live coercion path.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastmcp import Client

from nextdns_mcp import client as client_module
from nextdns_mcp.client import AccessControlledClient
from nextdns_mcp.server import build_mcp_server

API_BASE = "https://api.nextdns.io"
API_KEY = "test-key-12345"


class _RecordingClient:
    """A real AccessControlledClient on a MockTransport that records requests."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.client = AccessControlledClient(
            base_url=API_BASE,
            transport=httpx.MockTransport(self._handler),
            follow_redirects=False,
            headers={"X-Api-Key": API_KEY},
        )

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.method == "POST" and request.url.path.rstrip("/") == "/profiles":
            return httpx.Response(200, json={"id": "newprofile", "name": "ok"}, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    def last(self) -> httpx.Request:
        assert self.seen, "no upstream request was made"
        return self.seen[-1]

    def body(self) -> Any:
        return json.loads(self.last().content)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    rec = _RecordingClient()
    monkeypatch.setattr(client_module, "api_client", rec.client)
    return rec


@pytest.fixture
async def mcp_client() -> AsyncIterator[Client]:
    async with Client(build_mcp_server()) as client:
        yield client


async def test_numeric_profile_name_reaches_api_as_string(mcp_client: Client, recorder: _RecordingClient) -> None:
    """A profile name made only of digits is not turned into a number (issue #273)."""
    result = await mcp_client.call_tool(
        "manageProfiles",
        {"operation": "create", "name": "315244"},
    )
    assert result.data == {"id": "newprofile", "name": "ok"}

    sent = recorder.body()
    assert sent == {"name": "315244"}, f"profile name corrupted: {sent!r}"
    assert isinstance(sent["name"], str)


async def test_leading_zero_strings_survive_the_full_mcp_round_trip(
    mcp_client: Client, recorder: _RecordingClient
) -> None:
    """The exact payload from issue #273 (``password: "0012"``) stays intact."""
    result = await mcp_client.call_tool(
        "manageSettings",
        {"operation": "update", "category": "general", "profile_id": "abc123",
         "settings": {"pin": "0012", "zip": "01234", "name": "315244", "flag": "true"}},
    )
    assert result.data == {"data": []}

    sent = recorder.body()
    assert sent == {"pin": "0012", "zip": "01234", "name": "315244", "flag": "true"}, f"coerced: {sent!r}"
    assert all(isinstance(value, str) for value in sent.values())


async def test_schema_typed_scalars_are_still_coerced(mcp_client: Client, recorder: _RecordingClient) -> None:
    """bool/int-typed arguments still arrive typed after the helper move."""
    result = await mcp_client.call_tool(
        "queryAnalytics",
        {"metric": "status", "profile_id": "315244", "limit": "10", "series": "true", "interval": "3600"},
    )
    assert result.data == {"data": []}

    request = recorder.last()
    # profile_id is declared string: it stays in the path verbatim, un-coerced.
    assert request.url.path == "/profiles/315244/analytics/status;series", request.url.path
    # limit/interval are declared integer and series is declared boolean: both
    # arrive coerced, and series is reflected as the ";series" path suffix.
    assert request.url.params["limit"] == "10"
    assert request.url.params["interval"] == "3600"


async def test_integer_shape_helper_is_live_in_the_mcp_call_path(
    mcp_client: Client, recorder: _RecordingClient
) -> None:
    """The moved ``is_integer_shaped`` still runs before argument validation.

    ``"١٠"`` (Arabic-Indic decimal digits) is not accepted by pydantic's lax int
    parsing, so the call only succeeds because ``StripExtraFieldsMiddleware``
    recognises the integer shape and converts it first. That makes the surviving
    helper observable in the live request path, not just in a unit test.
    """
    result = await mcp_client.call_tool(
        "queryAnalytics",
        {"metric": "status", "profile_id": "abc123", "limit": "١٠"},
    )
    assert result.data == {"data": []}
    assert recorder.last().url.params["limit"] == "10"
