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
    ) -> None:
        self.endpoint = endpoint
        self.variant = variant
        self.allow_live_writes = allow_live_writes
        self.plot_profile = plot_profile
        self.artifacts_dir = artifacts_dir
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

        duration = time.time() - start_time

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
                if isinstance(parsed_json, dict) and "error" in parsed_json:
                    is_error = True
                    last_error = str(parsed_json["error"])
            except json.JSONDecodeError:
                parsed_json = response_text

        # Validate plotAnalytics image output
        if (
            tool_name == "plotAnalytics"
            and not is_error
            and not has_image
            and not (isinstance(response_text, str) and "data:image" in response_text)
        ):
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
        schema_status = "SKIPPED"
        schema_error = ""
        if self.spec and parsed_json is not None and parsed_json != {"success": True}:
            status, err_lines = validate_tool_response(tool_name, parsed_json, self.spec)
            schema_status = status
            if status == "INVALID":
                schema_error = "; ".join(err_lines)
                log_warn(f"  Schema validation failed: {schema_error}")
                self.schema_errors += 1
            elif status == "VALID":
                schema_status = "VALID"

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

        if not await self.check_endpoint_readiness():
            return 1

        created_profile_id = ""

        try:
            async with (
                streamable_http_client(self.endpoint) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                log_success("Connected to MCP container and session initialized")

                # Preflight: tool enumeration
                log_info("Performing preflight checks...")
                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]

                missing_tools = [t for t in EXPECTED_TOOLS if t not in tool_names]
                if missing_tools:
                    log_error(f"Missing expected tools: {missing_tools}")
                    return 1

                log_success(f"Found {len(tool_names)} tools; all {len(EXPECTED_TOOLS)} expected tools present")

                # Profile setup
                profile_id = ""
                if self.allow_live_writes:
                    log_info("Step 1: Creating test profile for live writes...")
                    ts = int(time.time())
                    p_name = f"E2E Test Profile {ts}"
                    ok, res = await self.execute_call(
                        session,
                        "manageProfiles",
                        {"operation": "create", "name": p_name},
                    )
                    if ok and isinstance(res, dict):
                        data_obj = res.get("data") if isinstance(res.get("data"), dict) else res
                        created_profile_id = str(data_obj.get("id") or "") if isinstance(data_obj, dict) else ""
                        if created_profile_id:
                            profile_id = created_profile_id
                            (self.artifacts_dir / "test_profile_id.txt").write_text(profile_id, encoding="utf-8")
                            log_success(f"Created test profile: {profile_id}")
                    if not profile_id:
                        log_error("Failed to create test profile")
                        return 1
                else:
                    log_info("Step 1: Fetching existing profile for read-only tests...")
                    ok, res = await self.execute_call(session, "manageProfiles", {"operation": "list"})
                    if ok and isinstance(res, dict) and "data" in res and res["data"]:
                        profile_id = str(res["data"][0].get("id") or "")
                        log_success(f"Using existing profile: {profile_id}")
                    if not profile_id:
                        log_error("No existing profile available for read-only tests")
                        return 1

                plot_profile_id = self.plot_profile or profile_id
                now_ts = int(time.time())
                one_day_ago = now_ts - 86400
                one_hour_ago = now_ts - 3600

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

                # Plot analytics
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

                # Live writes
                if self.allow_live_writes:
                    log_info("Step 3: Running live write tests...")
                    ts = int(time.time())
                    allow_entry = f"e2e-{ts}-allow.example.com"
                    deny_entry = f"e2e-{ts}-deny.example.com"

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

                    # allowlist
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "allowlist",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": allow_entry}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "allowlist",
                            "operation": "update",
                            "profile_id": profile_id,
                            "entry_id": allow_entry,
                            "entry": {"active": True},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "allowlist",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": allow_entry,
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "allowlist",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": allow_entry},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "allowlist",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": allow_entry,
                        },
                    )

                    # denylist
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "denylist",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": deny_entry}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "denylist",
                            "operation": "update",
                            "profile_id": profile_id,
                            "entry_id": deny_entry,
                            "entry": {"active": True},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "denylist",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": deny_entry,
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "denylist",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": deny_entry},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "denylist",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": deny_entry,
                        },
                    )

                    # privacy_blocklists
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_blocklists",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": "nextdns-recommended"}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_blocklists",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "nextdns-recommended",
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_blocklists",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": "nextdns-recommended"},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_blocklists",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "nextdns-recommended",
                        },
                    )

                    # privacy_natives
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_natives",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": "apple"}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_natives",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "apple",
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_natives",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": "apple"},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "privacy_natives",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "apple",
                        },
                    )

                    # security_tlds
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "security_tlds",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": "zip"}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "security_tlds",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "zip",
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "security_tlds",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": "zip"},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "security_tlds",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "zip",
                        },
                    )

                    # parental_categories
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_categories",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": "gambling"}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_categories",
                            "operation": "update",
                            "profile_id": profile_id,
                            "entry_id": "gambling",
                            "entry": {"active": True},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_categories",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "gambling",
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_categories",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": "gambling"},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_categories",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "gambling",
                        },
                    )

                    # parental_services
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_services",
                            "operation": "replace",
                            "profile_id": profile_id,
                            "entries": [{"id": "tiktok"}],
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_services",
                            "operation": "update",
                            "profile_id": profile_id,
                            "entry_id": "tiktok",
                            "entry": {"active": True},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_services",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "tiktok",
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_services",
                            "operation": "add",
                            "profile_id": profile_id,
                            "entry": {"id": "tiktok"},
                        },
                    )
                    await self.execute_call(
                        session,
                        "manageLists",
                        {
                            "list_type": "parental_services",
                            "operation": "remove",
                            "profile_id": profile_id,
                            "entry_id": "tiktok",
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
                else:
                    log_info("Skipping write operations (allow_live_writes=False)")

        finally:
            # Cleanup test profile if created
            if created_profile_id:
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

        # Execution Summary
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

        if self.failed_count > 0 or self.schema_errors > 0:
            log_error("E2E test failed")
            return 1

        log_success("All executed E2E calls completed successfully")
        return 0


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = ContainerE2ERunner(
        endpoint=args.endpoint,
        variant=args.variant,
        allow_live_writes=args.allow_live_writes,
        plot_profile=args.plot_profile,
        artifacts_dir=args.artifacts_dir,
    )
    return asyncio.run(runner.run())


if __name__ == "__main__":
    sys.exit(main())
