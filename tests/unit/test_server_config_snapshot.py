"""Regression tests for the per-server immutable configuration snapshot.

The three oracle probes from the card are covered here directly: two embedded
servers keep their own settings after the process environment changes, one
generative-UI decision governs one build, and startup validation rejects an
unsupported tool-search mode.
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import httpx
import pytest
from fastmcp import FastMCP

from nextdns_mcp import catalog, config, generative, state
from nextdns_mcp import client as client_module
from nextdns_mcp import server as server_module
from nextdns_mcp.config import ServerConfig
from nextdns_mcp.lifespan import ServerResources, server_lifespan, server_resources
from nextdns_mcp.openapi import create_mcp_server
from nextdns_mcp.server import build_mcp_server
from nextdns_mcp.tools import doh
from nextdns_mcp.tools.logs import redirects
from nextdns_mcp.tools.logs import stream as stream_module
from nextdns_mcp.utils import resolve_profile_id


class _FakeResponse:
    """A streaming response shaped the way ``_write_stream_to_tempfile`` reads one."""

    def __init__(self, url: str, *, chunks: list[bytes]) -> None:
        self.request = httpx.Request("GET", url)
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "text/csv"})
        self._chunks = chunks
        self._consumed = False

    @property
    def has_redirect_location(self) -> bool:
        return False

    @property
    def is_error(self) -> bool:
        return False

    @property
    def is_stream_consumed(self) -> bool:
        return self._consumed

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        self._consumed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _RecordingRedirectClient:
    """The unauthenticated redirect client, recording what timeout it used."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.cookies: dict[str, str] = {}
        self.construct_timeouts: list[Any] = []
        self.request_timeouts: list[Any] = []

    def stream(self, method: str, url: Any, **kwargs: Any) -> _FakeResponse:
        self.request_timeouts.append(kwargs.get("timeout"))
        return self.responses.pop(0)

    async def aclose(self) -> None:
        return None


def test_snapshot_keeps_each_servers_default_profile(monkeypatch) -> None:
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "aaa111")
    server_a = build_mcp_server()
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "bbb222")
    server_b = build_mcp_server()

    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "ccc333")
    assert resolve_profile_id()[0] == "ccc333"

    resources_a = server_resources(server_a)
    resources_b = server_resources(server_b)
    assert resources_a.config.default_profile == "aaa111"
    assert resources_b.config.default_profile == "bbb222"

    token = state.server_config.set(resources_a.config)
    try:
        assert resolve_profile_id()[0] == "aaa111"
    finally:
        state.server_config.reset(token)

    token = state.server_config.set(resources_b.config)
    try:
        assert resolve_profile_id()[0] == "bbb222"
    finally:
        state.server_config.reset(token)


def test_snapshot_freezes_values_and_reads_secret_file_from_snapshot(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text("secret-file-key\n", encoding="utf-8")
    monkeypatch.setenv("NEXTDNS_API_BASE", "https://one.example/")
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "7.5")
    monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "40")
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "aaa111")
    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
    monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")

    snapshot = ServerConfig.from_env()

    monkeypatch.delenv("NEXTDNS_API_BASE")
    monkeypatch.delenv("NEXTDNS_API_KEY_FILE")
    monkeypatch.setenv("NEXTDNS_API_KEY", "process-key")
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "99")
    monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "1")
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "bbb222")
    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "off")
    monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "bm25")

    assert snapshot.api_base == "https://one.example"
    assert snapshot.api_key == "secret-file-key"
    assert snapshot.http_timeout == 7.5
    assert snapshot.download_max_bytes == 40
    assert snapshot.default_profile == "aaa111"
    assert snapshot.generative_ui_enabled is True
    assert snapshot.tool_search_mode == "off"


def test_snapshot_can_carry_the_builds_generative_decision(monkeypatch) -> None:
    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "off")
    snapshot = ServerConfig.from_env(generative_ui_enabled=True)
    assert snapshot.generative_ui_enabled is True


