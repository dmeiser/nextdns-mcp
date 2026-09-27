"""Tests for the per-request profile access control snapshot (issue #179).

The contract under test: a request reads the ACL environment exactly once,
into an immutable snapshot, and every access check for that request -- plus
the decision to send the request upstream -- is made against that snapshot.
A change to the environment that lands while a request is in flight therefore
cannot desynchronize the checks from each other; it takes effect at the next
request instead.
"""

import asyncio
import dataclasses
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import config
from nextdns_mcp.tools import doh as doh_module
from nextdns_mcp.tools import profiles as profiles_module

ACL_ENV_VARS = (
    "NEXTDNS_READ_ONLY",
    "NEXTDNS_READABLE_PROFILES",
    "NEXTDNS_WRITABLE_PROFILES",
)


def run(coro: Any) -> Any:
    """Run a coroutine to completion (tests here are sync by design)."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from a deny-all environment."""
    for var in ACL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def acl_client():
    return client_module.AccessControlledClient()


def _counted_getenv(monkeypatch) -> dict[str, int]:
    """Wrap os.getenv so tests can count reads of the ACL variables."""
    real_getenv = os.getenv
    counts: dict[str, int] = {}

    def counting_getenv(key, default=None):
        if key in ACL_ENV_VARS:
            counts[key] = counts.get(key, 0) + 1
        return real_getenv(key, default)

    monkeypatch.setattr(config.os, "getenv", counting_getenv)
    return counts


# --- Snapshot semantics ----------------------------------------------------


def test_snapshot_reflects_environment(monkeypatch):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "def456")

    snapshot = config.load_profile_access_control()

    assert snapshot.read_only is False
    # Write implies read, so both profiles are readable.
    assert snapshot.readable == frozenset({"abc123", "def456"})
    assert snapshot.writable == frozenset({"def456"})
    assert snapshot.can_read("abc123") and snapshot.can_read("def456")
    assert snapshot.can_write("def456")
    assert not snapshot.can_write("abc123")
    assert snapshot.any_readable and snapshot.any_writable


def test_snapshot_is_immutable():
    snapshot = config.load_profile_access_control()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.read_only = True  # type: ignore[misc]
    assert isinstance(snapshot.readable, (frozenset, type(None)))
    assert isinstance(snapshot.writable, (frozenset, type(None)))


def test_snapshot_allow_all(monkeypatch):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    snapshot = config.load_profile_access_control()

    assert snapshot.readable == frozenset()  # allow all
    assert snapshot.writable == frozenset()  # allow all
    assert snapshot.can_read("anything")
    assert snapshot.can_write("anything")


def test_snapshot_deny_all_by_default():
    snapshot = config.load_profile_access_control()

    assert snapshot.read_only is False
    assert snapshot.readable is None
    assert snapshot.writable is None
    assert not snapshot.can_read("abc123")
    assert not snapshot.can_write("abc123")
    assert not snapshot.any_readable
    assert not snapshot.any_writable


def test_snapshot_read_only_drops_writable(monkeypatch):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")

    snapshot = config.load_profile_access_control()

    assert snapshot.read_only is True
    assert snapshot.writable is None
    assert snapshot.any_writable is False
    assert not snapshot.can_write("abc123")
    # Read-only mode does not grant read access on its own; abc123 is readable
    # because it is listed in NEXTDNS_READABLE_PROFILES.
    assert snapshot.can_read("abc123")


def test_snapshot_writable_only_is_readable(monkeypatch):
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")

    snapshot = config.load_profile_access_control()

    assert snapshot.readable == frozenset({"abc123"})
    assert snapshot.can_read("abc123")
    assert snapshot.can_write("abc123")


def test_writable_all_grants_read_all(monkeypatch):
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")

    snapshot = config.load_profile_access_control()

    assert snapshot.readable == frozenset()  # allow all
    assert snapshot.any_readable


# --- Per-request contract --------------------------------------------------


