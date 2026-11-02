"""Unit tests for the container E2E runner script."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.types import ImageContent, TextContent, Tool

from scripts.run_container_e2e import (
    EXPECTED_TOOLS,
    SPEC_PATH,
    WRITE_CASES,
    ContainerE2ERunner,
    check_health_endpoint,
    parse_args,
)


@pytest.fixture
def runner(tmp_path: Path) -> ContainerE2ERunner:
    """Create a runner instance with a temporary artifacts directory."""
    return ContainerE2ERunner(
        endpoint="http://127.0.0.1:8000/mcp",
        variant="slim",
        allow_live_writes=False,
        plot_profile="",
        artifacts_dir=tmp_path,
    )


def test_expected_tools_match_registered_and_schema_tools():
    """The E2E expectation, the server's published catalog, and the spec name one set (issue #271).

    The list is derived from the document instead of typed out beside it, so what needs
    checking is that the three agree: the document the container is built from, the
    catalog the server publishes from it, and the list the container is graded against.
    """
    from nextdns_mcp.server import published_tool_names
    from scripts.validate_schema import assert_operation_coverage, load_openapi_spec, spec_operations

    spec = load_openapi_spec(str(SPEC_PATH))

    assert set(EXPECTED_TOOLS) == set(published_tool_names())
    # No drift to report, in either direction, against the server's own catalog.
    assert_operation_coverage(spec, published_tool_names())
    # The withheld operation is the only one the document describes and the server lacks.
    assert set(spec_operations(spec)) - set(EXPECTED_TOOLS) == {"streamLogs"}


def test_operation_coverage_fails_when_the_container_lacks_an_operation():
    """A stale image is drift, and it has to fail loudly rather than skip quietly."""
    from scripts.validate_schema import assert_operation_coverage, load_openapi_spec

    spec = load_openapi_spec(str(SPEC_PATH))
    stale = [tool for tool in EXPECTED_TOOLS if tool != "getLogsSettings"]

    with pytest.raises(ValueError, match="spec operations published as no tool.*getLogsSettings"):
        assert_operation_coverage(spec, stale)


def test_operation_coverage_fails_on_a_tool_the_spec_does_not_describe():
    """The grouped tool names are exactly what this surface no longer has.

    A container that still advertises `manageLists` would pass a check written against
    the old surface; here it fails, because no operation in the document is called that.
    """
    from scripts.validate_schema import assert_operation_coverage, load_openapi_spec

    spec = load_openapi_spec(str(SPEC_PATH))

    with pytest.raises(ValueError, match="published tools that name no operation.*manageLists"):
        assert_operation_coverage(spec, [*EXPECTED_TOOLS, "manageLists"])


def test_read_arguments_come_from_the_served_schema(runner: ContainerE2ERunner):
    """The read sweep asks the server what a call needs, and refuses to guess the rest.

    A required argument with no value to send is reported as unfilled rather than
    invented: the run then says which endpoint it could not call, instead of sending
    something the endpoint never asked for and reporting on the answer to that.
    """
    runner.tool_schemas = {
        "getLogs": {"required": ["profile_id", "from"]},
        "getAnalyticsDestinations": {"required": ["profile_id", "from", "type"]},
        "listProfiles": {"required": []},
        "getAnOperationNobodyPlannedFor": {"required": ["widget"]},
    }

    assert runner._read_arguments("getLogs", "abc123") == ({"profile_id": "abc123", "from": "-1d"}, [])
    assert runner._read_arguments("getAnalyticsDestinations", "abc123") == (
        {"profile_id": "abc123", "from": "-1d", "type": "countries"},
        [],
    )
    # A collection endpoint takes no profile, so it is called with nothing.
    assert runner._read_arguments("listProfiles", "abc123") == ({}, [])

    arguments, unfilled = runner._read_arguments("getAnOperationNobodyPlannedFor", "abc123")
    assert arguments == {}
    assert unfilled == ["widget"]


def test_record_result(runner: ContainerE2ERunner, tmp_path: Path):
    """Test that record_result appends valid JSON lines."""
    runner.record_result(
        tool="listProfiles",
        status="OK",
        schema_status="VALID",
        schema_error="",
        args="",
        duration=0.5,
    )

    report_lines = runner.report_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(report_lines) == 1
    data = json.loads(report_lines[0])
    assert data["tool"] == "listProfiles"
    assert data["status"] == "OK"
    assert data["schema_validation"] == "VALID"
    assert data["args"] == ""
    assert data["duration"] == "0.50s"


def test_record_skip(runner: ContainerE2ERunner):
    """Test that record_skip appends skip lines and updates skipped count."""
    runner.record_skip(tool="plotAnalytics", reason="No profile")
    assert runner.skipped_count == 1

    report_lines = runner.report_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(report_lines) == 1
    data = json.loads(report_lines[0])
    assert data["tool"] == "plotAnalytics"
    assert data["status"] == "SKIPPED"
    assert data["reason"] == "No profile"


@pytest.mark.asyncio
async def test_execute_call_success(runner: ContainerE2ERunner):
    """Test successful tool call with JSON response."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [TextContent(type="text", text='{"data":[{"id":"p123","name":"Test"}]}')]
    session.call_tool.return_value = mock_result

    ok, res = await runner.execute_call(session, "listProfiles", {})

    assert ok is True
    assert res == {"data": [{"id": "p123", "name": "Test"}]}
    assert runner.executed_count == 1
    assert runner.failed_count == 0


