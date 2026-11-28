"""No log record may forge a second line or drive a terminal (issue #767).

The oracle is the reproduction filed against ``main``: one WARNING record built
from a caller-controlled URL came out as **two physical log lines** with a raw
``\\x1b`` byte in the stream, because the redaction helpers stripped only the
query string and the root logger used the stock formatter. These tests replay
that probe against the repo's real helpers and the real formatter — nothing is
reimplemented — and pin the two directions the fix must hold: control
characters never survive into a record, and the redaction issue #139 established
(query string stripped, httpx status errors logged as status only) still works.

SPDX-License-Identifier: MIT
"""

import io
import logging
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import httpx
import pytest
from fastmcp.exceptions import ToolError

from nextdns_mcp import client
from nextdns_mcp.constants import CONTROL_CHARS_PATTERN, escape_control_chars
from nextdns_mcp.logging_config import SanitizingFormatter, configure_logging
from nextdns_mcp.relaxation import StripExtraFieldsMiddleware
from nextdns_mcp.tools import doh, plots

if TYPE_CHECKING:
    from fastmcp import FastMCP

# What a caller can put into a profile id, a URL path, or a tool name: a record
# break that forges a complete CRITICAL line, and an ANSI escape that repaints
# the operator's terminal.
_FORGED_LINE = "\r\n2026-11-27 12:00:00 - nextdns_mcp.audit - CRITICAL - profile deleted by admin"
_FORGED_ESCAPE = "\x1b[31mFAKE-ALERT\x1b[0m"

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class TestEscapeControlChars:
    """The shared escaping: visible, complete, and safe to apply twice."""

    def test_record_break_becomes_visible_text(self) -> None:
        out = escape_control_chars(f"https://api.nextdns.io/profiles/abc{_FORGED_LINE}")

        assert not CONTROL_CHARS_PATTERN.search(out), f"control characters survived: {out!r}"
        # The injected bytes are still readable: erasing them would merge the
        # forgery into the record and hide the attempt.
        assert "\\r\\n" in out
        assert "nextdns_mcp.audit" in out

    def test_terminal_escapes_do_not_survive(self) -> None:
        out = escape_control_chars(f"https://api.nextdns.io/profiles/{_FORGED_ESCAPE}")

        assert "\x1b" not in out
        assert "\\x1b" in out
        assert "FAKE-ALERT" in out

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("\x00", "\\x00"), ("\x1f", "\\x1f"), ("\x7f", "\\x7f"), ("\x85", "\\x85"), ("\x9f", "\\x9f")],
        ids=["nul", "c0-last", "del", "c1-nel", "c1-last"],
    )
    def test_the_whole_c0_and_c1_class_is_escaped(self, raw: str, expected: str) -> None:
        assert escape_control_chars(f"a{raw}b") == f"a{expected}b"

    def test_text_without_control_characters_is_returned_as_is(self) -> None:
        assert escape_control_chars("dns_queries /profiles/abc123") == "dns_queries /profiles/abc123"

    def test_non_string_values_render_through_str(self) -> None:
        assert escape_control_chars(403) == "403"

    def test_escaping_is_idempotent_so_a_second_pass_is_harmless(self) -> None:
        once = escape_control_chars(f"x{_FORGED_LINE}")
        assert escape_control_chars(once) == once


class TestSanitizingFormatter:
    """The choke point: whatever a record interpolates, the rendered line stays one line."""

    @staticmethod
    def _make_record(msg: str, *args: object, level: int = logging.WARNING) -> logging.LogRecord:
        return logging.LogRecord("nextdns_mcp.transport", level, __file__, 1, msg, args, None)

    def test_interpolated_control_characters_are_escaped_in_the_output(self) -> None:
        record = self._make_record("Access denied in GET %s", f"https://api.nextdns.io/p/{_FORGED_ESCAPE}")
        rendered = SanitizingFormatter(_LOG_FORMAT, _DATE_FORMAT).format(record)

        assert "\n" not in rendered
        assert "\x1b" not in rendered
        assert "WARNING" in rendered

    def test_the_shared_record_is_left_for_other_handlers(self) -> None:
        """Sanitizing renders a copy: a second handler still sees the record as sent."""
        record = self._make_record("refused %s", f"a{_FORGED_LINE}b")
        SanitizingFormatter(_LOG_FORMAT, _DATE_FORMAT).format(record)

        assert "\r\n" in record.getMessage()
        assert record.args is not None

    def test_a_traceback_keeps_its_own_line_breaks(self) -> None:
        """Only the composed message is escaped; traceback bodies stay legible."""
        try:
            raise ValueError("schema fetch failed")
        except ValueError:
            record = self._make_record("preparing arguments for %s failed", f"tool{_FORGED_LINE}", level=logging.ERROR)
            record.exc_info = sys.exc_info()

        rendered = SanitizingFormatter(_LOG_FORMAT, _DATE_FORMAT).format(record)

        assert rendered.count("\n") > 1, "the traceback was flattened"
        assert "ValueError" in rendered
        assert "\\r\\n" in rendered, "the message itself was not escaped"