def test_generative_ui_setting_is_evaluated_once_per_build(monkeypatch) -> None:
    reads = 0

    def counted() -> bool:
        nonlocal reads
        reads += 1
        return True

    providers: list[Any] = []
    monkeypatch.setattr(server_module, "is_generative_ui_enabled", counted)
    monkeypatch.setattr(
        server_module, "register_generative_ui_provider", lambda mcp, enabled: providers.append((mcp, enabled))
    )

    mcp = build_mcp_server()

    assert reads == 1
    assert providers == [(mcp, True)]
    assert catalog.enabled_generative_ui_tool_names(True) == catalog.generative_ui_tool_names()


def test_generative_ui_enabled_names_include_provider_tools_when_env_on(monkeypatch) -> None:
    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
    names = set(server_module.published_tool_names())
    assert "generate_prefab_ui" in names
    assert "search_prefab_components" in names


def test_catalog_provider_registration_falls_back_to_environment(monkeypatch) -> None:
    registered: list[Any] = []

    def add_provider(self: FastMCP, provider: Any, *, namespace: str = "") -> None:
        registered.append(provider)

    class DummyProvider:
        pass

    monkeypatch.setattr(generative, "PrefabGenerativeUI", DummyProvider)
    off_server = FastMCP(name="off")
    on_server = FastMCP(name="on")
    monkeypatch.setattr(FastMCP, "add_provider", add_provider)

    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "off")
    catalog.register_generative_ui_provider(off_server)
    assert registered == []

    monkeypatch.setenv("NEXTDNS_GENERATIVE_UI", "on")
    catalog.register_generative_ui_provider(on_server)
    assert len(registered) == 1
    assert isinstance(registered[0], DummyProvider)


def test_validate_configuration_rejects_unsupported_tool_search_mode(monkeypatch, mock_api_key: str) -> None:
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "semantic")

    with pytest.raises(config.ConfigurationError, match="NEXTDNS_TOOL_SEARCH"):
        config.validate_configuration()


def test_build_mcp_server_accepts_a_prepared_snapshot(monkeypatch) -> None:
    def fail_if_called() -> bool:
        raise AssertionError("a passed configuration snapshot already carries the generative-UI decision")

    monkeypatch.setattr(server_module, "is_generative_ui_enabled", fail_if_called)
    snapshot = ServerConfig.from_env()
    mcp = build_mcp_server(config=snapshot)

    assert server_resources(mcp).config is snapshot


def test_served_request_config_beats_the_ambient_lifespan_config(monkeypatch) -> None:
    served = ServerConfig.from_env()
    ambient = ServerConfig.from_env()
    resources = ServerResources(config=served)

    class RequestCtx:
        lifespan_context: ClassVar[dict[str, Any]] = {state.RESOURCES_LIFESPAN_KEY: resources}

    token = state.server_config.set(ambient)
    monkeypatch.setattr(state, "fastmcp_request_ctx", SimpleNamespace(get=lambda: RequestCtx()))
    try:
        assert state.current_server_config() is served
    finally:
        state.server_config.reset(token)


def test_current_server_config_falls_back_to_ambient_lifespan() -> None:
    ambient = ServerConfig.from_env()
    token = state.server_config.set(ambient)
    try:
        assert state.current_server_config() is ambient
    finally:
        state.server_config.reset(token)


def test_current_server_config_is_none_outside_a_server() -> None:
    assert state.current_server_config() is None


async def test_nested_lifespan_host_serves_each_servers_snapshot(monkeypatch) -> None:
    """Two lifespans in one task must not make both servers use the last-entered snapshot."""
    from fastmcp import Client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        body = {"meta": {"series": {"times": ["2026-11-27T00:00:00Z"]}}, "data": [{"queries": [1]}]}
        return httpx.Response(200, json=body, request=request)

    def recording_client(config: ServerConfig | None = None) -> client_module.AccessControlledClient:
        return client_module.AccessControlledClient(
            base_url="https://api.nextdns.io",
            headers={"X-Api-Key": "dummy-key-0001"},
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            configured_base="https://api.nextdns.io",
        )

    monkeypatch.setenv("NEXTDNS_API_KEY", "dummy-key-0001")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_TOOL_SEARCH", "off")
    monkeypatch.setattr(client_module, "create_nextdns_client", recording_client)

    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "aaa111")
    server_a = build_mcp_server()
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "bbb222")
    server_b = build_mcp_server()
    monkeypatch.setenv("NEXTDNS_DEFAULT_PROFILE", "ccc333")

    async with server_lifespan(server_a), server_lifespan(server_b):
        async with Client(server_a) as client_a:
            await client_a.call_tool("plotAnalytics", {"metric": "status"}, raise_on_error=False)
        async with Client(server_b) as client_b:
            await client_b.call_tool("plotAnalytics", {"metric": "status"}, raise_on_error=False)

    assert any("/profiles/aaa111/" in url for url in seen)
    assert any("/profiles/bbb222/" in url for url in seen)


