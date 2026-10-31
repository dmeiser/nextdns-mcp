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

from scripts import validate_schema
from scripts.validate_schema import (
    assert_operation_coverage,
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


def expected_tools(spec: dict[str, Any]) -> list[str]:
    """The catalog a container built from this spec has to advertise.

    Every operation in the document, under its own ``operationId``, except the ones
    withheld because the transport cannot carry them; plus the operations the server
    performs itself, which no endpoint describes. The spec is the source, so a
    container built from a different document fails the preflight instead of quietly
    testing a different surface. The unit test for this script pins the result to the
    server's own ``published_tool_names()``.
    """
    operations = set(validate_schema.spec_operations(spec)) - validate_schema.WITHHELD_OPERATIONS
    return sorted(operations | set(validate_schema.LOCAL_TOOLS))


# Required query arguments a read cannot be called without. The API reads a relative
# offset, so the window is written as one rather than as an epoch; ``type`` is the
# destinations endpoint's own required enum, whose documented first value is countries.
READ_ARGUMENT_DEFAULTS: dict[str, Any] = {"from": "-1d", "to": "now", "limit": 10, "type": "countries"}


# Live write cases, in the order to run them: a tool and the arguments it is called
# with, after ``{profile}`` and ``{ts}`` are substituted. Every entry that adds
# something is paired with the one that takes it away, so a completed run leaves the
# profile as it found it.
WRITE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("updateProfile", {"profile_id": "{profile}", "name": "Updated E2E Profile {ts}"}),
    # One call per settings endpoint the spec describes; the category is the endpoint.
    ("updateSettings", {"profile_id": "{profile}", "web3": True}),
    ("updateLogsSettings", {"profile_id": "{profile}", "enabled": True, "retention": 86400}),
    ("updateBlockPageSettings", {"profile_id": "{profile}", "enabled": True}),
    ("updatePerformanceSettings", {"profile_id": "{profile}", "ecs": True, "cacheBoost": True}),
    ("updatePrivacySettings", {"profile_id": "{profile}", "disguisedTrackers": True, "allowAffiliate": False}),
    (
        "updateSecuritySettings",
        {
            "profile_id": "{profile}",
            "threatIntelligenceFeeds": True,
            "googleSafeBrowsing": True,
        },
    ),
    ("updateParentalControlSettings", {"profile_id": "{profile}", "safeSearch": True, "youtubeRestrictedMode": True}),
    # List entries: add, update where the entry has its own endpoint, remove.
    ("addToDenylist", {"profile_id": "{profile}", "id": "e2e-{ts}-deny.example.com"}),
    ("updateDenylistEntry", {"profile_id": "{profile}", "entry_id": "e2e-{ts}-deny.example.com", "active": True}),
    ("removeFromDenylist", {"profile_id": "{profile}", "entry_id": "e2e-{ts}-deny.example.com"}),
    ("addToAllowlist", {"profile_id": "{profile}", "id": "e2e-{ts}-allow.example.com"}),
    ("updateAllowlistEntry", {"profile_id": "{profile}", "entry_id": "e2e-{ts}-allow.example.com", "active": True}),
    ("removeFromAllowlist", {"profile_id": "{profile}", "entry_id": "e2e-{ts}-allow.example.com"}),
    ("addSecurityTLD", {"profile_id": "{profile}", "id": "zip"}),
    ("removeSecurityTLD", {"profile_id": "{profile}", "entry_id": "zip"}),
    ("addPrivacyNative", {"profile_id": "{profile}", "id": "apple"}),
    ("removePrivacyNative", {"profile_id": "{profile}", "entry_id": "apple"}),
    ("addToParentalControlCategories", {"profile_id": "{profile}", "id": "gambling"}),
    ("updateParentalControlCategoryEntry", {"profile_id": "{profile}", "id": "gambling", "active": True}),
    ("removeFromParentalControlCategories", {"profile_id": "{profile}", "id": "gambling"}),
    ("addToParentalControlServices", {"profile_id": "{profile}", "id": "tiktok"}),
    ("updateParentalControlServiceEntry", {"profile_id": "{profile}", "id": "tiktok", "active": True}),
    ("removeFromParentalControlServices", {"profile_id": "{profile}", "id": "tiktok"}),
]

