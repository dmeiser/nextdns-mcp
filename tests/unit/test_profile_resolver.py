"""Unit tests for the centralized profile resolver utility.

SPDX-License-Identifier: MIT
"""

import os

import pytest

from nextdns_mcp.errors import ErrorCode
from nextdns_mcp.server import _validate_profile_id, resolve_profile_id


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment for each test."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


class TestResolveProfileIdWithDefault:
    """Tests for resolve_profile_id with default fallback (allow_default=True)."""

    def test_returns_explicit_valid_profile_string(self):
        resolved, error = resolve_profile_id("abc123")
        assert resolved == "abc123"
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

    def test_uses_custom_default_fn(self):
        resolved, error = resolve_profile_id(None, default_fn=lambda: "xyz789")
        assert resolved == "xyz789"
        assert error is None

    def test_custom_default_fn_returning_none(self):
        resolved, error = resolve_profile_id(None, default_fn=lambda: None)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.MISSING_PROFILE_ID


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

    def test_rejects_none_even_if_default_profile_is_configured(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        resolved, error = resolve_profile_id(None, allow_default=False)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.INVALID_PROFILE_ID
        assert error["error"] == "Invalid profile_id format: None"

    def test_rejects_empty_string_even_if_default_profile_is_configured(self, clean_env):
        clean_env("NEXTDNS_DEFAULT_PROFILE", "def456")
        resolved, error = resolve_profile_id("", allow_default=False)
        assert resolved is None
        assert error is not None
        assert error["code"] == ErrorCode.INVALID_PROFILE_ID
        assert error["error"] == "Invalid profile_id format: "

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


class TestValidateProfileIdCompatibility:
    """Tests for _validate_profile_id backward-compatible helper."""

    def test_returns_none_for_valid_profile_id(self):
        assert _validate_profile_id("abc123") is None
        assert _validate_profile_id(123456) is None

    def test_returns_error_for_invalid_profile_id(self):
        error = _validate_profile_id("abc/def")
        assert error is not None
        assert error["code"] == ErrorCode.INVALID_PROFILE_ID
        assert error["error"] == "Invalid profile_id format: abc/def"