def test_client_owner_uses_the_snapshot_that_created_it(monkeypatch, mock_api_key: str) -> None:
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "11")
    snapshot = ServerConfig.from_env()
    monkeypatch.setenv("NEXTDNS_API_KEY", "process-key")
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "22")

    owners: list[ServerConfig] = []

    def fake_create_nextdns_client(config_snapshot: ServerConfig | None = None) -> object:
        assert config_snapshot is not None
        owners.append(config_snapshot)
        return object()

    monkeypatch.setattr(client_module, "create_nextdns_client", fake_create_nextdns_client)
    with client_module.api_client_owned(client_module.ApiClientOwner(config=snapshot)):
        assert client_module.get_api_client() is not None
    assert owners == [snapshot]


def test_authenticated_client_kwargs_use_snapshot(monkeypatch, mock_api_key: str) -> None:
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)
    monkeypatch.setenv("NEXTDNS_API_BASE", "https://snapshot.example/")
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "13")
    snapshot = ServerConfig.from_env()
    monkeypatch.setenv("NEXTDNS_API_KEY", "process-key")
    monkeypatch.setenv("NEXTDNS_API_BASE", "https://process.example/")
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "14")

    kwargs = client_module._authenticated_client_kwargs(snapshot)

    assert kwargs["base_url"] == "https://snapshot.example"
    assert kwargs["configured_base"] == "https://snapshot.example"
    assert kwargs["headers"]["X-Api-Key"] == mock_api_key
    assert kwargs["timeout"] == 13.0


async def test_doh_owner_uses_snapshot_timeout(monkeypatch) -> None:
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "7.5")
    snapshot = ServerConfig.from_env()
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "99")

    owner = doh.DohClientOwner(config=snapshot)
    doh_client = owner.get()
    try:
        assert doh_client.timeout.connect == 7.5
    finally:
        await owner.aclose()


async def test_log_download_path_uses_snapshot_timeout_and_cap(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "7.5")
    monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "10")
    snapshot = ServerConfig.from_env()
    monkeypatch.setenv("NEXTDNS_HTTP_TIMEOUT", "99")
    monkeypatch.setenv("NEXTDNS_DOWNLOAD_MAX_BYTES", "1")

    mcp = create_mcp_server(config=snapshot)
    redirect = _RecordingRedirectClient([_FakeResponse("https://storage.example/file.csv", chunks=[b"a,b,c\n"])])

    def fake_async_client(*args: Any, **kwargs: Any) -> _RecordingRedirectClient:
        redirect.construct_timeouts.append(kwargs.get("timeout"))
        return redirect

    async def fake_check_target(url: httpx.URL) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(redirects.guard, "_check_download_redirect_target", fake_check_target)
    monkeypatch.setattr(redirects.guard, "_pinned_dns", lambda host, infos: nullcontext())
    monkeypatch.setattr(redirects.guard, "_PIN_FETCH_LOCK", asyncio.Lock())

    async with server_lifespan(mcp):
        payload = await redirects._follow_redirects_to_tempfile(
            httpx.URL("https://storage.example/file.csv"), str(tmp_path / "download.csv")
        )
        assert payload["size"] == 6
        assert redirect.construct_timeouts == [7.5]
        assert redirect.request_timeouts == [7.5]

        large = await stream_module._write_stream_to_tempfile(
            _FakeResponse("https://api.nextdns.io/file.csv", chunks=[b"abcdefghij"]),
            str(tmp_path / "large.csv"),
        )
        assert large["size"] == 10

    with pytest.raises(stream_module.DownloadTooLargeError):
        await stream_module._write_stream_to_tempfile(
            _FakeResponse("https://api.nextdns.io/file.csv", chunks=[b"abcdefghij"]),
            str(tmp_path / "outside.csv"),
        )