class TestConfigureLogging:
    """The server's own entrypoint must install the formatter, not the stock one."""

    def test_configure_logging_installs_the_sanitizing_formatter(self) -> None:
        with patch("logging.basicConfig") as mock_basic_config:
            configure_logging()

        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.INFO
        handlers = kwargs["handlers"]
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, SanitizingFormatter)


def _write_one_warning_record_through(formatter: logging.Formatter, *, sanitized_inputs: bool) -> str:
    """Emit the probe's record through a real handler and return what hit the stream.

    ``sanitized_inputs=False`` reproduces a site that interpolates the raw values
    straight into the message — which is what the cited sites did before the fix,
    and what any future site can do again — so the formatter's own contribution
    is measured separately from the helpers'.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger = logging.getLogger("nextdns_mcp.transport")

    url = f"https://api.nextdns.io/profiles/abc%0a{_FORGED_ESCAPE}"
    error = ValueError(f"ACL denied for https://api.nextdns.io/profiles/abc%0a{_FORGED_LINE}")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        # The exact call shape of ``transport._api_request``'s denial line.
        if sanitized_inputs:
            logged_url, logged_error = client._redacted(url), client._log_safe_error(error)
        else:
            logged_url, logged_error = url, str(error)
        logger.warning("Access denied in %s %s: %s", "GET", logged_url, logged_error)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return stream.getvalue()


class TestRecordForgingReplay:
    """The blocked-step reproduction, run as a test."""

    def test_one_record_from_a_forged_url_reaches_the_stream_as_one_line(self) -> None:
        written = _write_one_warning_record_through(
            SanitizingFormatter(_LOG_FORMAT, _DATE_FORMAT), sanitized_inputs=True
        )

        assert written.endswith("\n")
        assert written.count("\n") == 1, f"one record became {written.count(chr(10))} lines: {written!r}"
        assert "\x1b" not in written
        assert "\\r\\n" in written, "the forgery vanished instead of being escaped"

    def test_a_record_that_interpolates_raw_values_is_still_one_line(self) -> None:
        """The formatter is the floor: it holds even where a site skips the helpers."""
        written = _write_one_warning_record_through(
            SanitizingFormatter(_LOG_FORMAT, _DATE_FORMAT), sanitized_inputs=False
        )

        assert written.count("\n") == 1
        assert "\x1b" not in written

    def test_the_stock_formatter_produces_the_forged_second_line(self) -> None:
        """The negative control: at the pre-fix shape the same record still forges."""
        written = _write_one_warning_record_through(
            logging.Formatter(_LOG_FORMAT, _DATE_FORMAT), sanitized_inputs=False
        )

        assert written.count("\n") > 1
        assert "\x1b" in written


class TestExistingRedactionIsPreserved:
    """issue #139's contract survives the escaping layered on top of it."""

    def test_redacted_still_strips_the_query_string(self) -> None:
        assert client._redacted("https://api.nextdns.io/profiles/abc123/logs?search=secret") == (
            "https://api.nextdns.io/profiles/abc123/logs"
        )

    def test_redacted_escapes_a_control_character_in_the_path(self) -> None:
        out = client._redacted(f"https://api.nextdns.io/profiles/abc{_FORGED_LINE}?search=secret")

        assert "secret" not in out
        assert not CONTROL_CHARS_PATTERN.search(out)

    def test_log_safe_error_still_logs_a_status_error_as_status_only(self) -> None:
        request = httpx.Request("GET", "https://api.nextdns.io/profiles/abc123/logs?search=secret")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError(
            "Client error '403 Forbidden' for url 'https://api.nextdns.io/profiles/abc123/logs?search=secret'",
            request=request,
            response=response,
        )

        out = client._log_safe_error(error)

        assert out == "403 Forbidden"

    def test_log_safe_error_escapes_an_upstream_reason_phrase(self) -> None:
        request = httpx.Request("GET", "https://api.nextdns.io/profiles/abc123")
        # The reason phrase is what the upstream status line said, so it is the
        # one part of the status branch that is not this server's own text. It
        # arrives as response extensions, which is exactly how a malicious or
        # broken upstream puts control characters in it.
        response = httpx.Response(400, request=request, extensions={"reason_phrase": b"Bad\r\nCRITICAL - forged"})
        error = httpx.HTTPStatusError("upstream status line", request=request, response=response)

        out = client._log_safe_error(error)

        assert not CONTROL_CHARS_PATTERN.search(out)
        assert out.startswith("400 ")

    def test_log_safe_error_still_names_a_non_status_failure(self) -> None:
        out = client._log_safe_error(ValueError(f"ACL denied for https://api.nextdns.io/p/abc{_FORGED_LINE}"))

        assert "ACL denied" in out
        assert not CONTROL_CHARS_PATTERN.search(out)