@pytest.mark.asyncio
async def test_execute_call_error_in_json(runner: ContainerE2ERunner):
    """Test tool call returning an error field in JSON payload."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [TextContent(type="text", text='{"error":"Profile not found"}')]
    session.call_tool.return_value = mock_result

    ok, _ = await runner.execute_call(session, "getProfile", {"profile_id": "bad"})

    assert ok is False
    assert runner.failed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_value", ["", None, 0, [], {}])
async def test_execute_call_error_key_marks_call_failed_even_when_value_is_falsy(
    runner: ContainerE2ERunner, error_value
):
    """The presence of the "error" key decides the verdict, not the value's truthiness.

    This harness exists to spot failures, so a payload carrying an error key with
    a falsy value must still be recorded as FAILED and counted, never as OK.
    """
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [TextContent(type="text", text=json.dumps({"data": "ignored", "error": error_value}))]
    session.call_tool.return_value = mock_result

    ok, _ = await runner.execute_call(session, "getProfile", {"profile_id": "bad"})

    assert ok is False
    assert runner.failed_count == 1
    assert runner.executed_count == 0

    record = json.loads(runner.report_file.read_text(encoding="utf-8").strip())
    assert record["status"] == "FAILED"
    assert "error" in record


@pytest.mark.asyncio
async def test_execute_call_retry_on_exception(runner: ContainerE2ERunner):
    """Test tool call retry on transient exception."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [TextContent(type="text", text='{"data":{"id":"p123"}}')]

    session.call_tool.side_effect = [RuntimeError("temporary error"), mock_result]

    ok, _ = await runner.execute_call(session, "getProfile", {"profile_id": "p123"}, max_retries=2, retry_delay=0.01)

    assert ok is True
    assert runner.executed_count == 1
    assert session.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_execute_call_plot_image_content(runner: ContainerE2ERunner):
    """Test plotAnalytics returns ImageContent."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [ImageContent(type="image", data="base64data==", mimeType="image/png")]
    session.call_tool.return_value = mock_result

    ok, _ = await runner.execute_call(session, "plotAnalytics", {"metric": "status"})

    assert ok is True
    assert runner.executed_count == 1


@pytest.mark.asyncio
async def test_check_endpoint_readiness_success(runner: ContainerE2ERunner):
    """Test endpoint readiness check succeeding."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        ready = await runner.check_endpoint_readiness(max_attempts=1)
        assert ready is True


@pytest.mark.asyncio
async def test_check_endpoint_readiness_failure(runner: ContainerE2ERunner):
    """Test endpoint readiness check failing."""
    with patch("httpx.AsyncClient.get", side_effect=RuntimeError("connection refused")):
        ready = await runner.check_endpoint_readiness(max_attempts=1)
        assert ready is False


@pytest.mark.asyncio
async def test_run_preflight_fails_on_missing_tools(runner: ContainerE2ERunner):
    """Test that runner fails preflight when tools are missing."""
    mock_tools_res = MagicMock()
    mock_tools_res.tools = [Tool(name="dohLookup", description="", inputSchema={})]

    mock_session = AsyncMock()
    mock_session.list_tools.return_value = mock_tools_res

    mock_read = AsyncMock()
    mock_write = AsyncMock()

    with (
        patch.object(runner, "check_endpoint_readiness", return_value=True),
        patch.object(runner, "check_health", return_value=True),
        patch("scripts.run_container_e2e.streamable_http_client") as mock_client,
        patch("scripts.run_container_e2e.ClientSession") as mock_session_cls,
    ):
        mock_client.return_value.__aenter__.return_value = (mock_read, mock_write)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        exit_code = await runner.run()
        assert exit_code == 1


@pytest.mark.asyncio
async def test_run_success_read_only(runner: ContainerE2ERunner):
    """Test a complete read-only run with mocked session.

    The plot sweep needs a data-bearing profile to target; without one the
    nine plotAnalytics calls record skips, and skips fail the run (a call
    that never executed certifies nothing), so this run gets one.
    """
    runner.plot_profile = "analytics-profile"
    tools_list = [Tool(name=t, description="", inputSchema={}) for t in EXPECTED_TOOLS]
    mock_tools_res = MagicMock()
    mock_tools_res.tools = tools_list

    mock_session = AsyncMock()
    mock_session.list_tools.return_value = mock_tools_res

    # Each read answers with a document its own schema describes: getProfile is one
    # profile rather than a list, and downloadLogs answers with the receipt for the file
    # it streamed rather than with the CSV.
    answers = {
        "getProfile": '{"id":"test-profile-id","name":"E2E"}',
        "downloadLogs": json.dumps(
            {
                "file_path": "/tmp/nextdns_logs_x/download.csv",
                "content_type": "text/csv",
                "size": 12,
                "row_count": 1,
                "preview": {"text": "a.com,blocked\n", "line_count": 1, "bytes": 14, "truncated": False},
            }
        ),
    }

    def _call_tool(name, arguments):
        mock_res = MagicMock()
        mock_res.is_error = False
        if name in answers:
            mock_res.content = [TextContent(type="text", text=answers[name])]
            return mock_res
        if name == "dohLookup":
            mock_res.content = [
                TextContent(
                    type="text",
                    text='{"data":{"Status":0},"_metadata":{"profile_id":"test-profile-id",'
                    '"query_domain":"example.com","query_type":"A",'
                    '"doh_endpoint":"https://dns.nextdns.io/test-profile-id/dns-query?name=example.com&type=A",'
                    '"status_description":"NOERROR"}}',
                )
            ]
        elif name == "plotAnalytics":
            # The runner treats a plot answer with no image content as a failure,
            # so the mocked sweep has to return the image the real tool returns.
            mock_res.content = [ImageContent(type="image", data="AA", mimeType="image/png")]
        else:
            mock_res.content = [TextContent(type="text", text='{"data":[{"id":"test-profile-id"}]}')]
        return mock_res

    mock_session.call_tool.side_effect = _call_tool

    mock_read = AsyncMock()
    mock_write = AsyncMock()

    with (
        patch.object(runner, "check_endpoint_readiness", return_value=True),
        patch.object(runner, "check_health", return_value=True),
        patch("scripts.run_container_e2e.streamable_http_client") as mock_client,
        patch("scripts.run_container_e2e.ClientSession") as mock_session_cls,
    ):
        mock_client.return_value.__aenter__.return_value = (mock_read, mock_write)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        exit_code = await runner.run()
        assert exit_code == 0
        assert runner.failed_count == 0
        assert runner.executed_count > 0


