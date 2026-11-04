"""End-to-end proof for issue #273 via the real MCP protocol surface.

Issue #273 removed the dead, blind JSON type-coercion subtree
(``coerce_json_types`` and its five private helpers) that mangled
identifier-like strings (``{"password": "0012"}`` -> ``{"password": 12}``),
and moved the two surviving shape helpers (``is_integer_shaped`` /
``is_float_shaped``) into ``utils`` as the only copy.

These tests drive the server the way an end user does: a real ``fastmcp.Client``
over the in-memory transport, calling the spec-generated tools exactly as an MCP
client would (including the Docker-MCP-CLI style of passing every scalar as a
string), against a real ``AccessControlledClient`` on a recording
``httpx.MockTransport``. What the upstream NextDNS API actually receives is the
observable outcome:

* string-typed arguments keep their exact string value, including leading zeros
  and all-digit profile names (the footgun the dead code demonstrated), and
* boolean/integer-typed arguments are still schema-coerced, so removing
  the duplicate heuristic did not break the live coercion path.

The coercion is read back out of the JSON request body rather than the query
string: an integer sent as a query parameter renders identically whether or not
it was coerced, while ``{"retention": 2592000}`` and ``{"retention":
"2592000"}`` do not.
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
    result = await mcp_client.call_tool("createProfile", {"name": "315244"}, raise_on_error=False)
    assert result.data == {"id": "newprofile", "name": "ok"}

    sent = recorder.body()
    assert sent == {"name": "315244"}, f"profile name corrupted: {sent!r}"
    assert isinstance(sent["name"], str)


async def test_leading_zero_strings_survive_the_full_mcp_round_trip(
    mcp_client: Client, recorder: _RecordingClient
) -> None:
    """The issue #273 hazard — ``"0012"`` silently cast to ``12`` — stays impossible.

    Nested container values stay opaque to *types*: nothing in the pipeline
    rewrites them, so a leading zero inside one is sent exactly as written.
    The published schema now constrains the *structure* of nested bodies
    (declared fields' types, required ids), so the opaque shape here is a
    schema-valid one: declared fields arrive typed, undeclared or string-typed
    ones survive byte-for-byte.
    """
    result = await mcp_client.call_tool(
        "updateSettings",
        {
            "profile_id": "abc123",
            "logs": {"enabled": True, "retention": 2592000, "name": "0012"},
            "rewrites": [{"name": "315244", "content": "192.0.2.1"}],
            "web3": "true",
        },
    )
    assert result.data == {"data": []}

    sent = recorder.body()
    assert sent == {
        "logs": {"enabled": True, "retention": 2592000, "name": "0012"},
        "rewrites": [{"name": "315244", "content": "192.0.2.1"}],
        "web3": True,
    }, f"coerced: {sent!r}"
    assert sent["web3"] is True
    assert sent["logs"]["name"] == "0012"
    assert sent["rewrites"][0]["name"] == "315244"


async def test_schema_typed_scalars_are_still_coerced(mcp_client: Client, recorder: _RecordingClient) -> None:
    """bool/int-typed arguments still arrive typed after the helper move.

    ``retention`` is declared integer and ``enabled`` boolean, so both reach the
    API as the type the endpoint documents; ``profile_id`` is declared string,
    so a name that is all digits stays a name in the path, un-coerced.
    """
    result = await mcp_client.call_tool(
        "updateLogsSettings",
        {"profile_id": "315244", "enabled": "true", "retention": "2592000", "location": "DE"},
    )
    assert result.data == {"data": []}

    request = recorder.last()
    assert request.url.path == "/profiles/315244/settings/logs", request.url.path

    sent = recorder.body()
    assert sent == {"enabled": True, "retention": 2592000, "location": "DE"}, f"coerced: {sent!r}"
    assert sent["enabled"] is True
    # The integer must arrive as a number, not as the boolean its truthiness allows.
    assert isinstance(sent["retention"], int) and not isinstance(sent["retention"], bool)
    assert isinstance(sent["location"], str)


async def test_integer_shape_helper_is_live_in_the_mcp_call_path(
    mcp_client: Client, recorder: _RecordingClient
) -> None:
    """The moved ``is_integer_shaped`` still runs before the arguments are sent.

    ``"٢٥٩٢٠٠٠"`` (Arabic-Indic decimal digits for 2592000) is not accepted by
    pydantic's lax int parsing, so the value only reaches the API as a number
    because ``StripExtraFieldsMiddleware`` recognises the integer shape and
    converts it first. That makes the surviving helper observable in the live
    request path, not just in a unit test.

    The coerced value must also survive the tool's own schema gate:
    ``retention`` is an enum of allowed seconds, so the digit string stands
    for a valid member (2592000 - 30 days), not an arbitrary integer.
    """
    result = await mcp_client.call_tool(
        "updateLogsSettings", {"profile_id": "abc123", "retention": "٢٥٩٢٠٠٠"}, raise_on_error=False
    )
    assert result.data == {"data": []}

    sent = recorder.body()
    assert sent == {"retention": 2592000}, f"the shape helper did not run: {sent!r}"
    assert isinstance(sent["retention"], int)
