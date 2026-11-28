"""Unit tests for the container E2E runner's generative-UI checks.

Covers rate-limit pacing, prefab content validation, and the
generative-UI-only mode that exercises ``search_components`` and
``generate_prefab_ui`` in the container. The rest of the runner suite lives
in ``test_run_container_e2e.py``.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from scripts.run_container_e2e import (
    PREFAB_UI_TOOL,
    PREFAB_VIEW_CODE,
    SEARCH_COMPONENTS_TOOL,
    ContainerE2ERunner,
    _generative_ui_failure,
    _missing_generative_ui_tools,
    _search_components_failure,
    parse_args,
)


@pytest.fixture
def runner(tmp_path: Path) -> ContainerE2ERunner:
    """Create a runner instance with a temporary artifacts directory.

    Pacing is off and the rate-limit table is zero-delay: these tests assert
    behaviour, not elapsed time, and a suite of ~127 calls must not spend
    minutes waiting for a wall clock it is not testing.
    """
    return ContainerE2ERunner(
        endpoint="http://127.0.0.1:8000/mcp",
        variant="slim",
        allow_live_writes=False,
        plot_profile="",
        artifacts_dir=tmp_path,
        rate_limit_backoff=(0.0,),
        read_delay=0.0,
        write_delay=0.0,
        phase_cooldown=0.0,
    )


def _tool_result(payload: dict) -> MagicMock:
    """Build a mock MCP tool result carrying a JSON payload, as the server returns it."""
    result = MagicMock()
    result.is_error = False
    result.content = [TextContent(type="text", text=json.dumps(payload))]
    return result


# The payload the server returns for an over-quota call: a normal result, not an
# exception, carrying the status and the upstream code (errors.http_error_payload).
RATE_LIMITED_PAYLOAD = {
    "error": "HTTP error in POST /profiles: Client error '429 Too Many Requests' for url 'https://api.nextdns.io/profiles'",
    "code": "http_error",
    "status_code": 429,
    "upstream_code": "rateLimit",
    "upstream_codes": ["rateLimit"],
}


class TestRateLimitPacingAndFullBackoff:
    """Default retries walk all RATE_LIMIT_BACKOFF entries and pace read/write bursts."""

    @pytest.mark.asyncio
    async def test_default_max_retries_walks_entire_backoff_table(self, tmp_path: Path, monkeypatch):
        from scripts.run_container_e2e import DEFAULT_MAX_RETRIES, RATE_LIMIT_BACKOFF

        assert DEFAULT_MAX_RETRIES == len(RATE_LIMIT_BACKOFF) + 1
        sleep_calls: list[float] = []

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        r = ContainerE2ERunner(
            endpoint="http://127.0.0.1:8000/mcp",
            variant="slim",
            allow_live_writes=False,
            plot_profile="",
            artifacts_dir=tmp_path,
            read_delay=0.0,
        )
        session = AsyncMock()
        session.call_tool.return_value = _tool_result(RATE_LIMITED_PAYLOAD)

        ok, _ = await r.execute_call(session, "updatePrivacySettings", {"profile_id": "p1"})
        assert ok is False
        assert session.call_tool.call_count == DEFAULT_MAX_RETRIES
        assert sleep_calls == list(RATE_LIMIT_BACKOFF)

    @pytest.mark.asyncio
    async def test_pacing_transitions_from_read_to_write_and_bumps_on_429(self, tmp_path: Path, monkeypatch):
        from scripts.run_container_e2e import ADAPTIVE_PACE_CAP_SECONDS

        sleep_calls: list[float] = []

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        r = ContainerE2ERunner(
            endpoint="http://127.0.0.1:8000/mcp",
            variant="slim",
            allow_live_writes=False,
            plot_profile="",
            artifacts_dir=tmp_path,
            rate_limit_backoff=(5.0,),
            read_delay=0.2,
            write_delay=0.5,
            phase_cooldown=2.5,
        )
        session = AsyncMock()
        session.call_tool.side_effect = [
            _tool_result({"data": []}),
            _tool_result({"data": []}),
            _tool_result(RATE_LIMITED_PAYLOAD),
            _tool_result({"data": []}),
        ]

        # First call has no pre-call delay; second waits read_delay; third waits
        # read_delay, hits 429 (doubles pace 0.2 -> 0.4), and sleeps 5.0 backoff.
        assert (await r.execute_call(session, "listProfiles", {}))[0] is True
        assert sleep_calls == []
        assert (await r.execute_call(session, "getProfile", {"profile_id": "p1"}))[0] is True
        assert sleep_calls == [0.2]
        assert (await r.execute_call(session, "getProfile", {"profile_id": "p1"}, max_retries=2))[0] is True
        assert sleep_calls == [0.2, 0.2, 5.0]
        assert r._pace_seconds == pytest.approx(0.4)

        # Entering write phase raises pace to write_delay and sleeps phase_cooldown.
        await r._enter_write_phase()
        assert sleep_calls[-1] == 2.5
        assert r._pace_seconds == 0.5

        # Repeated bumps cap cleanly at ADAPTIVE_PACE_CAP_SECONDS.
        for _ in range(5):
            r._bump_pace()
        assert r._pace_seconds == ADAPTIVE_PACE_CAP_SECONDS

    def test_bump_pace_is_noop_when_pacing_disabled(self, runner: ContainerE2ERunner):
        assert runner._pace_seconds == 0.0
        runner._bump_pace()
        assert runner._pace_seconds == 0.0


@pytest.mark.asyncio
async def test_execute_call_plot_prefab_content_rejects_summary_only(runner: ContainerE2ERunner):
    """A summary-only plotAnalytics answer must fail the visual check (issue #399).

    The reporter's own inputs — text carrying "Analytics:" / "[Rendered " with no
    $prefab envelope — passed under the old predicate, so the E2E could not detect
    a regressed UI payload. This test fails on that predicate and passes on the
    new one, which requires an image block or a $prefab envelope with a view.
    """
    for text in ("NextDNS Analytics: status", "[Rendered NextDNS dashboard]"):
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.is_error = False
        mock_result.content = [TextContent(type="text", text=text)]
        mock_result.structured_content = None
        session.call_tool.return_value = mock_result

        ok, _ = await runner.execute_call(session, "plotAnalytics", {"metric": "status"})

        assert ok is False

    # $prefab without a view is still rejected.
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [TextContent(type="text", text="NextDNS Analytics: status")]
    mock_result.structured_content = {"$prefab": {"version": "0.3"}}
    session.call_tool.return_value = mock_result

    ok, _ = await runner.execute_call(session, "plotAnalytics", {"metric": "status"})

    assert ok is False


# --- Issue #400: the enabled-container test for the generative-UI tool ------------


def _prefab_result(payload: dict | None = None, *, is_error: bool = False, text: str = "") -> SimpleNamespace:
    """A tools/call answer: a structured $prefab document, or whatever the sandbox said."""
    structured = {"$prefab": {"type": "view"}} if payload is None else payload
    return SimpleNamespace(
        is_error=is_error,
        structured_content=structured if not text else None,
        content=[TextContent(type="text", text=text or json.dumps(structured))],
    )


def _search_result(payload: object = ..., *, is_error: bool = False) -> SimpleNamespace:
    """A search_prefab_components answer: FastMCP's wrapped {"result": <markdown>} payload.

    ``payload=None`` models a server that answered only with the bare text block
    and no structured content, which the pin can no longer validate.
    """
    structured = {"result": "# Card"} if payload is ... else payload
    return SimpleNamespace(
        is_error=is_error,
        structured_content=structured,
        content=[TextContent(type="text", text="# Card")],
    )


def test_search_components_failure_accepts_a_wrapped_payload():
    """The enabled-container check judges the wrapped payload, not the text block."""
    assert _search_components_failure(_search_result()) == ""


def _report_records(runner: ContainerE2ERunner) -> list[dict]:
    """The jsonl call records the run wrote, in order — the artifact that says why a run failed."""
    text = runner.report_file.read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]


def test_search_components_failure_rejects_an_error_result_and_a_missing_payload():
    """An error flag or a bare-text-only answer is a failure, not an unvalidated pass."""
    assert "error result" in _search_components_failure(_search_result(is_error=True))
    assert "no structured payload" in _search_components_failure(_search_result(payload=None))


def test_generative_ui_failure_accepts_a_prefab_document():
    """A $prefab payload with no error flag is an executed UI document."""
    assert _generative_ui_failure(_prefab_result(), json.dumps({"$prefab": {"type": "view"}})) == ""


def test_generative_ui_failure_names_the_missing_deno_runtime():
    """The sandbox's Deno error is reported as an image regression, not a generic failure."""
    text = '{"error": "RuntimeError: Deno is required for the Prefab sandbox"}'
    failure = _generative_ui_failure(_prefab_result(payload={}, text=text), text)
    assert "no runnable Deno" in failure


def test_generative_ui_failure_rejects_an_error_result_and_a_missing_payload():
    """Either half of a broken answer fails: the error flag, or no $prefab document."""
    assert "error result" in _generative_ui_failure(_prefab_result(is_error=True, text="{}"), "{}")
    assert "no $prefab payload" in _generative_ui_failure(_prefab_result(payload={}), "{}")


def test_missing_generative_ui_tools_reports_the_pair():
    """tools/list without the generative-UI pair is the first half of the #400 contract."""
    assert _missing_generative_ui_tools({"listProfiles", PREFAB_UI_TOOL, "search_prefab_components"}) == []
    assert _missing_generative_ui_tools({"listProfiles"}) == ["generate_prefab_ui", "search_prefab_components"]


def _ui_runner(tmp_path: Path) -> ContainerE2ERunner:
    """A runner in generative-UI-only mode against the credential-free container."""
    return ContainerE2ERunner(
        endpoint="http://127.0.0.1:8002/mcp",
        variant="alpine",
        allow_live_writes=False,
        plot_profile="",
        artifacts_dir=tmp_path,
        expect_health_ok=False,
        generative_ui_only=True,
    )


async def test_generative_ui_only_calls_the_tool_in_the_container(tmp_path):
    """--generative-ui-only lists the catalog, calls both generative-UI tools, and passes."""
    runner = _ui_runner(tmp_path)
    session = AsyncMock()
    session.list_tools.return_value = MagicMock(
        tools=[SimpleNamespace(name=t) for t in ("listProfiles", PREFAB_UI_TOOL, SEARCH_COMPONENTS_TOOL)]
    )
    text = json.dumps({"$prefab": {"type": "view"}})
    session.call_tool.side_effect = [_prefab_result(text=text), _search_result()]

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch.object(runner, "_open_session") as open_session,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    assert exit_code == 0
    generate_call, search_call = session.call_tool.await_args_list
    assert generate_call.args == (PREFAB_UI_TOOL,)
    assert generate_call.kwargs["arguments"]["code"] == PREFAB_VIEW_CODE
    assert search_call.args == (SEARCH_COMPONENTS_TOOL,)
    assert search_call.kwargs["arguments"] == {"query": "Card", "detail": False}
    assert runner.executed_count == 2 and runner.failed_count == 0


async def test_generative_ui_only_fails_when_the_tool_is_not_served(tmp_path):
    """NEXTDNS_GENERATIVE_UI=on but no generate_prefab_ui in tools/list: the run fails."""
    runner = _ui_runner(tmp_path)
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(tools=[SimpleNamespace(name="listProfiles")])

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch.object(runner, "_open_session") as open_session,
        patch.object(runner, "execute_call", AsyncMock()) as execute,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    assert exit_code == 1
    execute.assert_not_called()


async def test_generative_ui_only_fails_when_the_sandbox_has_no_deno(tmp_path):
    """The call-time Deno crash #400 was filed for fails the run, not just the tool."""
    runner = _ui_runner(tmp_path)
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name=t) for t in (PREFAB_UI_TOOL, "search_prefab_components")]
    )
    text = '{"error": "RuntimeError: Deno is required for the Prefab sandbox"}'
    session.call_tool.side_effect = [_prefab_result(payload={}, text=text), _search_result()]

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch.object(runner, "_open_session") as open_session,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    assert exit_code == 1
    assert runner.failed_count == 1