# Independent oracle for the live write phase: the tools the runner must call, in
# order, each with the argument that names its entry and the value it must carry.
# (tool, entry-argument name, entry value or a regex for the timestamped ones.) Kept
# hardcoded rather than derived from WRITE_CASES, so a bad edit to that table fails
# here instead of agreeing with itself.
WRITE_ORACLE: list[tuple[str, str | None, str | None]] = [
    ("updateProfile", None, None),
    ("updateSettings", None, None),
    ("updateLogsSettings", None, None),
    ("updateBlockPageSettings", None, None),
    ("updatePerformanceSettings", None, None),
    ("updatePrivacySettings", None, None),
    ("updateSecuritySettings", None, None),
    ("updateParentalControlSettings", None, None),
    ("addToDenylist", "id", r"e2e-\d+-deny\.example\.com"),
    ("updateDenylistEntry", "entry_id", r"e2e-\d+-deny\.example\.com"),
    ("removeFromDenylist", "entry_id", r"e2e-\d+-deny\.example\.com"),
    ("addToAllowlist", "id", r"e2e-\d+-allow\.example\.com"),
    ("updateAllowlistEntry", "entry_id", r"e2e-\d+-allow\.example\.com"),
    ("removeFromAllowlist", "entry_id", r"e2e-\d+-allow\.example\.com"),
    ("addSecurityTLD", "id", "zip"),
    ("removeSecurityTLD", "entry_id", "zip"),
    ("addPrivacyNative", "id", "apple"),
    ("removePrivacyNative", "entry_id", "apple"),
    ("addToParentalControlCategories", "id", "gambling"),
    ("updateParentalControlCategoryEntry", "id", "gambling"),
    ("removeFromParentalControlCategories", "id", "gambling"),
    ("addToParentalControlServices", "id", "tiktok"),
    ("updateParentalControlServiceEntry", "id", "tiktok"),
    ("removeFromParentalControlServices", "id", "tiktok"),
    ("addPrivacyBlocklist", "id", "nextdns-recommended"),
    ("removePrivacyBlocklist", "entry_id", "nextdns-recommended"),
    # Whole-array replacements, each fill-then-clear (ARCH-3 execution coverage).
    ("replaceDenylist", None, None),
    ("replaceDenylist", None, None),
    ("replaceAllowlist", None, None),
    ("replaceAllowlist", None, None),
    ("replaceSecurityTLDs", None, None),
    ("replaceSecurityTLDs", None, None),
    ("replacePrivacyNatives", None, None),
    ("replacePrivacyNatives", None, None),
    ("replacePrivacyBlocklists", None, None),
    ("replacePrivacyBlocklists", None, None),
    ("replaceParentalControlCategories", None, None),
    ("replaceParentalControlCategories", None, None),
    ("replaceParentalControlServices", None, None),
    ("replaceParentalControlServices", None, None),
]

# The three calls that cannot live in the table: a rewrite's id comes back only from
# the call that adds it, and clearing the logs is the last thing a write run does.
WRITE_TAIL = ["addRewrite", "deleteRewrite", "clearLogs"]


def test_write_cases_are_the_documented_sequence():
    """WRITE_CASES is the single source of truth for the write phase; the oracle says so."""
    assert [name for name, _ in WRITE_CASES] == [name for name, _, _ in WRITE_ORACLE]


