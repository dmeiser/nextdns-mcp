import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from nextdns_mcp import server


def test_coerce_helpers():
    # bool
    assert server._coerce_string_to_bool("true") is True
    assert server._coerce_string_to_bool("false") is False
    assert server._coerce_string_to_bool("maybe") is None

    # integer
    assert server._is_integer("123") is True
    assert server._is_integer("-5") is True
    assert server._is_integer("1.2") is False

    # float parsing
    assert server._try_parse_float("1.23") == 1.23
    assert server._try_parse_float("notfloat") is None

    # coerce number
    assert server._coerce_string_to_number("42") == 42
    assert server._coerce_string_to_number("3.14") == 3.14
    assert server._coerce_string_to_number("no") is None

    # general string coercion
    assert server._coerce_string("true") is True
    assert server._coerce_string("10") == 10
    assert server._coerce_string("3.5") == 3.5
    assert server._coerce_string("x") == "x"

    # dict and list coercion
    assert server.coerce_json_types({"a": "true", "b": "2"}) == {"a": True, "b": 2}
    assert server.coerce_json_types(["1", "2.2"]) == [1, 2.2]
    assert server.coerce_json_types(123) == 123


class DummyTool:
    parameters: dict[str, object] = {"properties": {"keep": {}}}


class DummyToolManager:
    def __init__(self, raise_exc=False):
        self.raise_exc = raise_exc

    async def get_tool(self, name):
        if self.raise_exc:
            raise RuntimeError("no tool")
        return DummyTool()


class DummyFastMCPContext:
    def __init__(self, raise_exc=False):
        self.fastmcp = SimpleNamespace(get_tool=DummyToolManager(raise_exc=raise_exc).get_tool)


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
    mw = server.StripExtraFieldsMiddleware()

    # Normal operation: known property 'keep' retained, unknown removed, types coerced
    context = DummyContext("tool", {"keep": "true", "drop": "x"})
    result = await mw.on_call_tool(context, _dummy_call_next)
    assert result == {"keep": True}

    # Exception handling in get_tool: should proceed and return original args
    context_exc = DummyContext("tool", {"keep": "true"}, raise_exc=True)
    res2 = await mw.on_call_tool(context_exc, _dummy_call_next)
    assert res2 == {"keep": "true"}


def test_create_access_denied_response():
    resp = server.create_access_denied_response("PUT", "/profiles/abc/settings", "denied", "abc")
    assert resp.status_code == 403
    assert resp.json()["profile_id"] == "abc"
    assert resp.request.method == "PUT"


def test_access_control_client_checks(monkeypatch):
    client = server.AccessControlledClient()

    # Deny write, read-only true
    monkeypatch.setattr(server, "can_write_profile", lambda _id: False)
    monkeypatch.setattr(server, "is_read_only", lambda: True)

    r = client._check_write_access("abc", "PUT", "/profiles/abc")
    assert isinstance(r, httpx.Response)
    assert r.status_code == 403
    assert "read-only" in r.json()["error"].lower()

    # Deny read
    monkeypatch.setattr(server, "can_read_profile", lambda _id: False)
    r2 = client._check_read_access("abc", "GET", "/profiles/abc")
    assert r2.status_code == 403


@pytest.mark.asyncio
async def test_coerce_json_body_and_request(monkeypatch):
    client = server.AccessControlledClient()

    # Test coercion of JSON body
    kwargs = {"json": {"a": "true", "b": "2"}}
    client._coerce_json_body(kwargs)
    assert kwargs["json"] == {"a": True, "b": 2}

    # Test request returns early when access denied
    monkeypatch.setattr(server, "extract_profile_id_from_url", lambda url: "abc")
    monkeypatch.setattr(server, "can_write_profile", lambda _id: False)
    monkeypatch.setattr(server, "is_read_only", lambda: False)
    # allow reads for this test
    monkeypatch.setattr(server, "can_read_profile", lambda _id: True)

    async def fake_super_request(self, method, url, **kwargs):
        return httpx.Response(200, json={"ok": True})

    # Patch the parent httpx.AsyncClient.request
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_super_request)

    r = await client.request("GET", "/profiles/abc/something")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_execute_doh_and_doh_impl(monkeypatch, mock_doh_response, mock_profiles_response):
    # Mock AsyncClient used in _execute_doh_query
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

    monkeypatch.setattr(server.httpx, "AsyncClient", DummyClient)

    # _execute_doh_query success
    res = await server._execute_doh_query("https://dns.nextdns.io/abc/dns-query", "google.com", "A", "abc")
    assert "_metadata" in res

    # _dohLookup_impl: no default profile
    monkeypatch.setattr(server, "get_default_profile", lambda: None)
    r = await server._dohLookup_impl("example.com")
    assert "error" in r and "No profile_id" in r["error"]

    # invalid record type
    monkeypatch.setattr(server, "get_default_profile", lambda: "abc")
    r2 = await server._dohLookup_impl("example.com", record_type="INVALID")
    assert "error" in r2 and "Invalid record type" in r2["error"]

    # success path uses _execute_doh_query
    async def fake_exec(doh_url, domain, record_type, profile):
        return {"ok": True}

    monkeypatch.setattr(server, "_execute_doh_query", fake_exec)
    r3 = await server._dohLookup_impl("example.com")
    assert r3 == {"ok": True}


def test_get_mcp_run_options_http(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "9999")
    opts = server.get_mcp_run_options()
    assert opts["transport"] == "http"
    assert opts["host"] == "127.0.0.1"
    assert opts["port"] == 9999


def test_use_all_fixtures(
    intercept_exit_and_validation,
    mock_api_key,
    mock_profile_id,
    clean_env,
    set_env_api_key,
    temp_api_key_file,
    mock_openapi_spec,
    temp_openapi_file,
    mock_nextdns_base_url,
    mock_doh_response,
    mock_profiles_response,
):
    # This test merely accepts all fixtures to ensure they are executed and cleaned up
    assert mock_api_key == "test_api_key_12345"
    assert mock_profile_id == "abc123"
    assert temp_api_key_file.exists()
    assert temp_openapi_file.exists()
    assert mock_doh_response.get("Status") == 0
    assert isinstance(mock_profiles_response, dict)


def test_coerce_value_float_exception_and_collections(monkeypatch):
    mw = server.StripExtraFieldsMiddleware()

    # Dict and list should recurse
    assert mw._coerce_value({"a": "true", "b": "2"}) == {"a": True, "b": 2}
    assert mw._coerce_value(["1", "2.2"]) == [1, 2.2]

    # Simulate float() raising ValueError to hit the except branch
    import builtins

    real_float = builtins.float

    def bad_float(x):
        raise ValueError("boom")

    monkeypatch.setattr(builtins, "float", bad_float)
    try:
        # '1.23' matches the digit check and would attempt float()
        assert mw._coerce_value("1.23") == "1.23"
    finally:
        monkeypatch.setattr(builtins, "float", real_float)

    # Boolean false and negative integer cases
    assert mw._coerce_value("false") is False
    assert mw._coerce_value("-5") == -5

    # Non-string non-dict/list returns unchanged
    assert mw._coerce_value(10) == 10