async def test_generative_ui_only_fails_when_search_payload_is_missing(tmp_path):
    """A wrapped-tool answer with no structured content is a failed enabled-container check."""
    runner = _ui_runner(tmp_path)
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name=t) for t in (PREFAB_UI_TOOL, SEARCH_COMPONENTS_TOOL)]
    )
    text = json.dumps({"$prefab": {"type": "view"}})
    session.call_tool.side_effect = [_prefab_result(text=text), _search_result(payload=None)]

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch.object(runner, "_open_session") as open_session,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    assert exit_code == 1
    assert runner.failed_count == 1 and runner.schema_errors == 0


async def test_generative_ui_only_fails_when_search_payload_is_schema_invalid(tmp_path):
    """A structured answer the pin rejects fails the run, even with no error flag."""
    runner = _ui_runner(tmp_path)
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name=t) for t in (PREFAB_UI_TOOL, SEARCH_COMPONENTS_TOOL)]
    )
    text = json.dumps({"$prefab": {"type": "view"}})
    session.call_tool.side_effect = [_prefab_result(text=text), _search_result(payload={"result": 7})]

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch.object(runner, "_open_session") as open_session,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    assert exit_code == 1
    assert runner.failed_count == 1 and runner.schema_errors == 1
    # The record must carry the verdict, not the record_result default: the
    # workflow summary counts "schema_validation": "INVALID" records for its
    # schema-failure section, and an empty one contradicts the counter.
    record = _report_records(runner)[-1]
    assert record["tool"] == SEARCH_COMPONENTS_TOOL and record["status"] == "FAILED"
    assert record["schema_validation"] == "INVALID" and record["schema_error"]
    assert "failed schema validation" in record["error"]