class TestE2EWorkflowSecretHandling:
    """Guards on .github/workflows/e2e-container.yml (SEC-5 and SEC-7).

    The live run must not put the real credential on a command line (it is
    readable through /proc/<pid>/cmdline; the script reads the environment
    already), and the live container must run with the data-bearing plot
    profile on the write denylist, the way the actor harness protects it.
    """

    WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "e2e-container.yml"

    def _steps(self) -> dict:
        import yaml

        doc = yaml.safe_load(self.WORKFLOW.read_text(encoding="utf-8"))
        return {step.get("name", ""): step for step in doc["jobs"]["e2e"]["steps"]}

    def test_the_live_run_never_puts_the_real_key_on_the_command_line(self):
        step = next(s for name, s in self._steps().items() if name.startswith("Run E2E validation"))
        assert '--api-key "${NEXTDNS_API_KEY}"' not in step["run"]
        # The env it needs is still there: the script's --api-key default reads it.
        assert "NEXTDNS_API_KEY" in step["env"]

    def test_the_health_run_may_still_pass_its_fake_key(self):
        """The health-only probe uses a fake literal credential, not the secret."""
        step = next(s for name, s in self._steps().items() if name.startswith("Validate /health"))
        assert "--api-key" in step["run"]
        assert "NEXTDNS_API_KEY" not in step.get("env", {})

    def test_the_live_container_denies_writes_to_the_plot_profile(self):
        step = next(s for name, s in self._steps().items() if name.startswith("Start MCP Server Container"))
        assert "NEXTDNS_PLOT_PROFILE" in step["env"]
        # Conditional on the secret: an empty denylist would protect nothing but
        # an id literally named "", so the flag is added only when set.
        assert 'NEXTDNS_PROTECTED_PROFILES="${NEXTDNS_PLOT_PROFILE}"' in step["run"]
        assert 'if [ -n "${NEXTDNS_PLOT_PROFILE}" ]' in step["run"]


def test_settings_write_payloads_are_the_shared_table():
    """The seven settings payloads are SETTINGS_CATEGORIES' `set` payloads verbatim.

    This script used to restate them with its own values and had already drifted
    (logs retention 86400 vs the table's 604800, blockpage enabled True vs the
    table's False). The writer-tool mapping is this script's own (the category is
    the endpoint), but the payload is imported, so the two suites drive the same
    writes from one table.
    """
    from scripts import ai_e2e_common

    by_tool = dict(WRITE_CASES)
    for cat, tool in (
        ("general", "updateSettings"),
        ("logs", "updateLogsSettings"),
        ("blockpage", "updateBlockPageSettings"),
        ("performance", "updatePerformanceSettings"),
        ("privacy", "updatePrivacySettings"),
        ("security", "updateSecuritySettings"),
        ("parental", "updateParentalControlSettings"),
    ):
        expected = {"profile_id": "{profile}", **ai_e2e_common.SETTINGS_CATEGORIES[cat]["set"]}
        assert by_tool[tool] == expected, f"{cat}: the {tool} payload drifted from SETTINGS_CATEGORIES"


def test_plot_sweep_walks_the_server_metric_table(tmp_path: Path):
    """The plot sweep iterates the server's own nine chartable metrics, not a copy."""
    from scripts import ai_e2e_common

    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data": []}')]
    session.call_tool.return_value = mock_call_res

    runner = ContainerE2ERunner(
        endpoint="http://127.0.0.1:8000/mcp",
        variant="slim",
        allow_live_writes=True,
        plot_profile="plotpid",
        artifacts_dir=tmp_path,
    )
    asyncio.run(runner._run_plot_sweep(session, "plotpid"))
    metrics = [call.kwargs["arguments"]["metric"] for call in session.call_tool.call_args_list]
    assert metrics == list(ai_e2e_common.PLOT_METRICS)
    assert len(metrics) == 9


def test_load_spec_once_parses_each_path_once(tmp_path: Path, monkeypatch):
    """The shipped document is parsed once per path, not once per consumer.

    The catalog and the runner both used to load it at their own turn (~160 ms
    each); the memo keeps one parse while a monkeypatched SPEC_PATH still gets
    its own fresh parse rather than a stale memo.
    """
    from scripts import run_container_e2e as rce

    calls = {"n": 0}
    real_load = rce.load_openapi_spec

    def counting_load(path: str):
        calls["n"] += 1
        return real_load(path)

    monkeypatch.setattr(rce, "load_openapi_spec", counting_load)
    spec_a = tmp_path / "a.yaml"
    spec_a.write_text("paths: {}\n", encoding="utf-8")
    spec_b = tmp_path / "b.yaml"
    spec_b.write_text("paths: {}\n", encoding="utf-8")

    first = rce._load_spec_once(spec_a)
    second = rce._load_spec_once(spec_a)
    assert first is second  # memo hit, no second parse
    other = rce._load_spec_once(spec_b)
    assert other is not first  # a different path reparses instead of serving a stale memo
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_run_write_checks_issues_the_documented_sequence(runner: ContainerE2ERunner):
    """Each entry is added, changed where it has its own endpoint, and removed again.

    The settings writes are one per settings endpoint: the document has no single
    settings call to update, and a runner that invented one would report a pass for an
    endpoint that does not exist.
    """
    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":{"id":"rewrite-1"}}')]
    session.call_tool.return_value = mock_call_res

    await runner._run_write_checks(session, "profile-123")

    calls = [(call.args[0], call.kwargs["arguments"]) for call in session.call_tool.call_args_list]
    assert [name for name, _ in calls] == [name for name, _, _ in WRITE_ORACLE] + WRITE_TAIL

    for (name, arguments), (_, entry_name, pattern) in zip(calls, WRITE_ORACLE):
        assert arguments["profile_id"] == "profile-123", name
        if entry_name is None:
            continue
        value = arguments[entry_name]
        assert re.fullmatch(pattern, value), f"{name}: {value!r} does not match {pattern}"

    # The settings writes carry their own fields rather than a wrapped settings
    # object. Their payloads come from the shared SETTINGS_CATEGORIES table now —
    # this pinned the drift the table prevents (retention 86400 here vs 604800
    # there, blockpage enabled True vs False), so the values are the table's.
    settings = dict(calls[1:8])
    assert settings["updateSettings"] == {"profile_id": "profile-123", "web3": True}
    assert settings["updateLogsSettings"]["retention"] == 604800
    assert settings["updateBlockPageSettings"] == {"profile_id": "profile-123", "enabled": False}
    assert settings["updatePerformanceSettings"] == {
        "profile_id": "profile-123",
        "ecs": True,
        "cacheBoost": True,
    }

    # The rewrite is deleted with the id the add returned, which is why it is not in
    # the table: no static argument list can name it.
    rewrite_add = next(arguments for name, arguments in calls if name == "addRewrite")
    rewrite_delete = next(arguments for name, arguments in calls if name == "deleteRewrite")
    assert rewrite_delete == {"profile_id": "profile-123", "entry_id": "rewrite-1"}
    assert rewrite_add["name"].startswith("e2e-") and rewrite_add["content"] == "192.0.2.1"


