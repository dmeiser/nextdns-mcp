"""Unit tests for the container E2E runner's health-endpoint checks.

Covers health URL derivation, the 200/503 health contract, startup
consistency between the served document and the catalog, placeholder
substitution, and the private-artifact open guards (mode, O_NOFOLLOW,
O_NONBLOCK). The rest of the runner suite lives in
``test_run_container_e2e.py``.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from scripts import ai_e2e_common
from scripts.run_container_e2e import (
    WRITE_CASES,
    ContainerE2ERunner,
    check_health_endpoint,
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
    body = '{"status": "error"}'
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, body)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=True)

    assert passed is False
    assert "expected HTTP 200" in detail


def test_check_health_endpoint_accepts_503_with_the_minimal_body():
    """A 503 carrying exactly {"status": "error"} is the documented failure shape."""
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, '{"status": "error"}')
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is True
    assert "503" in detail


@pytest.mark.parametrize(
    ("failure_class", "reason"),
    [
        ("auth", "NextDNS API rejected the configured credentials (HTTP 401)"),
        ("unreachable", "NextDNS API did not respond within 5s"),
    ],
)
def test_check_health_endpoint_rejects_503_that_discloses_the_failure_class(failure_class, reason):
    """A 503 that names the class or the reason is the #603 disclosure, not a pass.

    The container E2E job runs this against a server started with an invalid
    credential, so it is the check that the served image — not just the unit —
    keeps the failure detail off an unauthenticated route.
    """
    payload = json.dumps({"status": "error", "class": failure_class, "reason": reason})
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, payload)
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert failure_class in detail


def test_check_health_endpoint_rejects_503_with_extra_fields():
    """Any extra key in the 503 body is outside the documented minimal shape."""
    with patch("scripts.run_container_e2e.httpx.get") as mock_get:
        mock_get.return_value = _health_response(503, '{"status": "error", "detail": "ConnectError"}')
        passed, detail = check_health_endpoint(HEALTH_URL, API_KEY, expect_ok=False)

    assert passed is False
    assert "ConnectError" in detail


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

        path = tmp_path / "report.jsonl"
        with ai_e2e_common.private_open(path, "a") as fh:
            fh.write('{"x": 1}\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        os.chmod(path, 0o644)
        with ai_e2e_common.private_open(path, "a") as fh:
            fh.write('{"x": 2}\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert len(path.read_text().splitlines()) == 2


def test_private_open_refuses_when_the_mode_cannot_be_established(tmp_path, monkeypatch):
    """No 0600, no artifact: the report carries profile ids and payloads."""
    import os

    def boom(fd: int, mode: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(os, "fchmod", boom)
    path = tmp_path / "report.jsonl"
    with pytest.raises(PermissionError), ai_e2e_common.private_open(path, "a"):
        pass
    assert not path.exists() or path.stat().st_size == 0


def test_private_open_refuses_a_preplanted_symlink(tmp_path):
    """O_NOFOLLOW: neither the append nor the tightening fchmod lands through a link."""
    import os
    import stat

    target = tmp_path / "elsewhere.jsonl"
    target.write_text("not an artifact\n", encoding="utf-8")
    os.chmod(target, 0o644)
    link = tmp_path / "report.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError), ai_e2e_common.private_open(link, "a"):
        pass
    assert target.read_text(encoding="utf-8") == "not an artifact\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_private_open_refuses_a_fifo_without_blocking(tmp_path):
    """O_NONBLOCK: a FIFO with no reader is refused, not waited on forever.

    The attempt runs on a daemon thread joined with a 3s timeout, so the old
    blocking open fails this test in seconds instead of hanging the suite.
    """
    import os
    import threading

    fifo = tmp_path / "report.jsonl"
    os.mkfifo(fifo)
    finished: list[bool] = []

    def attempt() -> None:
        try:
            with ai_e2e_common.private_open(fifo, "a"):
                pass
        except OSError:
            pass
        finished.append(True)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "private_open blocked on a FIFO instead of refusing it"
    assert finished == [True]
