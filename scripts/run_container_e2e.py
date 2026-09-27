#!/usr/bin/env python3
"""Run E2E validation against a NextDNS MCP server container over MCP.

SPDX-License-Identifier: MIT
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ImageContent, TextContent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.validate_schema import (
    load_openapi_spec,
    validate_tool_response,
)

HEALTH_FAILURE_CLASSES = ("auth", "unreachable")


def _assert_health_ok(body: Any, health_url: str) -> tuple[bool, str]:
    """Assert the 200 side of the contract: exactly ``{"status": "ok"}``."""
    if body != {"status": "ok"}:
        return False, f'expected {{"status": "ok"}} from {health_url}, got {body}'
    return True, f'GET {health_url} -> 200 {{"status": "ok"}}'


def _assert_health_failure(body: dict[str, Any], health_url: str) -> tuple[bool, str]:
    """Assert the 503 side: a documented failure class and a non-empty reason."""
    failure_class = body.get("class")
    if failure_class not in HEALTH_FAILURE_CLASSES:
        return False, f"expected class in {HEALTH_FAILURE_CLASSES}, got {failure_class!r}"
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return False, f"expected a non-empty reason in the {health_url} 503 body, got {body}"
    return True, f"GET {health_url} -> 503 class={failure_class} reason={reason!r}"


def check_health_endpoint(health_url: str, api_key: str, expect_ok: bool, timeout: float = 15.0) -> tuple[bool, str]:
    """Validate the served ``/health`` readiness endpoint over real HTTP.

    ``/health`` is a readiness probe, not a liveness constant, so this asserts the
    contract a client actually receives. ``expect_ok`` selects which side to
    assert: ``True`` for a server whose configured credentials work (200 with
    exactly ``{"status": "ok"}``), ``False`` for one whose credentials do not
    (503 carrying a failure class and a short reason). Either way the response
    body must not contain the API key.

    Args:
        health_url: Absolute URL of the ``/health`` route to GET.
        api_key: The credential the server was configured with; used only to
            assert it never appears in the response.
        expect_ok: True to require 200, False to require 503.
        timeout: Seconds to wait for the HTTP response.

    Returns:
        A (passed, detail) pair; ``detail`` states what was observed or why the
        contract was not met.
    """
    try:
        response = httpx.get(health_url, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"GET {health_url} failed: {type(exc).__name__}"

    if api_key and api_key in response.text:
        return False, f"GET {health_url} leaked the API key in the response body"

    try:
        body = response.json()
    except ValueError:
        return False, f"GET {health_url} returned a non-JSON body (HTTP {response.status_code})"

    if expect_ok:
        if response.status_code != 200:
            return False, f"expected HTTP 200 from {health_url}, got {response.status_code}: {body}"
        return _assert_health_ok(body, health_url)

    if response.status_code != 503:
        return False, f"expected HTTP 503 from {health_url}, got {response.status_code}: {body}"
    if not isinstance(body, dict) or body.get("status") != "error":
        return False, f'expected status "error" in the {health_url} 503 body, got {body}'
    return _assert_health_failure(body, health_url)


# Colors for terminal output
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{BLUE}[INFO]{NC} {msg}", file=sys.stderr)


def log_success(msg: str) -> None:
    print(f"{GREEN}[SUCCESS]{NC} {msg}", file=sys.stderr)


def log_warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{NC} {msg}", file=sys.stderr)


def log_error(msg: str) -> None:
    print(f"{RED}[ERROR]{NC} {msg}", file=sys.stderr)


EXPECTED_TOOLS = [
    "dohLookup",
    "manageLists",
    "manageLogs",
    "manageProfiles",
    "manageRewrites",
    "manageSettings",
    "plotAnalytics",
    "queryAnalytics",
]

LIST_WRITE_CASES = [
    # (list_type, entry_id, supports_update); "{ts}" in entry_id is substituted
    # with the run timestamp so repeated runs exercise fresh entries.
    ("allowlist", "e2e-{ts}-allow.example.com", True),
    ("denylist", "e2e-{ts}-deny.example.com", True),
    ("privacy_blocklists", "nextdns-recommended", False),
    ("privacy_natives", "apple", False),
    ("security_tlds", "zip", False),
    ("parental_categories", "gambling", True),
    ("parental_services", "tiktok", True),
]

SPEC_PATH = PROJECT_ROOT / "src" / "nextdns_mcp" / "nextdns-openapi.yaml"


class ContainerE2ERunner:
    """Orchestrates E2E testing of the NextDNS MCP container over MCP."""

    def __init__(
        self,
        endpoint: str,
        variant: str,
        allow_live_writes: bool,
        plot_profile: str,
        artifacts_dir: Path,
        health_url: str = "",
        api_key: str = "",
        expect_health_ok: bool = True,
        health_only: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.variant = variant
        self.allow_live_writes = allow_live_writes
        self.plot_profile = plot_profile
        self.artifacts_dir = artifacts_dir
        # /health sits beside the /mcp route on the same server unless overridden.
        self.health_url = health_url or f"{endpoint.rsplit('/', 1)[0]}/health"
        self.api_key = api_key
        self.expect_health_ok = expect_health_ok
        self.health_only = health_only
        self.report_file = artifacts_dir / f"tools_report_{variant}.jsonl"

        self.executed_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.schema_errors = 0

        self.spec: dict[str, Any] = {}
        if SPEC_PATH.exists():
            self.spec = load_openapi_spec(str(SPEC_PATH))
        else:
            log_warn(f"OpenAPI spec not found at {SPEC_PATH}; schema validation disabled")

    def record_result(
        self,
        tool: str,
        status: str,
        schema_status: str = "SKIPPED",
        schema_error: str = "",
        args: str = "",
        duration: float = 0.0,
        error_msg: str = "",
    ) -> None:
        """Write call result to jsonl report and update counters."""
        record: dict[str, Any] = {
            "tool": tool,
            "status": status,
            "schema_validation": schema_status,
            "schema_error": schema_error,
            "args": args,
            "duration": f"{duration:.2f}s",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if error_msg:
            record["error"] = error_msg

        with open(self.report_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def record_skip(self, tool: str, reason: str) -> None:
        """Record a skipped tool call."""
        record = {
            "tool": tool,
            "status": "SKIPPED",
            "reason": reason,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with open(self.report_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        self.skipped_count += 1

    def _parse_response_content(self, result: Any) -> tuple[str, bool, Any]:
        """Split a tool result into (response_text, has_image, parsed_json)."""
        response_text = ""
        has_image = False
        parsed_json: Any = None

        for content_block in getattr(result, "content", []):
            if isinstance(content_block, ImageContent) or getattr(content_block, "type", "") == "image":
                has_image = True
            elif isinstance(content_block, TextContent) or getattr(content_block, "type", "") == "text":
                response_text = getattr(content_block, "text", "")

        if response_text:
            try:
                parsed_json = json.loads(response_text)
            except json.JSONDecodeError:
                parsed_json = response_text

        return response_text, has_image, parsed_json

    @staticmethod
    def _payload_error(parsed_json: Any) -> str | None:
        """Return the error message carried by a JSON payload, or None when there is none."""
        if isinstance(parsed_json, dict) and "error" in parsed_json:
            return str(parsed_json["error"])
        return None

    @staticmethod
    def _plot_missing_image(tool_name: str, is_error: bool, has_image: bool, response_text: str) -> bool:
        """True when a plotAnalytics call succeeded without returning image content."""
        return (
            tool_name == "plotAnalytics"
            and not is_error
            and not has_image
            and not (isinstance(response_text, str) and "data:image" in response_text)
        )

    def _validate_against_schema(self, tool_name: str, parsed_json: Any) -> tuple[str, str]:
        """Validate a parsed payload against the OpenAPI spec.

        Returns a (status, error) pair, where status is SKIPPED when no spec is
        loaded or the payload is not schema-validated.
        """
        if not self.spec or parsed_json is None or parsed_json == {"success": True}:
            return "SKIPPED", ""
        status, err_lines = validate_tool_response(tool_name, parsed_json, self.spec)
        if status == "INVALID":
            log_warn(f"  Schema validation failed: {'; '.join(err_lines)}")
            self.schema_errors += 1
            return "INVALID", "; ".join(err_lines)
        if status == "VALID":
            return "VALID", ""
        return status, ""

    async def _call_with_retries(
        self,
        session: ClientSession,
        tool_name: str,
        args: dict[str, Any],
        max_retries: int,
        retry_delay: float,
    ) -> tuple[float, str, Any]:
        """Call the tool, retrying transient failures.

        Returns:
            A (duration, last_error, result) triple; ``result`` is None when every
            attempt failed.
        """
        start_time = time.time()
        attempt = 1
        last_error = ""
        result = None

        while attempt <= max_retries:
            try:
                result = await session.call_tool(tool_name, arguments=args)
                last_error = ""
                break
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                if attempt < max_retries:
                    log_warn(f"  Error on attempt {attempt}/{max_retries}: {e}; retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    attempt += 1
                else:
                    break

        return time.time() - start_time, last_error, result

    async def execute_call(
        self,
        session: ClientSession,
        tool_name: str,
        args: dict[str, Any],
        max_retries: int = 3,
        retry_delay: float = 5.0,
    ) -> tuple[bool, Any]:
        """Execute a single tool call with retries and schema validation."""
        args_str = " ".join(f"{k}={v}" for k, v in args.items())
        log_info(f"Executing: {tool_name} {args_str}")

        duration, last_error, result = await self._call_with_retries(session, tool_name, args, max_retries, retry_delay)

        if last_error or result is None:
            log_error(f"{tool_name}: FAILED (call failed: {last_error})")
            self.record_result(
                tool_name,
                "FAILED",
                args=args_str,
                duration=duration,
                error_msg=last_error or "No response",
            )
            self.failed_count += 1
            return False, None

        # Check for error payload or result.is_error
        is_error = getattr(result, "is_error", False)
        response_text, has_image, parsed_json = self._parse_response_content(result)

        payload_error = self._payload_error(parsed_json)
        if payload_error is not None:
            is_error = True
            last_error = payload_error

        # Validate plotAnalytics image output
        if self._plot_missing_image(tool_name, is_error, has_image, response_text):
            is_error = True
            last_error = "plotAnalytics did not return image content"

        if is_error:
            log_error(f"{tool_name}: FAILED ({last_error or 'server returned error'})")
            self.record_result(
                tool_name,
                "FAILED",
                args=args_str,
                duration=duration,
                error_msg=last_error or response_text[:200],
            )
            self.failed_count += 1
            return False, parsed_json

        # Schema validation
        schema_status, schema_error = self._validate_against_schema(tool_name, parsed_json)

        log_success(f"{tool_name}: OK")
        self.record_result(
            tool_name,
            "OK",
            schema_status=schema_status,
            schema_error=schema_error,
            args=args_str,
            duration=duration,
        )
        self.executed_count += 1
        return True, parsed_json

    async def check_endpoint_readiness(self, max_attempts: int = 30) -> bool:
        """Wait for MCP HTTP endpoint to become responsive."""
        log_info("Checking MCP endpoint readiness...")
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(self.endpoint)
                    if resp.status_code in (200, 400, 405):
                        log_success(f"MCP endpoint ready (HTTP {resp.status_code} on attempt {attempt})")
                        return True
            except Exception:  # noqa: BLE001, S110
                pass

            if attempt < max_attempts:
                await asyncio.sleep(1.0)

        log_error(f"MCP endpoint at {self.endpoint} did not become ready after {max_attempts}s")
        return False

    def check_health(self) -> bool:
        """Validate the served /health readiness endpoint; True when it conforms."""
        passed, detail = check_health_endpoint(self.health_url, self.api_key, self.expect_health_ok)
        if passed:
            log_success(detail)
        else:
            log_error(detail)
        return passed

    async def _preflight(self, session: ClientSession) -> bool:
        """Enumerate the served tools and verify the expected set is present."""
        log_info("Performing preflight checks...")
        tools_result = await session.list_tools()
        tool_names = [t.name for t in tools_result.tools]

        missing_tools = [t for t in EXPECTED_TOOLS if t not in tool_names]
        if missing_tools:
            log_error(f"Missing expected tools: {missing_tools}")
            return False

        log_success(f"Found {len(tool_names)} tools; all {len(EXPECTED_TOOLS)} expected tools present")
        return True

    async def _create_profile(self, session: ClientSession) -> str:
        """Create a test profile for live writes; empty when creation failed."""
        log_info("Step 1: Creating test profile for live writes...")
        ts = int(time.time())
        p_name = f"E2E Test Profile {ts}"
        ok, res = await self.execute_call(
            session,
            "manageProfiles",
            {"operation": "create", "name": p_name},
        )
        created_profile_id = ""
        if ok and isinstance(res, dict):
            data_obj = res.get("data") if isinstance(res.get("data"), dict) else res
            created_profile_id = str(data_obj.get("id") or "") if isinstance(data_obj, dict) else ""
            if created_profile_id:
                (self.artifacts_dir / "test_profile_id.txt").write_text(created_profile_id, encoding="utf-8")
                log_success(f"Created test profile: {created_profile_id}")
        if not created_profile_id:
            log_error("Failed to create test profile")
        return created_profile_id

    async def _fetch_existing_profile(self, session: ClientSession) -> str:
        """Fetch an existing profile for read-only tests; empty when none is available."""
        log_info("Step 1: Fetching existing profile for read-only tests...")
        ok, res = await self.execute_call(session, "manageProfiles", {"operation": "list"})
        if ok and isinstance(res, dict) and "data" in res and res["data"]:
            profile_id = str(res["data"][0].get("id") or "")
            log_success(f"Using existing profile: {profile_id}")
            if profile_id:
                return profile_id
        log_error("No existing profile available for read-only tests")
        return ""

    async def _provision_profile(self, session: ClientSession) -> tuple[str, str]:
        """Create (live writes) or fetch (read-only) the test profile.

        Returns:
            A (profile_id, created_profile_id) pair; ``profile_id`` is empty when
            provisioning failed. ``created_profile_id`` is non-empty only when this
            run created the profile and must clean it up.
        """
        if self.allow_live_writes:
            created_profile_id = await self._create_profile(session)
            return created_profile_id, created_profile_id
        profile_id = await self._fetch_existing_profile(session)
        return profile_id, ""

    async def _run_read_checks(
        self,
        session: ClientSession,
        profile_id: str,
        plot_profile_id: str,
        one_hour_ago: int,
        one_day_ago: int,
    ) -> None:
        """Exercise every read-only tool against the provisioned profile."""
        log_info(f"Step 2: Testing grouped tools with profile {profile_id}...")

        # Read-only tests
        await self.execute_call(session, "manageProfiles", {"operation": "list"})
        await self.execute_call(session, "manageProfiles", {"operation": "get", "profile_id": profile_id})

        settings_cats = ["general", "privacy", "security", "parental", "performance", "logs", "blockpage"]
        for cat in settings_cats:
            await self.execute_call(
                session,
                "manageSettings",
                {"operation": "get", "category": cat, "profile_id": profile_id},
            )

        list_types = [
            "allowlist",
            "denylist",
            "privacy_blocklists",
            "privacy_natives",
            "security_tlds",
            "parental_categories",
            "parental_services",
        ]
        for lt in list_types:
            await self.execute_call(
                session,
                "manageLists",
                {"operation": "get", "list_type": lt, "profile_id": profile_id},
            )

        await self.execute_call(session, "manageRewrites", {"operation": "list", "profile_id": profile_id})
        await self.execute_call(
            session,
            "manageLogs",
            {"operation": "get", "profile_id": profile_id, "from_time": str(one_hour_ago), "limit": 10},
        )
        await self.execute_call(
            session,
            "manageLogs",
            {"operation": "download", "profile_id": profile_id},
        )
        await self.execute_call(
            session,
            "dohLookup",
            {"domain": "example.com", "profile_id": profile_id, "record_type": "A"},
        )

        # Analytics metrics
        analytics_metrics = [
            "status",
            "domains",
            "queryTypes",
            "reasons",
            "ips",
            "dnssec",
            "encryption",
            "ipVersions",
            "protocols",
            "devices",
            "destinations",
        ]
        for metric in analytics_metrics:
            q_args: dict[str, Any] = {
                "metric": metric,
                "profile_id": plot_profile_id,
                "from_time": str(one_day_ago),
            }
            if metric == "destinations":
                q_args["destination_type"] = "countries"
            await self.execute_call(session, "queryAnalytics", q_args)

        series_metrics = [
            "status",
            "queryTypes",
            "reasons",
            "ips",
            "dnssec",
            "encryption",
            "ipVersions",
            "protocols",
            "devices",
            "destinations",
        ]
        for metric in series_metrics:
            q_args = {
                "metric": metric,
                "profile_id": plot_profile_id,
                "from_time": str(one_day_ago),
                "series": True,
            }
            if metric == "destinations":
                q_args["destination_type"] = "countries"
            await self.execute_call(session, "queryAnalytics", q_args)

    async def _run_plot_sweep(self, session: ClientSession, plot_profile_id: str, one_day_ago: int) -> None:
        """Exercise plotAnalytics for each metric, or record skips when no plot profile is set."""
        plot_metrics = [
            "status",
            "devices",
            "protocols",
            "queryTypes",
            "ipVersions",
            "dnssec",
            "encryption",
            "reasons",
            "ips",
        ]
        if self.plot_profile:
            for metric in plot_metrics:
                await self.execute_call(
                    session,
                    "plotAnalytics",
                    {"metric": metric, "profile_id": plot_profile_id, "from_time": str(one_day_ago)},
                )
        else:
            log_warn("NEXTDNS_PLOT_PROFILE not set; skipping plotAnalytics tools")
            for metric in plot_metrics:
                self.record_skip("plotAnalytics", f"metric={metric}: NEXTDNS_PLOT_PROFILE not set")

    async def _run_write_checks(self, session: ClientSession, profile_id: str) -> None:
        """Exercise profile/settings updates, every list type, rewrites, and log clearing."""
        log_info("Step 3: Running live write tests...")
        ts = int(time.time())

        await self.execute_call(
            session,
            "manageProfiles",
            {"operation": "update", "profile_id": profile_id, "name": f"Updated E2E Profile {ts}"},
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "general",
                "profile_id": profile_id,
                "settings": {"web3": True},
            },
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "logs",
                "profile_id": profile_id,
                "settings": {"enabled": True, "retention": 86400},
            },
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "blockpage",
                "profile_id": profile_id,
                "settings": {"enabled": True},
            },
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "performance",
                "profile_id": profile_id,
                "settings": {"ecs": True, "cacheBoost": True},
            },
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "privacy",
                "profile_id": profile_id,
                "settings": {"disguisedTrackers": True, "allowAffiliate": False},
            },
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "security",
                "profile_id": profile_id,
                "settings": {"threatIntelligenceFeeds": True, "googleSafeBrowsing": True},
            },
        )
        await self.execute_call(
            session,
            "manageSettings",
            {
                "operation": "update",
                "category": "parental",
                "profile_id": profile_id,
                "settings": {"safeSearch": True, "youtubeRestrictedMode": True},
            },
        )

        for list_type, entry_template, supports_update in LIST_WRITE_CASES:
            entry_id = entry_template.format(ts=ts)
            await self.execute_call(
                session,
                "manageLists",
                {
                    "list_type": list_type,
                    "operation": "replace",
                    "profile_id": profile_id,
                    "entries": [{"id": entry_id}],
                },
            )
            if supports_update:
                await self.execute_call(
                    session,
                    "manageLists",
                    {
                        "list_type": list_type,
                        "operation": "update",
                        "profile_id": profile_id,
                        "entry_id": entry_id,
                        "entry": {"active": True},
                    },
                )
            await self.execute_call(
                session,
                "manageLists",
                {
                    "list_type": list_type,
                    "operation": "remove",
                    "profile_id": profile_id,
                    "entry_id": entry_id,
                },
            )
            await self.execute_call(
                session,
                "manageLists",
                {
                    "list_type": list_type,
                    "operation": "add",
                    "profile_id": profile_id,
                    "entry": {"id": entry_id},
                },
            )
            await self.execute_call(
                session,
                "manageLists",
                {
                    "list_type": list_type,
                    "operation": "remove",
                    "profile_id": profile_id,
                    "entry_id": entry_id,
                },
            )

        # rewrites
        ok, rw_res = await self.execute_call(
            session,
            "manageRewrites",
            {
                "operation": "add",
                "profile_id": profile_id,
                "name": f"e2e-{ts}.example.com",
                "content": "192.0.2.1",
            },
        )
        if ok and isinstance(rw_res, dict):
            rw_data = rw_res.get("data") if isinstance(rw_res.get("data"), dict) else rw_res
            rw_id = str(rw_data.get("id") or "") if isinstance(rw_data, dict) else ""
            if rw_id:
                await self.execute_call(
                    session,
                    "manageRewrites",
                    {"operation": "delete", "profile_id": profile_id, "entry_id": rw_id},
                )

        # logs clear
        await self.execute_call(
            session,
            "manageLogs",
            {"operation": "clear", "profile_id": profile_id},
        )

    async def _cleanup(self, created_profile_id: str) -> None:
        """Delete the test profile if this run created it."""
        if not created_profile_id:
            return
        log_info(f"Cleaning up test profile {created_profile_id}...")
        try:
            async with (
                streamable_http_client(self.endpoint) as (r, w),
                ClientSession(r, w) as cleanup_session,
            ):
                await cleanup_session.initialize()
                await self.execute_call(
                    cleanup_session,
                    "manageProfiles",
                    {"operation": "delete", "profile_id": created_profile_id},
                )
        except Exception as e:  # noqa: BLE001
            log_warn(f"Failed to delete test profile {created_profile_id}: {e}")
        finally:
            (self.artifacts_dir / "test_profile_id.txt").unlink(missing_ok=True)

    async def _run_health_only(self) -> int:
        """Validate /health alone; returns the process exit code."""
        # Nothing listens on the /mcp endpoint in this mode, so the health
        # check is the only thing to run and there is no boot wait for it.
        if not self.check_health():
            return 1
        log_success("/health readiness probe validated over HTTP")
        return 0

    async def run(self) -> int:
        """Run the full container E2E suite."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        # Clear previous report file
        self.report_file.write_text("", encoding="utf-8")

        log_info("================================")
        log_info(f"NextDNS MCP Container E2E Test ({self.variant})")
        log_info(f"Endpoint: {self.endpoint}")
        log_info(f"Allow live writes: {self.allow_live_writes}")
        log_info(f"Artifacts report: {self.report_file}")
        log_info("================================")

        if self.health_only:
            return await self._run_health_only()

        # Wait for the container to accept connections first: the health probe
        # is a single un-retried request, so it must only run once the server
        # is actually listening.
        if not await self.check_endpoint_readiness():
            return 1

        if not self.check_health():
            return 1

        created_profile_id = ""

        try:
            async with (
                streamable_http_client(self.endpoint) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                log_success("Connected to MCP container and session initialized")

                if not await self._preflight(session):
                    return 1

                profile_id, created_profile_id = await self._provision_profile(session)
                if not profile_id:
                    return 1

                now_ts = int(time.time())
                one_day_ago = now_ts - 86400
                one_hour_ago = now_ts - 3600
                plot_profile_id = self.plot_profile or profile_id

                await self._run_read_checks(session, profile_id, plot_profile_id, one_hour_ago, one_day_ago)
                await self._run_plot_sweep(session, plot_profile_id, one_day_ago)

                if self.allow_live_writes:
                    await self._run_write_checks(session, profile_id)
                else:
                    log_info("Skipping write operations (allow_live_writes=False)")
        finally:
            await self._cleanup(created_profile_id)

        self._log_summary()

        if self.failed_count > 0 or self.schema_errors > 0:
            log_error("E2E test failed")
            return 1

        log_success("All executed E2E calls completed successfully")
        return 0

    def _log_summary(self) -> None:
        """Log the execution summary."""
        log_info("")
        log_info("================================")
        log_info(f"Execution Summary ({self.variant})")
        log_info("================================")
        log_info(f"Expected NextDNS tools: {len(EXPECTED_TOOLS)}")
        log_success(f"Executed calls: {self.executed_count}")
        log_warn(f"Skipped: {self.skipped_count}")
        if self.failed_count > 0:
            log_error(f"Failed: {self.failed_count}")
        else:
            log_success(f"Failed: {self.failed_count}")
        if self.schema_errors > 0:
            log_warn(f"Schema validation errors: {self.schema_errors}")
        log_info(f"Report: {self.report_file}")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="NextDNS MCP Container E2E Validation")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("MCP_ENDPOINT", "http://127.0.0.1:8000/mcp"),
        help="MCP server streamable HTTP endpoint URL (default: http://127.0.0.1:8000/mcp)",
    )
    parser.add_argument(
        "--variant",
        default=os.environ.get("VARIANT", "slim"),
        choices=["slim", "alpine"],
        help="Container image variant being tested: slim or alpine (default: slim)",
    )
    parser.add_argument(
        "--allow-live-writes",
        action="store_true",
        default=os.environ.get("ALLOW_LIVE_WRITES", "false").lower() == "true",
        help="Enable profile creation, update, and write calls (default: false)",
    )
    parser.add_argument(
        "--plot-profile",
        default=os.environ.get("NEXTDNS_PLOT_PROFILE", ""),
        help="Profile ID with analytics history for plotting tests (default: from env)",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts",
        help="Directory to write tools_report_<variant>.jsonl (default: artifacts/)",
    )
    parser.add_argument(
        "--health-url",
        default=os.environ.get("HEALTH_URL", ""),
        help="/health URL to validate (default: derived from --endpoint)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NEXTDNS_API_KEY", ""),
        help="Credential the server was configured with; only used to assert it is absent from /health responses",
    )
    parser.add_argument(
        "--expect-health",
        choices=["ok", "not-ok"],
        default=os.environ.get("EXPECT_HEALTH", "ok"),
        help="Assert /health returns 200 (ok) or 503 with a failure class (not-ok) (default: ok)",
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        default=os.environ.get("HEALTH_ONLY", "false").lower() == "true",
        help="Validate only the /health endpoint and skip the MCP tool suite (default: false)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = ContainerE2ERunner(
        endpoint=args.endpoint,
        variant=args.variant,
        allow_live_writes=args.allow_live_writes,
        plot_profile=args.plot_profile,
        artifacts_dir=args.artifacts_dir,
        health_url=args.health_url,
        api_key=args.api_key,
        expect_health_ok=args.expect_health == "ok",
        health_only=args.health_only,
    )
    return asyncio.run(runner.run())


if __name__ == "__main__":
    sys.exit(main())