@pytest.mark.asyncio
async def test_every_write_that_adds_an_entry_also_removes_it(runner: ContainerE2ERunner):
    """A live write phase has to leave the profile as it found it.

    Entries left behind change what the next run reads, and a check that alters the
    account cannot be run twice to find out whether it passes. The pairing is checked
    by name and by entry, so an add whose removal was renamed or pointed at a different
    entry fails here.
    """
    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":{"id":"rewrite-1"}}')]
    session.call_tool.return_value = mock_call_res

    await runner._run_write_checks(session, "profile-123")

    calls = [(call.args[0], call.kwargs["arguments"]) for call in session.call_tool.call_args_list]

    def entry(arguments: dict) -> str:
        return str(arguments.get("entry_id") or arguments.get("id") or "")

    removed = {name: {entry(arguments) for name_, arguments in calls if name_ == name} for name, _ in calls}

    for name, arguments in calls:
        if not name.startswith("add") or name == "addRewrite":
            # The rewrite pair is asserted separately: its removal names the id the add
            # returned, which no static argument list can know.
            continue
        # `addToX` has an endpoint called `removeFromX`; `addSecurityTLD` has
        # `removeSecurityTLD`. Both spellings exist in this document, so both are tried.
        partner = "removeFrom" + name[len("addTo") :] if name.startswith("addTo") else "remove" + name[len("add") :]
        assert partner in removed, f"{name} has no endpoint that removes its entry"
        assert entry(arguments) in removed[partner], f"{name} adds {entry(arguments)}, {partner} removes none of them"


@pytest.mark.asyncio
async def test_run_write_checks_skips_the_rewrite_delete_when_the_add_returns_no_id(runner: ContainerE2ERunner):
    """No id back means nothing to delete, and the run says so instead of calling blindly.

    A delete with a made-up entry id would answer 404 and read as an endpoint failure
    when the real gap is that the add gave nothing back.
    """
    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":{"name":"no id here"}}')]
    session.call_tool.return_value = mock_call_res

    await runner._run_write_checks(session, "profile-123")

    called = [call.args[0] for call in session.call_tool.call_args_list]
    assert "deleteRewrite" not in called
    assert "clearLogs" in called
    skips = runner.report_file.read_text(encoding="utf-8")
    assert "deleteRewrite" in skips and "no id" in skips


@pytest.mark.asyncio
async def test_read_checks_cover_every_read_the_spec_describes(runner: ContainerE2ERunner):
    """The sweep is the document's read list, so a new operation is covered by the next run.

    Without this, an endpoint added to the spec would sit untested until someone
    remembered to add it to a list in this script, which is the maintenance the
    generated surface was adopted to remove.
    """
    from scripts.validate_schema import WITHHELD_OPERATIONS, load_openapi_spec, spec_operations

    spec = load_openapi_spec(str(SPEC_PATH))
    reads = {name for name, method in spec_operations(spec).items() if method == "get"} - set(WITHHELD_OPERATIONS)
    assert reads, "the shipped spec describes no reads"

    runner.tool_schemas = {name: {"required": ["profile_id"]} for name in reads}

    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":[]}')]
    session.call_tool.return_value = mock_call_res

    await runner._run_read_checks(session, "profile-123", "plot-profile")

    called = [call.args[0] for call in session.call_tool.call_args_list]
    assert set(called) == reads | {"dohLookup"}
    assert runner.failed_count == 0


@pytest.mark.asyncio
async def test_an_unplanned_required_argument_is_recorded_as_a_skip(runner: ContainerE2ERunner):
    """An endpoint whose required argument nobody has a value for is reported, not guessed.

    The schema the container serves is the truth about what an endpoint needs, so a
    required argument this script has no value for is a gap in the run: skipping it by
    name says which endpoint went untested, where inventing a value would quietly
    report on something other than the endpoint.
    """
    from scripts.validate_schema import WITHHELD_OPERATIONS, load_openapi_spec, spec_operations

    spec = load_openapi_spec(str(SPEC_PATH))
    reads = {name for name, method in spec_operations(spec).items() if method == "get"} - set(WITHHELD_OPERATIONS)
    runner.tool_schemas = {name: {"required": ["profile_id"]} for name in reads}
    runner.tool_schemas["getLogs"] = {"required": ["profile_id", "aWindowNobodyNamed"]}

    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":[]}')]
    session.call_tool.return_value = mock_call_res

    await runner._run_read_checks(session, "profile-123", "")

    called = [call.args[0] for call in session.call_tool.call_args_list]
    assert "getLogs" not in called
    assert "getDenylist" in called
    report = runner.report_file.read_text(encoding="utf-8")
    assert "aWindowNobodyNamed" in report