def test_env_is_read_once_per_request(monkeypatch, acl_client):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    counts = _counted_getenv(monkeypatch)

    snapshot_calls = []
    real_loader = config.load_profile_access_control

    def counting_loader():
        snapshot_calls.append(1)
        return real_loader()

    monkeypatch.setattr(client_module, "load_profile_access_control", counting_loader)

    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = httpx.Response(200, json={"ok": True})
        response = run(acl_client.request("PUT", "/profiles/abc123/settings"))

    assert response.status_code == 200
    assert len(snapshot_calls) == 1, "one request must take exactly one ACL snapshot"
    # Each ACL variable is read exactly once for that snapshot.
    assert counts == {var: 1 for var in ACL_ENV_VARS}


def test_acl_env_read_at_most_once_per_request(monkeypatch, acl_client):
    """Regression for issue #179: one request reads each ACL variable once.

    Asserted purely through the public request path and os.getenv counting, so
    it fails against the pre-fix code (which consulted the environment
    repeatedly per request) and passes against the snapshot implementation.
    """
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
    counts = _counted_getenv(monkeypatch)

    async def fake_request(self, method, url, **kwargs):
        return httpx.Response(200, json={"ok": True})

    with patch.object(httpx.AsyncClient, "request", new=fake_request):
        response = run(acl_client.request("PUT", "/profiles/abc123/settings"))

    assert response.status_code == 200
    for var in ACL_ENV_VARS:
        assert counts.get(var, 0) <= 1, f"{var} was read {counts.get(var, 0)} times for one request"


def test_mid_request_env_change_cannot_desync(monkeypatch, acl_client):
    """The ACL decision and the outgoing request use the same values.

    The environment is rewritten to deny everything *while* the upstream
    request is being sent. The in-flight request must still reflect the values
    captured at the start of the request (no half-and-half state), and the new
    values must apply from the next request on.
    """
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
    env_during_request: dict[str, str | None] = {}

    async def env_mutating_super_request(self, method, url, **kwargs):
        # Env changes mid-flight: revoke everything. monkeypatch tracks these
        # so the teardown restores the environment for the rest of the suite.
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "")
        monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "")
        monkeypatch.setenv("NEXTDNS_READ_ONLY", "true")
        env_during_request["readable"] = os.environ.get("NEXTDNS_READABLE_PROFILES")
        # A snapshot taken now reflects the new env...
        env_during_request["fresh_readable"] = config.load_profile_access_control().readable
        return httpx.Response(200, json={"ok": True})

    with patch.object(httpx.AsyncClient, "request", new=env_mutating_super_request):
        response = run(acl_client.request("PUT", "/profiles/abc123/settings"))

    # ...but the request that was already in flight was allowed by the
    # snapshot it started with, so it is neither denied nor silently upgraded.
    assert response.status_code == 200
    assert env_during_request["readable"] == ""
    assert env_during_request["fresh_readable"] is None

    # The next request picks up the new environment: write now denied.
    with (
        patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request,
        pytest.raises(client_module.AccessDeniedError) as exc_info,
    ):
        run(acl_client.request("PUT", "/profiles/abc123/settings"))
    assert exc_info.value.code == "write_access_denied"
    assert mock_request.await_count == 0, "denied requests must not reach the network"


def test_snapshot_taken_once_for_stream(monkeypatch, acl_client):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
    counts = _counted_getenv(monkeypatch)

    response = httpx.Response(200, json={"ok": True})
    transport = httpx.MockTransport(lambda request: response)
    streaming_client = client_module.AccessControlledClient(base_url="https://api.nextdns.io", transport=transport)

    async def consume():
        async with streaming_client.stream("GET", "/profiles/abc123/settings") as resp:
            return await resp.aread()

    assert run(consume()) == b'{"ok":true}'
    assert counts == {var: 1 for var in ACL_ENV_VARS}


def test_collection_checks_use_one_snapshot(monkeypatch, acl_client):
    """Collection endpoints read the environment once too."""
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "abc123")
    counts = _counted_getenv(monkeypatch)

    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = httpx.Response(200, json={"ok": True})
        assert run(acl_client.request("GET", "/profiles")).status_code == 200
        assert run(acl_client.request("POST", "/profiles")).status_code == 200

    # Two requests, one snapshot each.
    assert counts == {var: 2 for var in ACL_ENV_VARS}