async def test_generative_ui_only_records_a_search_transport_error(tmp_path):
    """A raising call_tool is a recorded FAILED, not a traceback that eats the report.

    The sibling generate_prefab_ui call gets this from execute_call ->
    _call_with_retries -> _fail_call; the search call runs outside execute_call,
    so it owes the same treatment.
    """
    runner = _ui_runner(tmp_path)
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name=t) for t in (PREFAB_UI_TOOL, SEARCH_COMPONENTS_TOOL)]
    )
    text = json.dumps({"$prefab": {"type": "view"}})
    session.call_tool.side_effect = [_prefab_result(text=text), RuntimeError("transport error: connection closed")]

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch.object(runner, "_open_session") as open_session,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    assert exit_code == 1
    assert runner.failed_count == 1 and runner.executed_count == 1
    record = _report_records(runner)[-1]
    assert record["tool"] == SEARCH_COMPONENTS_TOOL and record["status"] == "FAILED"
    assert "transport error: connection closed" in record["error"]


async def test_generative_ui_only_says_so_when_schema_validation_could_not_run(tmp_path):
    """With no spec loaded the check neither fails nor claims the payload was validated."""
    runner = _ui_runner(tmp_path)
    runner.spec = {}
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name=t) for t in (PREFAB_UI_TOOL, SEARCH_COMPONENTS_TOOL)]
    )
    text = json.dumps({"$prefab": {"type": "view"}})
    session.call_tool.side_effect = [_prefab_result(text=text), _search_result(payload={"result": 7})]

    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=True)),
        patch("scripts.run_container_e2e.check_health_endpoint", return_value=(True, '503 {"status": "error"}')),
        patch("scripts.run_container_e2e.log_success") as log_success,
        patch.object(runner, "_open_session") as open_session,
    ):
        open_session.return_value.__aenter__ = AsyncMock(return_value=session)
        open_session.return_value.__aexit__ = AsyncMock(return_value=False)
        exit_code = await runner.run()

    messages = [call.args[0] for call in log_success.call_args_list]
    search_line = next(m for m in messages if SEARCH_COMPONENTS_TOOL in m and "structured payload" in m)
    assert "schema validation: SKIPPED" in search_line and "schema-valid" not in search_line
    assert exit_code == 0 and runner.schema_errors == 0
    assert _report_records(runner)[-1]["schema_validation"] == "SKIPPED"