SPEC_PATH = PROJECT_ROOT / "src" / "nextdns_mcp" / "nextdns-openapi.yaml"

# The catalog expected of a container built from the shipped document. Computed here so
# the summary and the preflight, and the unit test for this script, all read one list.
EXPECTED_TOOLS = expected_tools(load_openapi_spec(str(SPEC_PATH))) if SPEC_PATH.exists() else []


def _input_schema(tool: Any) -> dict[str, Any]:
    """A served tool's published input schema, under whichever name the SDK calls it.

    The MCP SDK renamed ``inputSchema`` to ``input_schema``, and a container can be
    built on either side of that rename. Reading one name alone fails with an
    AttributeError that looks like a server fault rather than a version skew, so both
    are read here.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is not None:
        return schema
    return getattr(tool, "inputSchema", None) or {}


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

        # The schemas the served container advertises, kept from the preflight: the
        # read sweep builds each call from them rather than from an argument list
        # maintained beside this script.
        self.tool_schemas: dict[str, dict[str, Any]] = {}

        self.executed_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.schema_errors = 0

        self.spec: dict[str, Any] = {}
        if SPEC_PATH.exists():
            self.spec = load_openapi_spec(str(SPEC_PATH))
            # Hard-fail on mapping drift: an operationId that resolves to no
            # schema would otherwise degrade to SKIPPED at validation time,
            # which the workflow counts as neither pass nor fail.
            assert_operation_coverage(self.spec, EXPECTED_TOOLS)
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
        """Enumerate the served tools and require them to be exactly the expected catalog.

        An extra tool is as much drift as a missing one: the container is either
        advertising something the shipped document does not describe, or holding back
        something it does. The schemas the same answer carries are what the read sweep
        below builds its arguments from, which is why they are kept.
        """
        log_info("Performing preflight checks...")
        tools_result = await session.list_tools()
        served = {tool.name for tool in tools_result.tools}
        self.tool_schemas = {tool.name: _input_schema(tool) for tool in tools_result.tools}

        missing = sorted(set(EXPECTED_TOOLS) - served)
        extra = sorted(served - set(EXPECTED_TOOLS))
        if missing:
            log_error(f"Missing expected tools: {missing}")
        if extra:
            log_error(f"Tools the shipped spec does not describe: {extra}")
        if missing or extra:
            return False

        log_success(f"Found {len(served)} tools; the served catalog matches the shipped spec")
        return True

    async def _create_profile(self, session: ClientSession) -> str:
        """Create a test profile for live writes; empty when creation failed."""
        log_info("Step 1: Creating test profile for live writes...")
        ts = int(time.time())
        p_name = f"E2E Test Profile {ts}"
        ok, res = await self.execute_call(session, "createProfile", {"name": p_name})
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
        ok, res = await self.execute_call(session, "listProfiles", {})
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

    def _read_arguments(self, tool_name: str, profile_id: str) -> tuple[dict[str, Any], list[str]]:
        """The smallest call that satisfies one read's own published schema.

        Arguments are read from the schema the server advertises for the tool rather
        than from a table kept beside this script: the schema is the contract a client
        sees, and a sweep that invents its own argument list would test the invention.
        Optional arguments are left alone, because each has a server-side default.

        Returns:
            A (arguments, unfilled) pair. ``unfilled`` names a required argument with
            no value to send; the caller records that as a skip instead of guessing,
            so the run says which endpoint it could not call.
        """
        schema = self.tool_schemas.get(tool_name, {})
        arguments: dict[str, Any] = {}
        unfilled: list[str] = []
        for name in schema.get("required", []):
            if name == "profile_id":
                arguments[name] = profile_id
            elif name in READ_ARGUMENT_DEFAULTS:
                arguments[name] = READ_ARGUMENT_DEFAULTS[name]
            else:
                unfilled.append(name)
        return arguments, unfilled

    async def _run_read_checks(
        self,
        session: ClientSession,
        profile_id: str,
        plot_profile_id: str,
    ) -> None:
        """Call every read the shipped document describes, on the provisioned profile.

        The list comes from the document, not from a table maintained here: an
        operation added to the spec is exercised by the next run without this script
        changing, and an operation the container does not publish shows up as a
        preflight failure rather than as a silent gap.

        Analytics reads use the plot profile when one is configured, because those
        endpoints answer empty for a profile with no traffic and an empty answer looks
        like a working endpoint either way.
        """
        operations = validate_schema.spec_operations(self.spec)
        reads = sorted(
            name
            for name, method in operations.items()
            if method == "get" and name not in validate_schema.WITHHELD_OPERATIONS
        )
        log_info(f"Step 2: Testing {len(reads)} read operations against profile {profile_id}...")

        analytics_profile = plot_profile_id or profile_id
        for operation_id in reads:
            if operation_id not in self.tool_schemas:
                self.record_skip(operation_id, "not published by the served container")
                continue
            profile = analytics_profile if operation_id.startswith("getAnalytics") else profile_id
            arguments, unfilled = self._read_arguments(operation_id, profile)
            if unfilled:
                self.record_skip(operation_id, f"required arguments with no value to send: {unfilled}")
                continue
            await self.execute_call(session, operation_id, arguments)

        # A DoH lookup is not an API endpoint: it queries the profile's resolver, so
        # nothing in the document describes it and it is called here on its own.
        await self.execute_call(
            session,
            "dohLookup",
            {"domain": "example.com", "profile_id": profile_id, "record_type": "A"},
        )

    async def _run_plot_sweep(self, session: ClientSession, plot_profile_id: str) -> None:
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
                    # The API reads a relative offset, and the chart is of the last day.
                    {"metric": metric, "profile_id": plot_profile_id, "from_time": "-1d"},
                )
        else:
            log_warn("NEXTDNS_PLOT_PROFILE not set; skipping plotAnalytics tools")
            for metric in plot_metrics:
                self.record_skip("plotAnalytics", f"metric={metric}: NEXTDNS_PLOT_PROFILE not set")

    @staticmethod
    def _substitute(template: dict[str, Any], profile_id: str, ts: int) -> dict[str, Any]:
        """Fill ``{profile}`` and ``{ts}`` in one write case's arguments.

        The timestamp makes an entry unique per run, which is what lets a repeated run
        exercise a real add instead of the answer to adding something that is already
        there.
        """
        return {
            name: value.format(profile=profile_id, ts=ts) if isinstance(value, str) else value
            for name, value in template.items()
        }

    async def _run_write_checks(self, session: ClientSession, profile_id: str) -> None:
        """Exercise every write this surface offers, and leave the profile as it was found.

        The settings writes are one per settings endpoint, because that is what the
        document says the settings are: there is no single settings call. Each list
        case adds an entry, changes it where the entry has its own endpoint, and removes
        it again.
        """
        log_info(f"Step 3: Running {len(WRITE_CASES)} live write cases against profile {profile_id}...")
        ts = int(time.time())

        for tool_name, template in WRITE_CASES:
            await self.execute_call(session, tool_name, self._substitute(template, profile_id, ts))

        # A rewrite's id comes back only from the call that adds it, so this pair lives
        # here rather than in the table: add it, read the id out of the answer, delete it.
        ok, rw_res = await self.execute_call(
            session,
            "addRewrite",
            {"profile_id": profile_id, "name": f"e2e-{ts}.example.com", "content": "192.0.2.1"},
        )
        rewrite_id = ""
        if ok and isinstance(rw_res, dict):
            rw_data = rw_res.get("data") if isinstance(rw_res.get("data"), dict) else rw_res
            rewrite_id = str(rw_data.get("id") or "") if isinstance(rw_data, dict) else ""
        if rewrite_id:
            await self.execute_call(session, "deleteRewrite", {"profile_id": profile_id, "entry_id": rewrite_id})
        else:
            self.record_skip("deleteRewrite", "the rewrite add returned no id to delete")

        await self.execute_call(session, "clearLogs", {"profile_id": profile_id})

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
                await self.execute_call(cleanup_session, "deleteProfile", {"profile_id": created_profile_id})
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

                plot_profile_id = self.plot_profile or profile_id

                await self._run_read_checks(session, profile_id, plot_profile_id)
                await self._run_plot_sweep(session, plot_profile_id)

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
