"""Regression tests for the profile-resolution guard in the tool layer (issue #281).

Every tool resolves a profile id with ``resolve_profile_id`` and then narrows
``str | None`` to ``str`` before building a request URL. The narrowing used to be
a bare ``assert target_profile is not None``, which ``python -O`` strips, leaving
the tool free to build a literal ``/profiles/None/...`` URL. These tests pin the
invariant that a tool never builds a URL from an unvalidated profile id: when
resolution yields no id and no error, the tool must return a typed error payload
and must not issue any request.
"""

from unittest.mock import AsyncMock

import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp.errors import ErrorCode
from nextdns_mcp.tools import analytics as analytics_module
from nextdns_mcp.tools import doh as doh_module
from nextdns_mcp.tools import lists as lists_module
from nextdns_mcp.tools import logs as logs_module
from nextdns_mcp.tools import plots as plots_module
from nextdns_mcp.tools import profiles as profiles_module
from nextdns_mcp.tools import rewrites as rewrites_module
from nextdns_mcp.tools import settings as settings_module

# One entry per tool site that narrows the resolved profile id before use.
TOOL_CALLS = [
    pytest.param(doh_module, lambda: doh_module._dohLookup_impl("example.com"), id="doh"),
    pytest.param(
        profiles_module, lambda: profiles_module._manage_profiles_impl("get", profile_id="abc123"), id="profiles"
    ),
    pytest.param(plots_module, lambda: plots_module._plot_analytics_series_impl("devices"), id="plots"),
    pytest.param(analytics_module, lambda: analytics_module._query_analytics_impl("domains", "abc123"), id="analytics"),
    pytest.param(logs_module, lambda: logs_module._manage_logs_impl("get", "abc123"), id="logs"),
    pytest.param(
        settings_module, lambda: settings_module._manage_settings_impl("get", "general", "abc123"), id="settings"
    ),
    pytest.param(lists_module, lambda: lists_module._manage_lists_impl("denylist", "get", "abc123"), id="lists"),
    pytest.param(
        rewrites_module, lambda: rewrites_module._manage_rewrites_impl("list", profile_id="abc123"), id="rewrites"
    ),
]


@pytest.fixture(autouse=True)
def open_profile_access(monkeypatch):
    """Allow all profile read/write access and provide a dummy API key."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
    monkeypatch.setenv("NEXTDNS_WRITABLE_PROFILES", "ALL")


@pytest.mark.parametrize(("module", "call"), TOOL_CALLS)
async def test_tool_fails_closed_when_profile_resolution_yields_no_id(monkeypatch, module, call):
    """An unresolved profile id must yield a typed error, never a /profiles/None URL."""
    monkeypatch.setattr(module, "resolve_profile_id", lambda *args, **kwargs: (None, None))

    api_client = AsyncMock()
    monkeypatch.setattr(client_module, "api_client", api_client)
    doh_client = AsyncMock()
    monkeypatch.setattr(doh_module, "_get_doh_client", lambda: doh_client)

    result = await call()

    assert api_client.request.await_count == 0
    assert doh_client.get.await_count == 0
    assert result["code"] == ErrorCode.INTERNAL_ERROR
    assert "profiles/None" not in result["error"]
