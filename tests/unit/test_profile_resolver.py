"""Unit tests for the centralized profile resolver utility.

``resolve_profile_id`` is used by the two hand-written tools (``dohLookup``,
``plotAnalytics``); the generated tools resolve the id as a path placeholder in
``spec_tools`` instead, and a missing one is refused there before any request.
Both are covered here, because both decide whether a call is made at all.

SPDX-License-Identifier: MIT
"""

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nextdns_mcp import client as client_module
from nextdns_mcp import spec_tools
from nextdns_mcp.errors import ErrorCode
from nextdns_mcp.tools.doh import dohLookup
from nextdns_mcp.tools.plots import plotAnalytics
from nextdns_mcp.utils import resolve_profile_id


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


@pytest.fixture
def mock_api_client(monkeypatch):
    """Replace the module-level api_client with a mock."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "test-api-key")
    client = AsyncMock()
    monkeypatch.setattr(client_module, "api_client", client)
    return client


# Every tool that resolves a profile id through ``resolve_profile_id``. Neither
# is generated from the spec, so both go through the resolver rather than a path
# template, and both report an omitted profile_id as ``missing_profile_id``.
_PROFILE_ID_SITES = [
    ("doh", lambda pid: dohLookup("example.com", pid, "A")),
    ("plot", lambda pid: plotAnalytics("status", profile_id=pid)),
]


class TestResolveProfileIdWithDefault:
    """Tests for resolve_profile_id with default fallback (allow_default=True)."""

    def test_returns_explicit_valid_profile_string(self):
        resolved, error = resolve_profile_id("abc123")
        assert resolved == "abc123"
        assert error is None

    def test_returns_explicit_valid_10_char_profile_string(self):
        """New profiles from the live API carry 10-character IDs (see CI run 37139687479)."""
        resolved, error = resolve_profile_id("abc123def0")
        assert resolved == "abc123def0"
        assert error is None

    def test_returns_explicit_valid_profile_int(self):
        resolved, error = resolve_profile_id(123456)
        assert resolved == "123456"
        assert error is None

    def test_falls_back_to_default_profile_when_none(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        resolved, error = resolve_profile_id(None)
        assert resolved == "def456"
        assert error is None

    def test_falls_back_to_default_profile_when_empty_string(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        resolved, error = resolve_profile_id("")
        assert resolved == "def456"
        assert error is None

    def test_returns_missing_error_when_no_profile_and_no_default(self, clean_env):
        resolved, error = resolve_profile_id(None)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.MISSING_PROFILE_ID
        assert "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set" in error["error"]
        assert "Provide profile_id parameter" in error.get("hint", "")

    def test_returns_missing_error_when_empty_string_and_no_default(self, clean_env):
        resolved, error = resolve_profile_id("")
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.MISSING_PROFILE_ID
        assert "No profile_id provided and NEXTDNS_DEFAULT_PROFILE not set" in error["error"]

    @pytest.mark.parametrize(
        "invalid_id",
        [
            "abc/def",
            "AbC123",
            "ABCDEF",
            "abc",
            "a" * 40,
            "abc_def",
            "abc-def",
            "abc.def",
            "abc/../def",
        ],
    )
    def test_rejects_invalid_explicit_profile_format(self, invalid_id):
        resolved, error = resolve_profile_id(invalid_id)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.INVALID_PROFILE_ID
        assert error["error"] == f"Invalid profile_id format: {invalid_id}"

    def test_rejects_invalid_default_profile_format(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "invalid_def")
        resolved, error = resolve_profile_id(None)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.INVALID_PROFILE_ID
        assert error["error"] == "Invalid profile_id format: invalid_def"


class TestResolveProfileIdMandatory:
    """Tests for resolve_profile_id without default fallback (allow_default=False)."""

    def test_accepts_valid_profile_string(self):
        resolved, error = resolve_profile_id("abc123", allow_default=False)
        assert resolved == "abc123"
        assert error is None

    def test_accepts_valid_profile_int(self):
        resolved, error = resolve_profile_id(123456, allow_default=False)
        assert resolved == "123456"
        assert error is None

    def test_reports_none_as_missing_even_if_default_profile_is_configured(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        resolved, error = resolve_profile_id(None, allow_default=False)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.MISSING_PROFILE_ID
        assert error["error"] == (
            "profile_id is required for this tool; NEXTDNS_DEFAULT_PROFILE is not used by this operation"
        )

    def test_reports_empty_string_as_missing_even_if_default_profile_is_configured(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        resolved, error = resolve_profile_id("", allow_default=False)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.MISSING_PROFILE_ID

    @pytest.mark.parametrize(
        "invalid_id",
        [
            "abc/def",
            "AbC123",
            "ABCDEF",
            "abc",
            "a" * 40,
            "abc_def",
            "abc-def",
            "abc.def",
            "abc/../def",
        ],
    )
    def test_rejects_invalid_profile_format_when_mandatory(self, invalid_id):
        resolved, error = resolve_profile_id(invalid_id, allow_default=False)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.INVALID_PROFILE_ID
        assert error["error"] == f"Invalid profile_id format: {invalid_id}"


class TestProfileIdCallSites:
    """Every ``resolve_profile_id`` call site must distinguish absent from malformed.

    An omitted ``profile_id`` is an absent argument (``missing_profile_id``), not
    a malformed one: the resolver must never quote a value the caller never sent
    back at the LLM driving the tool. And neither answer may be reached by way of
    a request — a call with no id it could authorize must stop at the resolver.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call", [site[1] for site in _PROFILE_ID_SITES], ids=[site[0] for site in _PROFILE_ID_SITES]
    )
    @pytest.mark.parametrize("omitted", [None, ""], ids=["none", "blank"])
    async def test_omitted_profile_id_reports_missing(self, call, omitted, mock_api_client):
        result = await call(omitted)
        assert result["code"] == ErrorCode.MISSING_PROFILE_ID
        assert mock_api_client.request.await_count == 0, "the call reached the network without a profile id"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [site[1] for site in _PROFILE_ID_SITES],
        ids=[site[0] for site in _PROFILE_ID_SITES],
    )
    async def test_malformed_profile_id_reports_invalid(self, call, mock_api_client):
        result = await call("nope!")
        assert result["code"] == ErrorCode.INVALID_PROFILE_ID
        assert result["error"] == "Invalid profile_id format: nope!"
        assert mock_api_client.request.await_count == 0, "the malformed id was sent upstream anyway"