def test_collection_write_denied_without_writable(monkeypatch, acl_client):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    with (
        patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request,
        pytest.raises(client_module.AccessDeniedError) as exc_info,
    ):
        run(acl_client.request("POST", "/profiles"))
    assert exc_info.value.code == "write_access_denied"
    assert "no profiles are writable" in str(exc_info.value)
    assert mock_request.await_count == 0


def test_collection_read_denied_without_readable(monkeypatch, acl_client):
    with (
        patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request,
        pytest.raises(client_module.AccessDeniedError) as exc_info,
    ):
        run(acl_client.request("GET", "/profiles"))
    assert exc_info.value.code == "read_access_denied"
    assert "no profiles are readable" in str(exc_info.value)
    assert mock_request.await_count == 0


def test_read_only_collection_write_denied(monkeypatch, acl_client):
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_READ_ONLY", "1")
    with (
        patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request,
        pytest.raises(client_module.AccessDeniedError) as exc_info,
    ):
        run(acl_client.request("POST", "/profiles"))
    assert exc_info.value.code == "write_access_denied"
    assert "read-only" in str(exc_info.value).lower()
    assert mock_request.await_count == 0


def test_access_control_logging_uses_one_snapshot(monkeypatch):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "def456")
    counts = _counted_getenv(monkeypatch)

    config._log_access_control_settings()

    assert counts == {var: 1 for var in ACL_ENV_VARS}


def test_convenience_wrappers_match_snapshot(monkeypatch):
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "def456")

    snapshot = config.load_profile_access_control()
    assert config.get_readable_profiles_set() == set(snapshot.readable)
    assert config.get_writable_profiles_set() == set(snapshot.writable)
    assert config.can_read_profile("abc123") is snapshot.can_read("abc123")
    assert config.can_write_profile("def456") is snapshot.can_write("def456")
    assert config.is_read_only() is snapshot.read_only


# --- Collection tools share one snapshot for the whole operation (issue #257)


@pytest.mark.parametrize(
    ("operation", "kwargs", "env"),
    [
        ("list", {}, {"NEXTDNS_READABLE_PROFILES": "abc123"}),
        ("create", {"name": "new"}, {"NEXTDNS_WRITABLE_PROFILES": "abc123"}),
    ],
)
def test_collection_tool_takes_one_snapshot_per_operation(monkeypatch, operation, kwargs, env):
    """manageProfiles reads the ACL environment once per logical call.

    The tool's own checks used to take one snapshot each (two for
    ``create``, which consults both the read-only flag and the writable
    set), so a single MCP call read every ACL variable twice. The upstream
    request is stubbed out here: this asserts the tool-side contract, the
    client's own send-time re-check is covered by the request tests above.
    """
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    counts = _counted_getenv(monkeypatch)

    with patch.object(profiles_module, "_api_request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = {"profiles": []}
        result = run(profiles_module.manageProfiles(operation, **kwargs))

    assert "error" not in result
    assert counts == {var: 1 for var in ACL_ENV_VARS}


# --- DoH lookups share the request's snapshot ------------------------------


class _RecordingDohClient:
    """Stand-in DoH client that records the queries it was asked to send."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def get(self, doh_url, params=None, headers=None):
        self.queries.append(doh_url)
        return _StubResponse({"Status": 0})


class _StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def test_doh_query_is_authorized_by_the_request_snapshot(monkeypatch):
    """A DoH query is sent only under the ACL values of the request that gated it."""
    doh_client = _RecordingDohClient()
    monkeypatch.setattr(doh_module, "_doh_client", doh_client)
    doh_url = "https://dns.nextdns.io/abc123/dns-query"

    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    allowing = config.load_profile_access_control()
    # Environment tightened while the request is in flight: the in-flight
    # request keeps the values it started with, the next one does not.
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "")
    allowed = run(doh_module.doh_lookup(doh_url, "example.com", "A", "abc123", allowing))
    assert "error" not in allowed
    assert doh_client.queries == [doh_url]

    denying = config.load_profile_access_control()
    # Environment reopened after the denying snapshot was taken: a request
    # holding that snapshot still must not put the query on the wire.
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "abc123")
    denied = run(doh_module.doh_lookup(doh_url, "example.com", "A", "abc123", denying))
    assert denied["code"] == "read_access_denied"
    assert doh_client.queries == [doh_url]