@pytest.mark.asyncio
async def test_an_operation_the_container_does_not_publish_is_skipped(runner: ContainerE2ERunner):
    """The spec can describe more than a stale container serves; the run says which."""
    from scripts.validate_schema import WITHHELD_OPERATIONS, load_openapi_spec, spec_operations

    spec = load_openapi_spec(str(SPEC_PATH))
    reads = {name for name, method in spec_operations(spec).items() if method == "get"} - set(WITHHELD_OPERATIONS)
    runner.tool_schemas = {name: {"required": ["profile_id"]} for name in reads - {"getLogs"}}

    session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":[]}')]
    session.call_tool.return_value = mock_call_res

    await runner._run_read_checks(session, "profile-123", "")

    called = [call.args[0] for call in session.call_tool.call_args_list]
    assert "getLogs" not in called
    assert "not published" in runner.report_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_stops_when_preflight_fails(runner: ContainerE2ERunner):
    """A failed preflight returns 1 without provisioning a profile."""
    mock_session = AsyncMock()
    mock_preflight = AsyncMock(return_value=False)

    with (
        patch.object(runner, "check_endpoint_readiness", return_value=True),
        patch.object(runner, "check_health", return_value=True),
        patch("scripts.run_container_e2e.streamable_http_client") as mock_client,
        patch("scripts.run_container_e2e.ClientSession") as mock_session_cls,
        patch.object(runner, "_preflight", new=mock_preflight),
        patch.object(runner, "_provision_profile") as mock_provision,
    ):
        mock_client.return_value.__aenter__.return_value = (AsyncMock(), AsyncMock())
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        exit_code = await runner.run()

    assert exit_code == 1
    mock_preflight.assert_called_once()
    mock_provision.assert_not_called()


@pytest.mark.asyncio
async def test_run_stops_when_profile_provisioning_fails(runner: ContainerE2ERunner):
    """Failed provisioning returns 1 without running any check phase."""
    mock_session = AsyncMock()
    mock_preflight = AsyncMock(return_value=True)
    mock_provision = AsyncMock(return_value=("", ""))

    with (
        patch.object(runner, "check_endpoint_readiness", return_value=True),
        patch.object(runner, "check_health", return_value=True),
        patch("scripts.run_container_e2e.streamable_http_client") as mock_client,
        patch("scripts.run_container_e2e.ClientSession") as mock_session_cls,
        patch.object(runner, "_preflight", new=mock_preflight),
        patch.object(runner, "_provision_profile", new=mock_provision),
        patch.object(runner, "_run_read_checks") as mock_read_checks,
    ):
        mock_client.return_value.__aenter__.return_value = (AsyncMock(), AsyncMock())
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        exit_code = await runner.run()

    assert exit_code == 1
    mock_read_checks.assert_not_called()


def test_parse_args(monkeypatch):
    """Test argument parsing defaults and overrides."""
    monkeypatch.setenv("MCP_ENDPOINT", "http://test:8000/mcp")
    monkeypatch.setenv("ALLOW_LIVE_WRITES", "true")
    monkeypatch.setenv("VARIANT", "alpine")

    with patch("sys.argv", ["run_container_e2e.py"]):
        args = parse_args()
        assert args.endpoint == "http://test:8000/mcp"
        assert args.variant == "alpine"
        assert args.allow_live_writes is True


def _health_response(status_code: int, payload: str) -> MagicMock:
    """Build a mock httpx response carrying a JSON body."""
    response = MagicMock()
    response.status_code = status_code
    response.text = payload
    response.json.return_value = json.loads(payload)
    return response


HEALTH_URL = "http://127.0.0.1:8000/health"
API_KEY = "e2e-invalid-key-not-real"


def test_check_health_endpoint_accepts_ready_200():
    """A healthy probe returning exactly {"status": "ok"} passes."""
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(200, '{"status": "ok"}')
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=True)

    assert passed is True
    assert "200" in detail


def test_check_health_endpoint_rejects_health_body_with_extra_fields():
    """A 200 carrying anything beyond {"status": "ok"} is not the documented shape."""
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(200, '{"status": "ok", "extra": 1}')
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=True)

    assert passed is False
    assert "expected" in detail


def test_check_health_endpoint_requires_200_when_credentials_are_expected_usable():
    """Expecting readiness but getting 503 is a contract failure, not a pass."""
    body = '{"status": "error", "class": "auth", "reason": "rejected (HTTP 401)"}'
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, body)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=True)

    assert passed is False
    assert "expected HTTP 200" in detail


@pytest.mark.parametrize(
    ("failure_class", "reason"),
    [
        ("auth", "NextDNS API rejected the configured credentials (HTTP 401)"),
        ("unreachable", "NextDNS API did not respond within 5s"),
    ],
)
def test_check_health_endpoint_accepts_503_with_class_and_reason(failure_class, reason):
    """A 503 carrying a documented failure class and a reason passes."""
    payload = json.dumps({"status": "error", "class": failure_class, "reason": reason})
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, payload)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is True
    assert failure_class in detail