async def test_generative_ui_only_needs_no_endpoint_and_no_credential(tmp_path):
    """The health probe is skipped once the endpoint fails to come up; that run still fails."""
    runner = _ui_runner(tmp_path)
    with (
        patch.object(runner, "check_endpoint_readiness", AsyncMock(return_value=False)),
        patch("scripts.run_container_e2e.check_health_endpoint") as health,
    ):
        assert await runner.run() == 1
    health.assert_not_called()


def test_parse_args_generative_ui(monkeypatch):
    """--generative-ui-only is off by default and settable from the environment or CLI."""
    monkeypatch.delenv("GENERATIVE_UI_ONLY", raising=False)
    with patch("sys.argv", ["run_container_e2e.py"]):
        assert parse_args().generative_ui_only is False

    monkeypatch.setenv("GENERATIVE_UI_ONLY", "true")
    with patch("sys.argv", ["run_container_e2e.py"]):
        assert parse_args().generative_ui_only is True


def test_execute_call_names_the_deno_regression_for_the_prefab_tool(runner):
    """A generate_prefab_ui answer without a $prefab document fails, named as an image regression."""
    deno_error = "Code execution failed: Deno is required for the Prefab sandbox"
    text = json.dumps({"error": deno_error})
    result = _prefab_result(payload={"error": deno_error}, text=text)
    session = AsyncMock()
    session.call_tool.return_value = result

    ok, _parsed = asyncio.run(runner.execute_call(session, PREFAB_UI_TOOL, {"code": PREFAB_VIEW_CODE}, max_retries=1))

    assert ok is False and runner.failed_count == 1
    assert "no runnable Deno" in runner._call_failure(result, PREFAB_UI_TOOL)[0]
