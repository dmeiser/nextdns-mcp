"""Unit tests for the container E2E runner script."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.types import ImageContent, TextContent, Tool

from scripts.run_container_e2e import (
    EXPECTED_TOOLS,
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


def test_record_result(runner: ContainerE2ERunner, tmp_path: Path):
    """Test that record_result appends valid JSON lines."""
    runner.record_result(
        tool="manageProfiles",
        status="OK",
        schema_status="VALID",
        schema_error="",
        args="operation=list",
        duration=0.5,
    )

    report_lines = runner.report_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(report_lines) == 1
    data = json.loads(report_lines[0])
    assert data["tool"] == "manageProfiles"
    assert data["status"] == "OK"
    assert data["schema_validation"] == "VALID"
    assert data["args"] == "operation=list"
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

    ok, res = await runner.execute_call(session, "manageProfiles", {"operation": "list"})

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

    ok, _ = await runner.execute_call(session, "manageProfiles", {"operation": "get", "profile_id": "bad"})

    assert ok is False
    assert runner.failed_count == 1


@pytest.mark.asyncio
async def test_execute_call_retry_on_exception(runner: ContainerE2ERunner):
    """Test tool call retry on transient exception."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.content = [TextContent(type="text", text='{"data":{"id":"p123"}}')]

    session.call_tool.side_effect = [RuntimeError("temporary error"), mock_result]

    ok, _ = await runner.execute_call(session, "manageProfiles", {"operation": "get"}, max_retries=2, retry_delay=0.01)

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
    """Test a complete read-only run with mocked session."""
    tools_list = [Tool(name=t, description="", inputSchema={}) for t in EXPECTED_TOOLS]
    mock_tools_res = MagicMock()
    mock_tools_res.tools = tools_list

    mock_session = AsyncMock()
    mock_session.list_tools.return_value = mock_tools_res

    mock_call_res = MagicMock()
    mock_call_res.is_error = False
    mock_call_res.content = [TextContent(type="text", text='{"data":[{"id":"test-profile-id"}]}')]
    mock_session.call_tool.return_value = mock_call_res

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


def test_runner_startup_fails_when_mapping_unresolvable(tmp_path: Path, monkeypatch):
    """A spec whose mapped operations do not resolve is a hard error at startup.

    Before the coverage assertion, an unresolvable operationId degraded to
    SKIPPED at validation time, which the E2E workflow counts as neither pass
    nor fail.
    """
    empty_spec = tmp_path / "empty-spec.yaml"
    empty_spec.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("scripts.run_container_e2e.SPEC_PATH", empty_spec)

    with pytest.raises(ValueError, match="resolve to no response schema"):
        ContainerE2ERunner(
            endpoint="http://127.0.0.1:8000/mcp",
            variant="slim",
            allow_live_writes=False,
            plot_profile="",
            artifacts_dir=tmp_path,
        )