def test_check_health_endpoint_rejects_503_with_unknown_class():
    """A 503 whose class is outside the documented set is a failure."""
    payload = '{"status": "error", "class": "banana", "reason": "why not"}'
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, payload)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert "banana" in detail


@pytest.mark.parametrize("reason", ["", "   "])
def test_check_health_endpoint_rejects_503_without_a_reason(reason):
    """A 503 with an empty reason gives an operator nothing to act on."""
    payload = json.dumps({"status": "error", "class": "auth", "reason": reason})
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, payload)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert "reason" in detail


def test_check_health_endpoint_rejects_503_with_wrong_status_field():
    """A 503 that is not shaped as an error payload is a failure."""
    payload = '{"status": "ok"}'
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, payload)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert 'status "error"' in detail


def test_check_health_endpoint_rejects_200_when_failure_is_expected():
    """A 200 on the invalid-credential server means the probe stopped being real."""
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(200, '{"status": "ok"}')
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert "expected HTTP 503" in detail


def test_check_health_endpoint_fails_when_the_response_leaks_the_key():
    """A body echoing the API key back is a hard failure, not a pass."""
    payload = json.dumps({"status": "error", "class": "auth", "reason": f"bad key {API_KEY}"})
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, payload)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert "leaked the API key" in detail


def test_check_health_endpoint_rejects_non_json_body():
    """A non-JSON body means something other than the server answered /health."""
    response = MagicMock()
    response.status_code = 200
    response.text = "<html>nginx</html>"
    response.json.side_effect = ValueError("not json")

    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = response
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=True)

    assert passed is False
    assert "non-JSON" in detail


def test_check_health_endpoint_reports_transport_failure():
    """An unreachable server is reported rather than raised."""
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.side_effect = httpx.ConnectError("connection refused")
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=True)

    assert passed is False
    assert "ConnectError" in detail


def test_runner_health_url_is_derived_from_the_mcp_endpoint(runner):
    """Without an explicit --health-url, /health is derived from the /mcp endpoint."""
    assert runner.health_url == "http://127.0.0.1:8000/health"


def test_runner_health_url_honors_explicit_override(tmp_path):
    """An explicit health URL overrides the derived one."""
    runner = ContainerE2ERunner(
        endpoint="http://127.0.0.1:8000/mcp",
        variant="slim",
        allow_live_writes=False,
        plot_profile="",
        artifacts_dir=tmp_path,
        health_url="http://127.0.0.1:8001/health",
    )

    assert runner.health_url == "http://127.0.0.1:8001/health"


def test_runner_check_health_reports_failure(runner):
    """check_health() returns False when the served endpoint violates the contract."""
    with patch("scripts.run_container_e2e.check_health_endpoint", return_value=(False, "boom")) as mock_check:
        assert runner.check_health() is False

    mock_check.assert_called_once_with(runner.health_url, runner.api_key, runner.expect_health_ok)


def test_runner_check_health_reports_success(runner):
    """check_health() returns True when the served endpoint conforms."""
    with patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, "fine")) as mock_check:
        assert runner.check_health() is True

    mock_check.assert_called_once_with(runner.health_url, runner.api_key, runner.expect_health_ok)


async def test_health_only_mode_skips_the_mcp_tool_suite(tmp_path):
    """--health-only validates /health and returns without running the tool suite."""
    runner = ContainerE2ERunner(
        endpoint="http://127.0.0.1:8000/mcp",
        variant="slim",
        allow_live_writes=False,
        plot_profile="",
        artifacts_dir=tmp_path,
        health_only=True,
    )

    with (
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, "ok")) as mock_check,
        patch.object(runner, "check_endpoint_readiness") as mock_ready,
    ):
        exit_code = await runner.run()

    assert exit_code == 0
    mock_check.assert_called_once()
    mock_ready.assert_not_called()


async def test_run_waits_for_the_mcp_endpoint_before_probing_health(runner):
    """The retrying /mcp wait runs first, so the single-shot health probe never races startup."""
    order: list[str] = []

    async def _ready(*_args, **_kwargs) -> bool:
        order.append("readiness")
        return True

    def _health(*_args, **_kwargs) -> tuple[bool, str]:
        order.append("health")
        return False, "stop here"

    with (
        patch.object(runner, "check_endpoint_readiness", side_effect=_ready),
        patch("scripts.run_container_e2e.check_health_endpoint", side_effect=_health),
    ):
        assert await runner.run() == 1

    assert order == ["readiness", "health"]


async def test_health_failure_aborts_the_mcp_tool_suite(runner):
    """A failing /health check returns non-zero without running the tool suite."""
    with (
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(False, "boom")),
        patch.object(runner, "check_endpoint_readiness", return_value=True) as mock_ready,
        patch("scripts.run_container_e2e.streamable_http_client") as mock_client,
    ):
        exit_code = await runner.run()

    assert exit_code == 1
    mock_ready.assert_called_once()
    mock_client.assert_not_called()


def test_parse_args_health_defaults(monkeypatch):
    """Health arguments default to deriving /health and expecting a 200."""
    monkeypatch.setenv("MCP_ENDPOINT", "http://test:8000/mcp")
    for var in ("HEALTH_URL", "API_KEY_UNUSED", "EXPECT_HEALTH", "HEALTH_ONLY"):
        monkeypatch.delenv(var, raising=False)

    with patch("sys.argv", ["run_container_e2e.py"]):
        args = parse_args()
        assert args.health_url == ""
        assert args.expect_health == "ok"
        assert args.health_only is False


