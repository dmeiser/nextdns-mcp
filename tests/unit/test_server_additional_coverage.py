from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from fastmcp.exceptions import ToolError

from nextdns_mcp import client as client_module
from nextdns_mcp import config, relaxation, server
from nextdns_mcp import utils as utils_module
from nextdns_mcp.tools import doh as doh_module


class _FakeServer(SimpleNamespace):
    """Server stand-in weakref-able, unlike plain ``types.SimpleNamespace``.

    StripExtraFieldsMiddleware keys its caches on the server object via a
    WeakKeyDictionary; fixture servers must support weak references the way
    real FastMCP servers do.
    """

    __slots__ = ("__weakref__",)
    __hash__ = object.__hash__  # identity, like the real FastMCP server key
    __eq__ = object.__eq__


@pytest.fixture(autouse=True)
def allow_doh_read_access(monkeypatch):
    """Allow all DoH lookups by opening the readable profile gate via the env."""
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


def test_identifier_like_strings_are_never_type_coerced():
    """The blind JSON type-coercion subtree is gone (issue #273); pinned as the
    behaviour, not as the absence of its helpers: an identifier-shaped string
    with leading zeros must survive the schema-aware path unchanged."""
    assert utils_module.is_integer_shaped("0012") is True


@pytest.mark.asyncio
async def test_middleware_leaves_leading_zero_identifiers_alone():
    """`{"password": "0012"}` was the live corruption (string -> int 12).

    Through StripExtraFieldsMiddleware the value must come back still a string.
    """
    mw = relaxation.StripExtraFieldsMiddleware()

    class StringDummyTool:
        parameters: ClassVar[dict[str, object]] = {"properties": {"password": {"type": "string"}}}

    class StringToolManager:
        async def get_tool(self, name):
            return StringDummyTool()

    context = DummyContext("tool", {})
    context.fastmcp_context.fastmcp = _FakeServer(get_tool=StringToolManager().get_tool, list_tools=_no_tools)
    context.message.arguments = {"password": "0012"}
    result = await mw.on_call_tool(context, _dummy_call_next)
    assert result == {"password": "0012"}
    assert isinstance(result["password"], str)


def test_shape_helpers():
    # integer shape
    assert utils_module.is_integer_shaped("123") is True
    assert utils_module.is_integer_shaped("-5") is True
    assert utils_module.is_integer_shaped("1.2") is False
    assert utils_module.is_integer_shaped("²") is False
    assert utils_module.is_integer_shaped("-²") is False

    # float shape
    assert utils_module.is_float_shaped("1.23") is True
    assert utils_module.is_float_shaped("-1.23") is True
    assert utils_module.is_float_shaped("3") is True
    assert utils_module.is_float_shaped("notfloat") is False
    assert utils_module.is_float_shaped("²") is False
    assert utils_module.is_float_shaped("1.²") is False
    assert utils_module.is_float_shaped("") is False


class DummyTool:
    parameters: ClassVar[dict[str, object]] = {"properties": {"keep": {"type": "boolean"}}}


class DummyToolManager:
    def __init__(self, raise_exc=False):
        self.raise_exc = raise_exc

    async def get_tool(self, name):
        if self.raise_exc:
            raise RuntimeError("no tool")
        return DummyTool()


async def _no_tools(**_: object) -> list:
    """The catalog a minimal dummy server lists: nothing, so the owner hint stays generic."""
    return []


class DummyFastMCPContext:
    def __init__(self, raise_exc=False):
        # A real FastMCP server answers get_tool and list_tools; the owner hint
        # builds its index from list_tools, so the dummy must carry both.
        self.fastmcp = _FakeServer(get_tool=DummyToolManager(raise_exc=raise_exc).get_tool, list_tools=_no_tools)


class DummyMessage:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class DummyContext:
    def __init__(self, name, arguments, raise_exc=False):
        self.message = DummyMessage(name, arguments)
        self.fastmcp_context = DummyFastMCPContext(raise_exc=raise_exc)


async def _dummy_call_next(context):
    # return the updated arguments for inspection
    return context.message.arguments


