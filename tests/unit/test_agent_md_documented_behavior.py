"""Behavior tests for the callable claims ``AGENT.md`` makes to agents (issue #274).

``AGENT.md`` is an instruction document, so asserting that it *contains* a
sentence proves nothing: the failure mode of issue #274 was documentation that
described an API which does not exist, and a substring check cannot tell a
callable example from a plausible-looking one. These tests therefore execute the
documented contract instead of reading it:

* the ``manageLists(...)`` example an agent is told to copy is invoked through
  the real MCP protocol against the real server, and the assertion is the HTTP
  request that actually reaches the transport;
* the write-scoping rule is checked as a matrix of env-var configurations
  against the real access-controlled client, and the assertion is whether a
  write is refused before any I/O or allowed through.

``AGENT.md`` is read in exactly one place -- to obtain the argument set the
document tells agents to use, so the test cannot silently drift away from the
document -- and nothing is asserted about its prose.
"""

import ast
import inspect
import json
import re
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from nextdns_mcp import client as client_module
from nextdns_mcp.client import AccessControlledClient
from nextdns_mcp.server import build_mcp_server
from nextdns_mcp.tools.lists import manageLists

API_BASE = "https://api.nextdns.io"
API_KEY = "test-key-12345"
# A well-formed profile id that is named in no configuration: stands in for a
# production profile, i.e. one an operator never intended to write to.
UNLISTED_PROFILE = "9f3c1a"


class _RecordingClient:
    """A real ``AccessControlledClient`` on a mock transport that records requests.

    The grouped tools reach the API through this client, so the real URL
    normalisation, destination allow-list, and profile ACL all run, and the
    recorded requests are what an operator's NextDNS API would really receive.
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.client = AccessControlledClient(
            base_url=API_BASE,
            transport=httpx.MockTransport(self._handle),
            follow_redirects=False,
            headers={"X-Api-Key": API_KEY},
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(204, request=request)

    async def close(self) -> None:
        await self.client.aclose()


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    """Wire the recording client in as the API client the tools use."""
    live = _RecordingClient()
    # The key must exist before the module attribute is replaced: reading
    # ``client.api_client`` for the first time would build the real singleton.
    monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)
    monkeypatch.setattr(client_module, "api_client", live.client)
    return live


def _documented_manage_lists_kwargs() -> dict[str, object]:
    """Return the argument set of the ``manageLists(...)`` example in ``AGENT.md``.

    The example is parsed as a Python expression so the values come from the
    document as an agent would copy them, not from a restatement in the test.
    """
    agent_md = (Path(__file__).resolve().parents[2] / "AGENT.md").read_text(encoding="utf-8")
    match = re.search(r"`(manageLists\([^`]*?\))`", agent_md, re.DOTALL)
    assert match is not None, "AGENT.md must document the manageLists replacement call"
    call = ast.parse(match.group(1), mode="eval").body
    assert isinstance(call, ast.Call), "the documented example must be a call"
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}


def test_documented_manage_lists_example_is_invocable() -> None:
    """The documented example names the real tool's real parameters.

    An agent copying the snippet gets a call that binds: omitting a required
    parameter would raise ``TypeError`` here rather than reach the API.
    """
    kwargs = _documented_manage_lists_kwargs()

    inspect.signature(manageLists).bind(**kwargs)


@pytest.mark.asyncio
async def test_documented_manage_lists_example_replaces_the_list_upstream(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: _RecordingClient,
) -> None:
    """Calling the documented example over MCP performs the real bulk PUT.

    This is the end-user surface: an MCP client, the tool as registered on the
    server, and the request the NextDNS API receives. Write access is granted
    the way ``AGENT.md`` tells an operator to grant it.
    """
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    kwargs = _documented_manage_lists_kwargs()

    async with Client(build_mcp_server()) as mcp_client:
        result = await mcp_client.call_tool("manageLists", kwargs)

    assert not result.is_error
    assert result.data == {"success": True}

    assert len(recording_client.seen) == 1
    request = recording_client.seen[0]
    assert request.method == "PUT"
    assert str(request.url) == f"{API_BASE}/profiles/abc123/privacy/blocklists"
    assert json.loads(request.read()) == [{"id": "nextdns-recommended"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "expect_allowed"),
    [
        pytest.param({}, False, id="unset-denies-writes"),
        pytest.param({"NEXTDNS_WRITABLE_PROFILES": "ALL"}, True, id="all-grants-every-profile"),
        pytest.param(
            {"NEXTDNS_WRITABLE_PROFILES": "ALL", "NEXTDNS_READ_ONLY": "true"},
            False,
            id="read-only-overrides-all",
        ),
        pytest.param({"NEXTDNS_WRITABLE_PROFILES": "abc123"}, False, id="named-list-scopes-writes"),
    ],
)
async def test_documented_write_scoping(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: _RecordingClient,
    env: dict[str, str],
    expect_allowed: bool,
) -> None:
    """The documented write-scoping rule matches what the server enforces.

    ``AGENT.md`` and ``docs/safety.md`` tell operators that an unset
    ``NEXTDNS_WRITABLE_PROFILES`` denies writes, that ``ALL`` grants writes to
    every profile including production ones, and that ``NEXTDNS_READ_ONLY``
    denies every write. The tool is exercised against a profile named in no
    configuration, so a denial must happen before any request is sent.
    """
    for key in ("NEXTDNS_WRITABLE_PROFILES", "NEXTDNS_READABLE_PROFILES", "NEXTDNS_READ_ONLY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result = await manageLists(
        list_type="denylist",
        operation="replace",
        profile_id=UNLISTED_PROFILE,
        entries=[{"id": "evil.example"}],
    )

    if expect_allowed:
        assert result == {"success": True}
        assert [r.method for r in recording_client.seen] == ["PUT"]
    else:
        assert result["code"] == "write_access_denied"
        assert result["profile_id"] == UNLISTED_PROFILE
        assert recording_client.seen == []