def test_parse_args_health_overrides(monkeypatch):
    """Health arguments are overridable from the command line and the environment."""
    monkeypatch.setenv("HEALTH_URL", "http://env:8000/health")
    monkeypatch.setenv("EXPECT_HEALTH", "not-ok")
    monkeypatch.setenv("HEALTH_ONLY", "true")

    with patch("sys.argv", ["run_container_e2e.py", "--expect-health", "ok"]):
        args = parse_args()
        assert args.health_url == "http://env:8000/health"
        assert args.health_only is True
        # The command line wins over the environment.
        assert args.expect_health == "ok"


def test_runner_startup_fails_when_the_document_and_the_catalog_disagree(tmp_path: Path, monkeypatch):
    """Grading a container against a document that does not describe its tools is an error.

    A run whose spec and catalog disagree cannot report anything useful: the drift has
    to stop the run at startup rather than degrade to SKIPPED, which the workflow counts
    as neither pass nor fail.
    """
    empty_spec = tmp_path / "empty-spec.yaml"
    empty_spec.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("scripts.run_container_e2e.SPEC_PATH", empty_spec)

    with pytest.raises(ValueError, match="published tools that name no operation"):
        ContainerE2ERunner(
            endpoint="http://127.0.0.1:8000/mcp",
            variant="slim",
            allow_live_writes=False,
            plot_profile="",
            artifacts_dir=tmp_path,
        )


def test_runner_startup_fails_when_a_published_read_has_no_response_schema(tmp_path: Path, monkeypatch):
    """A read with no response schema in the document would validate as SKIPPED forever.

    The document is what tells the run what a tool's answer should look like; a read it
    describes without a schema is the silent gap this check exists to close.
    """
    spec = tmp_path / "one-read.yaml"
    spec.write_text(
        "paths:\n  /profiles/{profile_id}/logs:\n    get:\n      operationId: getLogs\n      responses:\n        '200': {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.run_container_e2e.SPEC_PATH", spec)
    monkeypatch.setattr("scripts.run_container_e2e.EXPECTED_TOOLS", ["getLogs"])

    with pytest.raises(ValueError, match="published reads that resolve to no response schema"):
        ContainerE2ERunner(
            endpoint="http://127.0.0.1:8000/mcp",
            variant="slim",
            allow_live_writes=False,
            plot_profile="",
            artifacts_dir=tmp_path,
        )


class TestExecutionCoverageIsClosed:
    """ARCH-3: published write operations must be executed or excluded by name.

    Publication coverage alone cannot fail a whole-array replacement that no
    case ever sends, so the gate is execution: every state-changing operation
    in the shipped document either appears in WRITE_CASES or sits in
    UNEXERCISED_WRITE_OPERATIONS with a stated reason.
    """

    def test_every_published_write_operation_is_exercised_or_excluded(self):
        from scripts.run_container_e2e import uncovered_write_operations

        assert uncovered_write_operations() == []

    def test_every_exclusion_states_a_reason(self):
        from scripts.run_container_e2e import UNEXERCISED_WRITE_OPERATIONS

        assert all(reason.strip() for reason in UNEXERCISED_WRITE_OPERATIONS.values())

    def test_whole_array_replacements_are_exercised(self):
        exercised = {tool for tool, _ in WRITE_CASES}
        for op in (
            "replaceDenylist",
            "replaceAllowlist",
            "replaceSecurityTLDs",
            "replacePrivacyNatives",
            "replacePrivacyBlocklists",
            "replaceParentalControlCategories",
            "replaceParentalControlServices",
        ):
            assert op in exercised


class TestPlaceholderSubstitution:
    """Placeholders fill at any depth: a literal ``{ts}`` upstream is a broken domain.

    The first live run with nested replace bodies proved it: the whole-array
    cases carry their entries inside a list of objects, a top-level-only fill
    sent ``e2e-{ts}-replace.example.com`` verbatim, and NextDNS answered 200
    with an ``invalid`` error document.
    """

    def test_nested_body_entries_are_substituted(self, runner):
        filled = runner._substitute(
            {"profile_id": "{profile}", "body": [{"id": "e2e-{ts}-replace.example.com", "active": True}]},
            "abc123",
            1700000000,
        )
        assert filled == {
            "profile_id": "abc123",
            "body": [{"id": "e2e-1700000000-replace.example.com", "active": True}],
        }

    def test_no_write_case_sends_an_unfilled_placeholder(self, runner):
        for tool, template in WRITE_CASES:
            filled = runner._substitute(template, "abc123", 1700000000)
            blob = json.dumps(filled)
            assert "{profile}" not in blob and "{ts}" not in blob, f"{tool} sent {blob}"


class TestPrivateArtifacts:
    """Report artifacts carry profile ids and payloads: 0600, create or tighten."""

    def test_new_artifact_is_created_private_and_existing_ones_are_tightened(self, tmp_path):
        import os
        import stat

        from scripts.run_container_e2e import _private_open

        path = tmp_path / "report.jsonl"
        with _private_open(path, "a") as fh:
            fh.write('{"x": 1}\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        os.chmod(path, 0o644)
        with _private_open(path, "a") as fh:
            fh.write('{"x": 2}\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert len(path.read_text().splitlines()) == 2