class TestGeneratedToolProfileGuard:
    """A generated tool refuses an absent profile id before any request is made.

    It does not call ``resolve_profile_id``: the id is a path template
    placeholder, and an unfilled one would be sent upstream as the literal
    ``/profiles/{profile_id}/...`` — a URL the access-control layer cannot
    authorize, because there is no profile id in it to check. So the generator
    reports the missing argument itself, naming the endpoint it cannot build.
    """

    @pytest.fixture
    def recorder(self, monkeypatch) -> list[dict[str, Any]]:
        """Stand in for the HTTP layer so the guard's "no request" claim is observable.

        The list it returns is what the endpoint received: an empty list after a
        call means the tool refused before it built a request.
        """
        seen: list[dict[str, Any]] = []

        async def fake_payload(method: str, url: str, params=None, json_body=None):
            seen.append({"method": method, "url": url})
            return {"data": []}

        monkeypatch.setattr(spec_tools, "_api_request_payload", fake_payload)
        return seen

    @pytest.fixture
    def routes(self) -> dict[str, Any]:
        return {route.operation_id: route for route in spec_tools.spec_routes()}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation_id", ["getDenylist", "clearLogs", "getAnalyticsDomains"])
    async def test_a_blank_profile_id_is_refused_without_a_request(self, operation_id, routes, recorder):
        payload = (await spec_tools.SpecApiTool(routes[operation_id]).run({"profile_id": ""})).structured_content
        assert payload["code"] == ErrorCode.MISSING_REQUIRED_ARGUMENT
        assert "profile_id" in payload["error"]
        assert recorder == [], "the unfilled path placeholder was sent upstream"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation_id", ["getDenylist", "clearLogs"])
    async def test_a_whitespace_only_profile_id_is_refused_as_invalid(self, operation_id, routes, recorder):
        """Blank is not the only shape that means "no id".

        Spaces would be substituted into the path segment, and the ACL layer
        reads the profile id back out of that path — so a whitespace id is
        refused as invalid, not treated as absent.
        """
        payload = (await spec_tools.SpecApiTool(routes[operation_id]).run({"profile_id": "   "})).structured_content
        assert payload["code"] == ErrorCode.INVALID_PROFILE_ID
        assert recorder == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation_id", ["getDenylist", "clearLogs"])
    async def test_an_omitted_profile_id_is_refused_too(self, operation_id, routes, recorder):
        payload = (await spec_tools.SpecApiTool(routes[operation_id]).run({})).structured_content
        assert payload["code"] == ErrorCode.MISSING_REQUIRED_ARGUMENT
        assert recorder == []

    @pytest.mark.asyncio
    async def test_a_filled_profile_id_builds_the_endpoint(self, routes, recorder):
        """The guard has to leave a good call alone, or it would be a blanket refusal."""
        result = await spec_tools.SpecApiTool(routes["getDenylist"]).run({"profile_id": "abc123"})
        assert result.structured_content == {"data": []}
        assert recorder == [{"method": "GET", "url": "/profiles/abc123/denylist"}]