@pytest.mark.asyncio
async def test_strip_extra_fields_middleware_basic_and_exception():
    mw = relaxation.StripExtraFieldsMiddleware()

    # Normal operation: known property 'keep' retained, unknown removed, bool coerced
    context = DummyContext("tool", {"keep": "true", "drop": "x"})
    result = await mw.on_call_tool(context, _dummy_call_next)
    assert result == {"keep": True}

    # String-typed identifier-like values must not be coerced
    class StringDummyTool:
        parameters: ClassVar[dict[str, object]] = {"properties": {"profile_id": {"type": "string"}}}

    class StringToolManager:
        async def get_tool(self, name):
            return StringDummyTool()

    string_context = DummyContext("tool", {})
    string_context.fastmcp_context.fastmcp = _FakeServer(get_tool=StringToolManager().get_tool, list_tools=_no_tools)
    string_context.message.arguments = {"profile_id": "315244"}
    result = await mw.on_call_tool(string_context, _dummy_call_next)
    assert result == {"profile_id": "315244"}

    # Exception handling in get_tool: fail closed with ToolError, args untouched
    context_exc = DummyContext("tool", {"keep": "true"}, raise_exc=True)
    with pytest.raises(ToolError, match="failed closed"):
        await mw.on_call_tool(context_exc, _dummy_call_next)


def test_access_denied_error_carries_reason():
    err = client_module.AccessDeniedError("denied", code="write_access_denied", profile_id="abc")
    assert str(err) == "denied"
    assert err.code == "write_access_denied"
    assert err.profile_id == "abc"


def test_access_control_client_checks():
    client = client_module.AccessControlledClient()

    # Deny write, read-only true
    read_only_denies = config.ProfileAccessControl(read_only=True, readable=frozenset(), writable=None)
    with pytest.raises(client_module.AccessDeniedError) as exc_info:
        client._check_write_access("abc", "PUT", "/profiles/abc", read_only_denies)
    assert exc_info.value.code == "write_access_denied"
    assert exc_info.value.profile_id == "abc"
    assert "read-only" in str(exc_info.value).lower()

    # Deny read
    read_denies = config.ProfileAccessControl(read_only=False, readable=None, writable=frozenset({"abc123"}))
    with pytest.raises(client_module.AccessDeniedError) as exc_info2:
        client._check_read_access("abc", "GET", "/profiles/abc", read_denies)
    assert exc_info2.value.code == "read_access_denied"
    assert exc_info2.value.profile_id == "abc"


@pytest.mark.asyncio
async def test_request_body_passthrough_and_access_denied(monkeypatch):
    client = client_module.AccessControlledClient()

    # Test request returns early when access denied
    monkeypatch.setattr(client_module, "extract_profile_id_from_url", lambda url: "abc123")
    # allow reads for this test
    allow_read = config.ProfileAccessControl(read_only=False, readable=frozenset(), writable=frozenset())
    monkeypatch.setattr(client_module, "load_profile_access_control", lambda: allow_read)

    async def fake_super_request(self, method, url, **kwargs):
        return httpx.Response(200, json={"ok": True})

    # Patch the parent httpx.AsyncClient.request
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_super_request)

    r = await client.request("GET", "/profiles/abc123/something")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_execute_doh_and_doh_impl(monkeypatch, mock_doh_response, mock_profiles_response):
    class DummyResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, doh_url, params=None, headers=None):
            return DummyResponse({**mock_doh_response})

    # Install the dummy as the DoH owner's client (issues #149, #642) so
    # doh_lookup never hits the network.
    monkeypatch.setattr(doh_module.DohClientOwner, "get", lambda self: DummyClient())
    with doh_module.doh_client_owned(doh_module.DohClientOwner()):
        # doh_lookup success
        res = await doh_module.doh_lookup(
            "https://dns.nextdns.io/abc123/dns-query",
            "google.com",
            "A",
            "abc123",
            config.load_profile_access_control(),
        )
        assert "_metadata" in res

        # _dohLookup_impl: no default profile
        monkeypatch.setattr(utils_module, "get_default_profile", lambda: None)
        r = await doh_module._dohLookup_impl("example.com")
        assert "error" in r and "No profile_id" in r["error"]

        # invalid record type
        monkeypatch.setattr(utils_module, "get_default_profile", lambda: "abc123")
        r2 = await doh_module._dohLookup_impl("example.com", record_type="INVALID")
        assert "error" in r2 and "Invalid record type" in r2["error"]

        # success path uses doh_lookup
        async def fake_exec(doh_url, domain, record_type, profile, access):
            return {"ok": True}

        monkeypatch.setattr(doh_module, "doh_lookup", fake_exec)
        r3 = await doh_module._dohLookup_impl("example.com")
        assert r3 == {"ok": True}