class TestCitedCallSites:
    """The sites the finding names route their caller-controlled value through the sanitizer."""

    def test_read_denied_logs_an_escaped_profile_id(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tools.doh"):
            payload = doh._read_denied(f"abc123{_FORGED_LINE}")

        # The caller-facing payload keeps the value verbatim; only the record is escaped.
        assert f"abc123{_FORGED_LINE}" in payload["error"]
        assert any("Read access denied for profile: abc123\\r\\n" in record.getMessage() for record in caplog.records)
        for record in caplog.records:
            assert not CONTROL_CHARS_PATTERN.search(record.getMessage())

    @pytest.mark.asyncio
    async def test_invalid_record_type_logs_the_escaped_value(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``record_type`` is free text from the caller, so its rejection line is the vector."""
        monkeypatch.setenv("NEXTDNS_READABLE_PROFILES", "ALL")
        with caplog.at_level(logging.WARNING, logger="nextdns_mcp.tools.doh"):
            payload = await doh._dohLookup_impl("example.com", "abc123", f"A{_FORGED_LINE}")

        assert payload["code"] == "invalid_record_type"
        messages = [record.getMessage() for record in caplog.records]
        assert any("Invalid DNS record type requested: A\\r\\n" in msg for msg in messages)
        for msg in messages:
            assert not CONTROL_CHARS_PATTERN.search(msg)

    @pytest.mark.asyncio
    async def test_plot_series_info_line_escapes_its_values(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The metric and profile id on the plot INFO line are caller-controlled too."""

        async def fake_fetch(url: str, params: dict[str, object]) -> tuple[dict[str, object], None]:
            return {}, None

        monkeypatch.setattr(plots, "_fetch_series_payload", fake_fetch)
        monkeypatch.setattr(plots, "_plot_rendered_series", lambda *args: {"ok": True})

        with caplog.at_level(logging.INFO, logger="nextdns_mcp.tools.plots"):
            result = await plots._fetch_plot_series(f"dns_queries{_FORGED_ESCAPE}", f"abc123{_FORGED_LINE}", {})

        assert result == {"ok": True}
        messages = [record.getMessage() for record in caplog.records]
        assert any("Plotting analytics series: dns_queries\\x1b" in msg for msg in messages)
        for msg in messages:
            assert not CONTROL_CHARS_PATTERN.search(msg)

    @pytest.mark.asyncio
    async def test_relaxation_failure_logs_an_escaped_tool_name(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        middleware = StripExtraFieldsMiddleware()

        async def failing_schema_fetch(*args: object) -> dict[str, object]:
            raise RuntimeError("could not read the parameter schema")

        monkeypatch.setattr(middleware, "_tool_parameters", failing_schema_fetch)

        with caplog.at_level(logging.ERROR, logger="nextdns_mcp.relaxation"), pytest.raises(ToolError):
            await middleware._prepare_arguments(
                cast("FastMCP[Any]", SimpleNamespace()), f"getProfile{_FORGED_LINE}", {}
            )

        messages = [record.getMessage() for record in caplog.records]
        assert any("Failed to prepare arguments for tool 'getProfile\\r\\n" in msg for msg in messages)
        for msg in messages:
            assert not CONTROL_CHARS_PATTERN.search(msg)
