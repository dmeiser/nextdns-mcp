"""Unit tests for the container E2E runner script."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import ImageContent, TextContent, Tool

from scripts.run_container_e2e import EXPECTED_TOOLS, ContainerE2ERunner, parse_args


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