def test_get_mcp_run_options_http(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "9999")
    opts = server.get_mcp_run_options()
    assert opts["transport"] == "http"
    assert opts["host"] == "127.0.0.1"
    assert opts["port"] == 9999


@pytest.mark.asyncio
async def test_fixture_values_drive_config_precedence_and_doh_gate(
    monkeypatch,
    mock_api_key,
    mock_profile_id,
    temp_api_key_file,
    mock_nextdns_base_url,
    mock_doh_response,
    mock_profiles_response,
):
    """Every fixture here must change what the application does: the env key
    wins over the Docker-secret file key (both sources carry distinct values,
    so the assertion can tell which one produced the result), the file key is
    the fallback, the read gate stops a denied lookup before any request is
    sent, the fixture's profile ids are honored by the read gate, and a
    fixture answer with a failing Status maps to the right DNS status
    description."""

    # Env key takes precedence over the file key — distinct values, so the
    # assertion below can tell which source produced the result.
    env_key = "env-wins-key-01234"
    monkeypatch.setenv("NEXTDNS_API_KEY", env_key)
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(temp_api_key_file))
    assert config.get_api_key() == env_key

    # The file key is the fallback once the env key is gone.
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    assert config.get_api_key() == mock_api_key

    # A denied profile must fail closed without sending a request.
    class ExplodingClient:
        async def get(self, *args, **kwargs):
            raise AssertionError("doh_lookup sent a request for a denied profile")

    # The persistent DoH client lives behind an explicit owner (issues #149,
    # #642): the exploding stand-in is adopted into a published owner.
    monkeypatch.setattr(doh_module.DohClientOwner, "get", lambda self: ExplodingClient())
    deny = config.ProfileAccessControl(read_only=False, readable=None, writable=frozenset())
    with doh_module.doh_client_owned(doh_module.DohClientOwner()):
        res = await doh_module.doh_lookup(
            f"{mock_nextdns_base_url}/{mock_profile_id}/dns-query", "google.com", "A", mock_profile_id, deny
        )
    assert res["code"] == "read_access_denied"
    assert res["profile_id"] == mock_profile_id
    assert res["status_code"] == 403

    # A fixture answer with Status 3 (NXDOMAIN) must surface through the metadata mapping.
    deny_all_status = {**mock_doh_response, "Status": 3}
    metadata = doh_module._build_doh_metadata(
        mock_profile_id, "google.com", "A", mock_nextdns_base_url, deny_all_status["Status"]
    )
    assert metadata["status_description"] == "NXDOMAIN - Non-existent domain"

    # The fixture's listed profile ids must be honored by the read gate.
    listed_ids = {p["id"] for p in mock_profiles_response["data"]}
    allow_listed = config.ProfileAccessControl(read_only=False, readable=frozenset(listed_ids), writable=None)
    for listed_id in listed_ids:
        assert allow_listed.can_read(listed_id) is True
    assert allow_listed.can_read("not-a-listed-profile") is False


def test_coerce_value_schema_aware_and_collections(monkeypatch):
    mw = relaxation.StripExtraFieldsMiddleware()

    # Dict and list values are recursed without schema context, so strings stay strings
    assert mw._coerce_value({"a": "true", "b": "2"}) == {"a": "true", "b": "2"}
    assert mw._coerce_value(["1", "2.2"]) == ["1", "2.2"]

    # With an array-of-numbers schema, list items are coerced
    assert mw._coerce_value(["1", "2.2"], {"type": "array", "items": {"type": "number"}}) == [1, 2.2]

    # Simulate float() raising ValueError to hit the except branch
    import builtins

    real_float = builtins.float

    def bad_float(x):
        raise ValueError("boom")

    monkeypatch.setattr(builtins, "float", bad_float)
    try:
        # '1.23' matches the digit check and would attempt float()
        assert mw._coerce_value("1.23", {"type": "number"}) == "1.23"
    finally:
        monkeypatch.setattr(builtins, "float", real_float)

    # Boolean false and negative integer cases with matching schemas
    assert mw._coerce_value("false", {"type": "boolean"}) is False
    assert mw._coerce_value("-5", {"type": "integer"}) == -5

    # Identifier-like strings are not coerced when schema expects a string
    assert mw._coerce_value("315244", {"type": "string"}) == "315244"

    # Non-string non-dict/list returns unchanged
    assert mw._coerce_value(10) == 10
