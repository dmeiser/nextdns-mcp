#!/usr/bin/env python3
"""
Live integration test for NextDNS MCP Server.

This script tests ALL 76 tools (68 enabled API operations + 7 custom bulk operations + 1 custom DoH lookup) by
invoking them through the MCP server (server.py), not via direct API calls.

Includes tests for:
- Profile management
- Settings operations (logs, block page, performance, parental control)
- Security operations (settings, TLDs, bulk replacement, item-level PATCH)
- Privacy operations (settings, blocklists, natives, bulk replacement, item-level PATCH)
- Parental control (settings, services, categories, bulk replacement, item operations)
- Content lists (allowlist, denylist) with bulk replacement and item-level PATCH operations
- Analytics (base endpoints + time-series)
- Logs operations
- Custom DoH lookup tool

Note: 2 operations excluded from MCP tools (truly unsupported):
- 1 streamLogs - SSE streaming not supported (FastMCP limitation)
- 1 getAnalyticsDomainsSeries - returns 404 from NextDNS API (API bug)

Note: 7 bulk replacement operations now supported via custom implementations:
- updateDenylist, updateAllowlist, updateParentalControlServices, 
  updateParentalControlCategories, updateSecurityTlds, updatePrivacyBlocklists,
  updatePrivacyNatives (accept JSON array strings, convert to array body)

Requirements:
- NEXTDNS_API_KEY environment variable must be set
- Internet connection to NextDNS API
- Valid NextDNS account

Usage:
    poetry run python tests/integration/test_live_api.py
    poetry run python tests/integration/test_live_api.py --skip-cleanup
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastmcp.tools.tool import ToolResult

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv

    # Look for .env in project root
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded environment from {env_path}")
except ImportError:
    pass  # python-dotenv not installed, skip

# Set up environment before importing server
if not os.getenv("NEXTDNS_API_KEY"):
    print("ERROR: NEXTDNS_API_KEY environment variable is required")
    print("Set it with: export NEXTDNS_API_KEY='your_key_here'")
    print("Or create a .env file in the project root with:")
    print("  NEXTDNS_API_KEY=your_key_here")
    sys.exit(1)

# Import the MCP server
from nextdns_mcp import server


class MCPServerTester:
    """Test all NextDNS MCP tools by invoking them through the server."""

    def __init__(self, skip_cleanup: bool = False, auto_delete_profile: bool = False):
        """Initialize the tester.

        Args:
            skip_cleanup: If True, skip the profile deletion prompt
        """
        self.validation_profile_id: Optional[str] = None
        self.tools: Dict = {}
        self.skip_cleanup = skip_cleanup

        # Track test results
        self.passed: List[str] = []
        self.failed: List[Dict[str, str]] = []
        self.skipped: List[Dict[str, str]] = []

        # Store entry IDs for cleanup
        self.entry_ids: Dict[str, str] = {}

        # Track DoH lookup details for log verification
        self.doh_lookup_info: Optional[Dict[str, object]] = None
        self.logs_ready = False
        self.log_provision_timeout = int(os.getenv("LOG_PROVISION_TIMEOUT_SECONDS", "120"))
        self.log_ingest_wait_seconds = int(os.getenv("LOG_INGEST_WAIT_SECONDS", "10"))
        self.log_verification_timeout = int(os.getenv("LOG_VERIFICATION_TIMEOUT_SECONDS", "180"))
        self.doh_record_types = ["A", "AAAA", "TXT"]
        self.doh_lookup_runs = 0
        self.auto_delete_profile = auto_delete_profile

    async def initialize(self):
        """Load all tools from the MCP server."""
        self.tools = await server.mcp.get_tools()
        print(f"Loaded {len(self.tools)} tools from MCP server")

    def print_header(self, text: str):
        """Print a section header."""
        print(f"\n{'=' * 80}")
        print(f"  {text}")
        print(f"{'=' * 80}\n")

    def print_test(self, name: str, status: str, details: str = ""):
        """Print test result."""
        symbols = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"}
        colors = {"PASS": "\033[92m", "FAIL": "\033[91m", "SKIP": "\033[93m"}
        reset = "\033[0m"

        symbol = symbols.get(status, "?")
        color = colors.get(status, "")
        print(f"{color}{symbol}{reset} {name:<50} [{status}]")
        if details:
            print(f"  → {details}")

    def record_skip(self, name: str, reason: str = ""):
        """Record and display a skipped test."""
        self.skipped.append({"name": name, "reason": reason})
        self.print_test(name, "SKIP", reason)

    async def call_tool(self, tool_name: str, **params):
        """Call an MCP tool and return the result."""
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found in MCP server")

        tool = self.tools[tool_name]
        # FastMCP tools always expect arguments parameter
        result = await tool.run(arguments=params)
        return result

    async def test_tool(self, tool_name: str, **params):
        """Run a test for a specific tool."""
        try:
            result = await self.call_tool(tool_name, **params)
            self.passed.append(tool_name)

            # Extract meaningful info from ToolResult
            from fastmcp.tools.tool import ToolResult

            if isinstance(result, ToolResult):
                # Use structured_content if available, otherwise content
                if result.structured_content:
                    details = str(result.structured_content)[:60]
                elif result.content:
                    details = str(result.content)[:60]
                else:
                    details = "Success (no content)"
            else:
                details = str(result)[:60] if result else ""

            details = details[:60] + "..." if len(details) > 60 else details
            self.print_test(tool_name, "PASS", details)
            return result
        except Exception as e:
            self.failed.append({"name": tool_name, "error": str(e)})
            self.print_test(tool_name, "FAIL", str(e)[:60])
            return None

    async def wait_for_logging_provisioning(self):
        """Poll until logging endpoints are available for the validation profile."""
        if not self.validation_profile_id:
            return

        if "getLogsSettings" not in self.tools:
            self.record_skip(
                "getLogsSettings", "Tool unavailable; cannot verify logging provisioning"
            )
            return

        pid = self.validation_profile_id
        start = time.monotonic()
        delay = 1.0
        print(
            f"Waiting for logging to provision for profile {pid} (timeout {self.log_provision_timeout}s)..."
        )

        while time.monotonic() - start < self.log_provision_timeout:
            try:
                await self.tools["getLogsSettings"].run(arguments={"profile_id": pid})
                self.logs_ready = True
                print(f"✓ Logging provisioning detected for profile {pid}")
                return
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

        raise RuntimeError(
            f"Logging did not provision within {self.log_provision_timeout} seconds for profile {pid}. "
            "Enable logs manually or increase LOG_PROVISION_TIMEOUT_SECONDS."
        )

    async def wait_for_log_ingestion(self):
        """Sleep briefly to allow the DoH lookup to appear in logs."""
        if not self.logs_ready:
            print("ℹ️ Logging not provisioned; skipping log ingestion wait.")
            return

        wait_seconds = max(self.log_ingest_wait_seconds, 0)
        if wait_seconds == 0:
            return

        domain = (self.doh_lookup_info or {}).get("domain", "validation.example.com")
        pid = self.validation_profile_id or "(unknown)"
        print(
            f"Waiting {wait_seconds} seconds for log ingestion "
            f"(profile {pid}, domain {domain})..."
        )
        await asyncio.sleep(wait_seconds)

    @staticmethod
    def _extract_payload(result):
        """Normalize ToolResult payloads into plain Python structures."""
        if isinstance(result, ToolResult):
            if result.structured_content:
                return result.structured_content
            if result.content and isinstance(result.content, list):
                # Join text content blocks if present
                texts = [getattr(block, "text", "") for block in result.content]
                merged = "".join(texts)
                try:
                    return json.loads(merged)
                except json.JSONDecodeError:
                    return merged
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return result
        return result

    async def poll_for_log_entry(
        self,
        profile_id: str,
        domain: str,
        lookup_timestamp: Optional[datetime],
        refresh_lookup: bool = False,
    ) -> tuple[Optional[dict], List[dict]]:
        """Poll the logs endpoint until a matching entry is found or timeout expires."""
        if "getLogs" not in self.tools:
            raise KeyError("getLogs tool unavailable")

        deadline = time.monotonic() + self.log_verification_timeout
        delay = 1.0
        last_entries: List[dict] = []
        last_error: Optional[Exception] = None

        first_attempt = True
        while time.monotonic() < deadline:
            if refresh_lookup and not first_attempt:
                try:
                    lookup_timestamp = await self.issue_doh_lookup(profile_id, domain)
                except Exception:
                    pass
            first_attempt = False
            try:
                result = await self.tools["getLogs"].run(arguments={"profile_id": profile_id})
                payload = self._extract_payload(result)
                if isinstance(payload, dict):
                    entries = payload.get("data") or []
                else:
                    entries = []
                last_entries = entries if isinstance(entries, list) else []

                normalized_target = domain.rstrip(".").lower()
                for entry in last_entries:
                    if not isinstance(entry, dict):
                        continue
                    entry_domain = str(entry.get("domain", "")).rstrip(".").lower()
                    if entry_domain != normalized_target:
                        continue
                    return entry, last_entries

                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
                continue
            except Exception as exc:  # pragma: no cover - best-effort polling
                last_error = exc
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)

        if last_error:
            raise last_error

        return None, last_entries

    def next_record_type(self) -> str:
        record_type = self.doh_record_types[self.doh_lookup_runs % len(self.doh_record_types)]
        self.doh_lookup_runs += 1
        return record_type

    async def issue_doh_lookup(self, profile_id: str, domain: str) -> datetime:
        """Execute a DoH lookup and return the timestamp used for verification."""
        if "dohLookup" not in self.tools:
            raise KeyError("dohLookup tool unavailable")

        record_type = self.next_record_type()
        timestamp = datetime.now(timezone.utc)
        await self.tools["dohLookup"].run(
            arguments={"domain": domain, "profile_id": profile_id, "record_type": record_type}
        )
        self.doh_lookup_info = {
            "domain": domain,
            "timestamp": timestamp,
            "record_type": record_type,
        }
        return timestamp

    # ========================================================================
    # Profile Management Tests
    # ========================================================================

    async def test_create_validation_profile(self):
        """Create a new profile for validation testing."""
        self.print_header("PROFILE CREATION")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        profile_name = f"Validation Profile {timestamp}"

        result = await self.test_tool("createProfile", name=profile_name)

        # Extract profile ID from ToolResult
        from fastmcp.tools.tool import ToolResult

        if isinstance(result, ToolResult):
            # Try structured_content first (likely a dict), then content
            data = result.structured_content or result.content

            if isinstance(data, dict):
                # Check for nested 'data' key or direct 'id' key
                self.validation_profile_id = data.get("data", {}).get("id") or data.get("id")
            elif isinstance(data, str):
                # Try parsing as JSON
                import json

                try:
                    parsed = json.loads(data)
                    self.validation_profile_id = parsed.get("data", {}).get("id") or parsed.get(
                        "id"
                    )
                except:
                    pass

            if self.validation_profile_id:
                print(f"\n  Created profile ID: {self.validation_profile_id}\n")
            else:
                print(f"\n  Warning: Could not extract profile ID from result\n")
                print(f"  Result data: {data}\n")

        return self.validation_profile_id

    async def test_profile_operations(self):
        """Test all profile operations."""
        self.print_header("PROFILE OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping profile operations")
            return

        await self.test_tool("listProfiles")
        await self.test_tool("getProfile", profile_id=self.validation_profile_id)
        await self.test_tool(
            "updateProfile",
            profile_id=self.validation_profile_id,
            name="Validation Profile (Updated)",
        )

    # ========================================================================
    # Settings Tests
    # ========================================================================

    async def test_settings(self):
        """Test all settings operations."""
        self.print_header("SETTINGS OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        # Main settings
        await self.test_tool("getSettings", profile_id=pid)
        await self.test_tool("updateSettings", profile_id=pid, blockPage={"enabled": True})

        # Logs settings
        logs_result = await self.test_tool("getLogsSettings", profile_id=pid)
        logs_payload = self._extract_payload(logs_result)
        retention = 7776000
        location = "us"
        if isinstance(logs_payload, dict):
            data = logs_payload.get("data") or {}
            retention = data.get("retention", retention)
            location = data.get("location", location)

        await self.test_tool(
            "updateLogsSettings",
            profile_id=pid,
            enabled=True,
            retention=retention,
            location=location,
        )

        # Block page settings
        await self.test_tool("getBlockPageSettings", profile_id=pid)
        await self.test_tool("updateBlockPageSettings", profile_id=pid, enabled=True)

        # Performance settings
        await self.test_tool("getPerformanceSettings", profile_id=pid)
        await self.test_tool("updatePerformanceSettings", profile_id=pid, ecs=True)

    # ========================================================================
    # Security Tests
    # ========================================================================

    async def test_security(self):
        """Test all security operations."""
        self.print_header("SECURITY OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        await self.test_tool("getSecuritySettings", profile_id=pid)
        await self.test_tool("updateSecuritySettings", profile_id=pid, threatIntelligenceFeeds=True)

        # TLD operations
        await self.test_tool("getSecurityTLDs", profile_id=pid)

        # Add a TLD (using a real TLD)
        security_tld_value = "xyz"
        await self.test_tool("addSecurityTLD", profile_id=pid, id=security_tld_value)

        # Look up the ID for the entry we just added
        security_tld_id = None
        result = await self.test_tool("getSecurityTLDs", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == security_tld_value:
                        security_tld_id = entry.get("id")
                        break
                if security_tld_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        security_tld_id = entry.get("id")

        if security_tld_id:
            self.entry_ids["security_tld"] = security_tld_id
            await self.test_tool("removeSecurityTLD", profile_id=pid, entry_id=security_tld_id)
        else:
            print("⚠️  Could not determine security TLD entry ID; skipping deletion")

        # Test bulk TLD replacement (custom tool)
        print("\n🧪 Testing bulk TLD replacement (custom tool)...")
        tlds_bulk = '["zip", "mov"]'
        await self.test_tool("updateSecurityTlds", profile_id=pid, tlds=tlds_bulk)

    # ========================================================================
    # Privacy Tests
    # ========================================================================

    async def test_privacy(self):
        """Test all privacy operations."""
        self.print_header("PRIVACY OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        await self.test_tool("getPrivacySettings", profile_id=pid)
        await self.test_tool("updatePrivacySettings", profile_id=pid, blocklists=[])

        # Blocklists
        await self.test_tool("getPrivacyBlocklists", profile_id=pid)

        blocklist_value = "nextdns-recommended"
        await self.test_tool("addPrivacyBlocklist", profile_id=pid, id=blocklist_value)

        blocklist_id = None
        result = await self.test_tool("getPrivacyBlocklists", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == blocklist_value:
                        blocklist_id = entry.get("id")
                        break
                if blocklist_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        blocklist_id = entry.get("id")

        if blocklist_id:
            self.entry_ids["blocklist"] = blocklist_id
            await self.test_tool("removePrivacyBlocklist", profile_id=pid, entry_id=blocklist_id)
        else:
            print("⚠️  Could not determine privacy blocklist entry ID; skipping deletion")

        # Native tracking protection
        await self.test_tool("getPrivacyNatives", profile_id=pid)
        native_value = "apple"
        await self.test_tool("addPrivacyNative", profile_id=pid, id=native_value)

        native_id = None
        result = await self.test_tool("getPrivacyNatives", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == native_value:
                        native_id = entry.get("id")
                        break
                if native_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        native_id = entry.get("id")

        if native_id:
            self.entry_ids["native"] = native_id
            await self.test_tool("removePrivacyNative", profile_id=pid, entry_id=native_id)
        else:
            print("⚠️  Could not determine privacy native entry ID; skipping deletion")

        # Test bulk privacy operations (custom tools)
        print("\n🧪 Testing bulk privacy operations (custom tools)...")

        # Test updatePrivacyBlocklists (bulk replacement)
        blocklists_bulk = '["nextdns-recommended", "oisd"]'
        await self.test_tool("updatePrivacyBlocklists", profile_id=pid, blocklists=blocklists_bulk)

        # Test updatePrivacyNatives (bulk replacement)
        natives_bulk = '["apple", "windows"]'
        await self.test_tool("updatePrivacyNatives", profile_id=pid, natives=natives_bulk)

    # ========================================================================
    # Parental Control Tests
    # ========================================================================

    async def test_parental_control(self):
        """Test all parental control operations."""
        self.print_header("PARENTAL CONTROL OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        # Settings operations
        await self.test_tool("getParentalControlSettings", profile_id=pid)
        await self.test_tool(
            "updateParentalControlSettings",
            profile_id=pid,
            safeSearch=False,
            youtubeRestrictedMode=False,
        )

        # Services and Categories
        await self.test_tool("getParentalControlServices", profile_id=pid)
        await self.test_tool("getParentalControlCategories", profile_id=pid)

        # Add a service
        service_id = "tiktok"
        await self.test_tool("addToParentalControlServices", profile_id=pid, id=service_id)

        # Get the list to find the entry ID
        result = await self.test_tool("getParentalControlServices", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            entry_id = None
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == service_id:
                        entry_id = entry.get("id")
                        break
                # Fallback to first entry if we didn't find an exact match
                if entry_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        entry_id = entry.get("id")

            if entry_id:
                self.entry_ids["pc_service"] = entry_id
            else:
                print(
                    "⚠️  Could not determine parental control service entry ID; skipping entry-specific tests"
                )

        # Test item-level operations for services
        if "pc_service" in self.entry_ids:
            entry_id = self.entry_ids["pc_service"]
            await self.test_tool(
                "updateParentalControlServiceEntry", profile_id=pid, id=entry_id, active=False
            )
            await self.test_tool("removeFromParentalControlServices", profile_id=pid, id=entry_id)

        # Add a category
        category_id = "gambling"
        await self.test_tool("addToParentalControlCategories", profile_id=pid, id=category_id)

        # Get the list to find the entry ID
        result = await self.test_tool("getParentalControlCategories", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            entry_id = None
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == category_id:
                        entry_id = entry.get("id")
                        break
                # Fallback to first entry if we didn't find an exact match
                if entry_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        entry_id = entry.get("id")

            if entry_id:
                self.entry_ids["pc_category"] = entry_id
            else:
                print(
                    "⚠️  Could not determine parental control category entry ID; skipping entry-specific tests"
                )

        # Test item-level operations for categories
        if "pc_category" in self.entry_ids:
            entry_id = self.entry_ids["pc_category"]
            await self.test_tool(
                "updateParentalControlCategoryEntry", profile_id=pid, id=entry_id, active=False
            )
            await self.test_tool("removeFromParentalControlCategories", profile_id=pid, id=entry_id)

        # Test bulk parental control operations (custom tools)
        print("\n🧪 Testing bulk parental control operations (custom tools)...")

        # Test updateParentalControlServices (bulk replacement)
        services_bulk = '["tiktok", "fortnite"]'
        await self.test_tool(
            "updateParentalControlServices", profile_id=pid, services=services_bulk
        )

        # Test updateParentalControlCategories (bulk replacement)
        categories_bulk = '["gambling", "dating"]'
        await self.test_tool(
            "updateParentalControlCategories", profile_id=pid, categories=categories_bulk
        )

    # ========================================================================
    # Allowlist/Denylist Tests
    # ========================================================================

    async def test_allowlist_denylist(self):
        """Test allowlist and denylist operations."""
        self.print_header("ALLOWLIST/DENYLIST OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        # Denylist
        await self.test_tool("getDenylist", profile_id=pid)

        denylist_value = "example-blocked.com"
        await self.test_tool("addToDenylist", profile_id=pid, id=denylist_value)

        denylist_id = None
        result = await self.test_tool("getDenylist", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == denylist_value:
                        denylist_id = entry.get("id")
                        break
                if denylist_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        denylist_id = entry.get("id")

        if denylist_id:
            self.entry_ids["denylist"] = denylist_id
            await self.test_tool(
                "updateDenylistEntry", profile_id=pid, entry_id=denylist_id, active=False
            )
            await self.test_tool("removeFromDenylist", profile_id=pid, entry_id=denylist_id)
        else:
            print("⚠️  Could not determine denylist entry ID; skipping update/delete")

        # Allowlist
        await self.test_tool("getAllowlist", profile_id=pid)
        allowlist_value = "example-allowed.com"
        await self.test_tool("addToAllowlist", profile_id=pid, id=allowlist_value)

        allowlist_id = None
        result = await self.test_tool("getAllowlist", profile_id=pid)
        if result and isinstance(result, ToolResult):
            payload = result.structured_content or {}
            if isinstance(payload, dict):
                entries = payload.get("data") or []
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == allowlist_value:
                        allowlist_id = entry.get("id")
                        break
                if allowlist_id is None and entries:
                    entry = entries[0]
                    if isinstance(entry, dict):
                        allowlist_id = entry.get("id")

        if allowlist_id:
            self.entry_ids["allowlist"] = allowlist_id
            await self.test_tool(
                "updateAllowlistEntry", profile_id=pid, entry_id=allowlist_id, active=False
            )
            await self.test_tool("removeFromAllowlist", profile_id=pid, entry_id=allowlist_id)
        else:
            print("⚠️  Could not determine allowlist entry ID; skipping update/delete")

        # Test bulk replacement operations (custom tools)
        print("\n🧪 Testing bulk replacement operations (custom tools)...")

        # Test updateDenylist (bulk replacement)
        denylist_bulk = '["bulk1.example.com", "bulk2.example.com"]'
        await self.test_tool("updateDenylist", profile_id=pid, entries=denylist_bulk)

        # Test updateAllowlist (bulk replacement)
        allowlist_bulk = '["trusted1.example.com", "trusted2.example.com"]'
        await self.test_tool("updateAllowlist", profile_id=pid, entries=allowlist_bulk)

    # ========================================================================
    # Analytics Tests
    # ========================================================================

    async def test_analytics(self):
        """Test all analytics operations."""
        self.print_header("ANALYTICS OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        # Note: Analytics might not have data for newly created profiles
        await self.test_tool("getAnalyticsStatus", profile_id=pid)
        await self.test_tool("getAnalyticsDomains", profile_id=pid)
        await self.test_tool("getAnalyticsQueryTypes", profile_id=pid)
        await self.test_tool("getAnalyticsReasons", profile_id=pid)
        await self.test_tool("getAnalyticsIPs", profile_id=pid)
        await self.test_tool("getAnalyticsDNSSEC", profile_id=pid)
        await self.test_tool("getAnalyticsEncryption", profile_id=pid)
        await self.test_tool("getAnalyticsIPVersions", profile_id=pid)
        await self.test_tool("getAnalyticsProtocols", profile_id=pid)
        await self.test_tool("getAnalyticsDestinations", profile_id=pid, type="countries")
        await self.test_tool("getAnalyticsDevices", profile_id=pid)

    # ========================================================================
    # Analytics Time-Series Tests
    # ========================================================================

    async def test_analytics_series(self):
        """Test time-series analytics operations (sample of endpoints)."""
        self.print_header("ANALYTICS TIME-SERIES OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        # Calculate a date range for analytics (last 24 hours)
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        from_time = yesterday.isoformat().replace("+00:00", "Z")
        to_time = now.isoformat().replace("+00:00", "Z")

        # Test ALL time-series endpoints (except getAnalyticsDomainsSeries - API issue)
        # Note: These might not have data for newly created profiles
        # Note: getAnalyticsDomainsSeries excluded - returns 404 from NextDNS API (known issue)
        await self.test_tool(
            "getAnalyticsStatusSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsQueryTypesSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsReasonsSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsIPsSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsDevicesSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsProtocolsSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsIPVersionsSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsDNSSECSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsEncryptionSeries", profile_id=pid, **{"from": from_time, "to": to_time}
        )
        await self.test_tool(
            "getAnalyticsDestinationsSeries",
            profile_id=pid,
            type="countries",
            **{"from": from_time, "to": to_time},
        )

    # ========================================================================
    # Logs Tests
    # ========================================================================

    async def test_logs(self):
        """Test logs operations."""
        self.print_header("LOGS OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id
        if not self.logs_ready:
            self.record_skip("getLogs", "Logging not provisioned for validation profile")
            if "downloadLogs" in self.tools:
                self.record_skip("downloadLogs", "Logging not provisioned for validation profile")
            return

        if not self.doh_lookup_info:
            self.record_skip("getLogs", "DoH lookup not executed prior to log verification")
            if "downloadLogs" in self.tools:
                self.record_skip(
                    "downloadLogs", "DoH lookup not executed prior to log verification"
                )
            return

        domain = "validation.example.com"
        try:
            lookup_timestamp = await self.issue_doh_lookup(pid, domain)
        except KeyError:
            self.record_skip("getLogs", "dohLookup tool unavailable for verification")
            if "downloadLogs" in self.tools:
                self.record_skip("downloadLogs", "dohLookup tool unavailable for verification")
            return
        except Exception as exc:
            self.failed.append({"name": "getLogs", "error": f"DoH lookup failed: {exc}"})
            self.print_test("getLogs", "FAIL", f"DoH lookup failed: {exc}")
            return

        try:
            matched_entry, entries = await self.poll_for_log_entry(
                pid, domain, lookup_timestamp, refresh_lookup=True
            )
        except KeyError:
            self.record_skip("getLogs", "Tool unavailable")
            if "downloadLogs" in self.tools:
                self.record_skip("downloadLogs", "Tool unavailable")
            return
        except Exception as exc:
            self.failed.append({"name": "getLogs", "error": str(exc)})
            self.print_test("getLogs", "FAIL", str(exc)[:60])
            return

        if matched_entry:
            details = f"Found log entry for {domain}"
            self.passed.append("getLogs")
            self.print_test("getLogs", "PASS", details)
        else:
            sample = [
                str(entry.get("domain"))
                for entry in entries
                if isinstance(entry, dict) and "domain" in entry
            ][:5]
            error = (
                f"No log entry for {domain} found after DoH lookup; received {len(entries)} entries"
                + (f" (sample domains: {sample})" if sample else "")
            )
            self.failed.append({"name": "getLogs", "error": error})
            self.print_test("getLogs", "FAIL", error)
            return

        if "downloadLogs" in self.tools:
            await self.test_tool("downloadLogs", profile_id=pid)
        else:
            self.record_skip("downloadLogs", "Tool unavailable")

    # ========================================================================
    # DoH Lookup Test (Custom Tool)
    # ========================================================================

    async def test_doh_lookup(self):
        """Test custom DoH lookup tool."""
        self.print_header("CUSTOM DOH LOOKUP")

        if not self.validation_profile_id:
            # Try with default profile if available
            profile_id = os.getenv("NEXTDNS_DEFAULT_PROFILE")
            if not profile_id:
                print("⚠️  No profile ID available for DoH test")
                return
        else:
            profile_id = self.validation_profile_id

        if not self.logs_ready:
            print("ℹ️ Logging not provisioned; DoH lookup will run without log verification.")

        domain = "validation.example.com"
        try:
            await self.issue_doh_lookup(profile_id, domain)
        except KeyError:
            self.record_skip("dohLookup", "Tool unavailable")
            return
        except Exception as exc:
            self.failed.append({"name": "dohLookup", "error": str(exc)})
            self.print_test("dohLookup", "FAIL", str(exc)[:60])
            return

        self.passed.append(
            "dohLookup"
        )  # mark success manually since issue_doh_lookup bypassed test_tool
        self.print_test("dohLookup", "PASS", "Executed DoH lookup for validation.example.com")
        await self.wait_for_log_ingestion()

    # ========================================================================
    # Cleanup
    # ========================================================================

    async def cleanup_profile(self):
        """Delete the validation profile with user confirmation."""
        self.print_header("CLEANUP")

        if not self.validation_profile_id:
            print("No validation profile to clean up.")
            return

        print(f"\nValidation profile ID: {self.validation_profile_id}")
        print("This profile was created for testing purposes.")

        if self.skip_cleanup:
            print("\n🔒 Profile deletion skipped (--skip-cleanup flag)")
            print(f"   Profile will remain in your account for manual inspection.")
            print(f"   To delete it manually, use profile ID: {self.validation_profile_id}")
            print(
                f"   Or run: poetry run python tests/integration/test_live_api.py --delete-profile {self.validation_profile_id}"
            )
            self.record_skip("clearLogs", "Cleanup skipped (--skip-cleanup flag)")
            return

        if self.auto_delete_profile:
            print("\n⚙️  Auto-deletion enabled (--auto-delete-profile). Proceeding without prompt.")
            response = "yes"
        else:
            print("\n⚠️  WARNING: This action cannot be undone!")
            print("\nOptions:")
            print("  yes   - Delete the profile now")
            print("  no    - Keep the profile for manual inspection")
            print("  skip  - Same as 'no' (alias)")

            response = (
                input("\nDo you want to DELETE this profile? (yes/no/skip): ").strip().lower()
            )

        if response not in ("yes", "y"):
            print("\n✓ Profile deletion skipped. Profile will remain in your account.")
            print(f"   Profile ID: {self.validation_profile_id}")
            print(f"   To delete it later, use the NextDNS web interface or run:")
            print(
                f"   poetry run python tests/integration/test_live_api.py --delete-profile {self.validation_profile_id}"
            )
            self.record_skip("clearLogs", "Profile retained per user instruction")
            return

        try:
            if "clearLogs" in self.tools:
                await self.test_tool("clearLogs", profile_id=self.validation_profile_id)
            else:
                self.record_skip("clearLogs", "Tool unavailable during cleanup")

            await self.test_tool("deleteProfile", profile_id=self.validation_profile_id)
            print("✓ Validation profile deleted successfully")
        except Exception as e:
            print(f"✗ Failed to delete profile: {e}")
            self.failed.append({"name": "deleteProfile", "error": str(e)})

    # ========================================================================
    # Test Runner
    # ========================================================================

    async def run_all_tests(self):
        """Run all integration tests."""
        print("\n" + "=" * 80)
        print("  NextDNS MCP Server - Live Integration Test")
        print("  Testing 75 tools via MCP server (74 API + 1 custom)")
        print("=" * 80)

        try:
            # Initialize - load tools from MCP server
            await self.initialize()

            # 1. Create validation profile
            await self.test_create_validation_profile()

            # Ensure logging endpoints are ready before continuing
            await self.wait_for_logging_provisioning()

            # 2. Profile operations
            await self.test_profile_operations()

            # 3. Settings
            await self.test_settings()

            # 4. Security
            await self.test_security()

            # 5. Privacy
            await self.test_privacy()

            # 6. Parental Control
            await self.test_parental_control()

            # 7. Allowlist/Denylist
            await self.test_allowlist_denylist()

            # 8. Analytics
            await self.test_analytics()

            # 9. Analytics Time-Series
            await self.test_analytics_series()

            # 10. DoH Lookup (custom tool)
            await self.test_doh_lookup()

            # 11. Logs
            await self.test_logs()

            # 12. Cleanup with confirmation
            await self.cleanup_profile()

        except KeyboardInterrupt:
            print("\n\n⚠️  Test interrupted by user!")
            await self.cleanup_profile()
            raise

        # Print summary
        self.print_summary()

    def print_summary(self):
        """Print test summary."""
        self.print_header("TEST SUMMARY")

        total = len(self.passed) + len(self.failed) + len(self.skipped)
        denom = total if total else 1

        print(f"Total Tests: {total}")
        print(f"✓ Passed:    {len(self.passed)} ({len(self.passed)/denom*100:.1f}%)")
        print(f"✗ Failed:    {len(self.failed)} ({len(self.failed)/denom*100:.1f}%)")
        print(f"- Skipped:   {len(self.skipped)} ({len(self.skipped)/denom*100:.1f}%)")

        if self.failed:
            print("\n" + "=" * 80)
            print("  FAILED TESTS")
            print("=" * 80)
            for failure in self.failed:
                print(f"\n✗ {failure['name']}")
                print(f"  Error: {failure['error']}")

        if self.skipped:
            print("\n" + "=" * 80)
            print("  SKIPPED TESTS")
            print("=" * 80)
            for item in self.skipped:
                print(f"\n- {item['name']}")
                print(f"  Reason: {item['reason']}")

        print("\n" + "=" * 80)


async def delete_profile(profile_id: str):
    """Delete a specific profile by ID.

    Args:
        profile_id: The profile ID to delete
    """
    print(f"\nDeleting profile: {profile_id}")
    print("⚠️  WARNING: This action cannot be undone!")

    response = input("\nAre you sure you want to DELETE this profile? (yes/no): ").strip().lower()

    if response != "yes":
        print("\n❌ Profile deletion cancelled.")
        return

    try:
        # Get tools
        tools = await server.mcp.get_tools()
        delete_tool = tools["deleteProfile"]

        # Delete the profile
        result = await delete_tool.run(arguments={"profile_id": profile_id})
        print(f"✓ Profile {profile_id} deleted successfully")
        print(f"Result: {result}")
    except Exception as e:
        print(f"✗ Failed to delete profile: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Live integration test for NextDNS MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full test suite
  poetry run python tests/integration/test_live_api.py

  # Run tests but keep the validation profile
  poetry run python tests/integration/test_live_api.py --skip-cleanup

  # Run tests and automatically delete the validation profile
  poetry run python tests/integration/test_live_api.py --auto-delete-profile

  # Delete a specific profile
  poetry run python tests/integration/test_live_api.py --delete-profile abc123
        """,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Skip the profile deletion prompt and keep the validation profile",
    )
    group.add_argument(
        "--auto-delete-profile",
        action="store_true",
        help="Automatically delete the validation profile without prompting",
    )
    parser.add_argument(
        "--delete-profile",
        metavar="PROFILE_ID",
        help="Delete a specific profile by ID (skips test suite)",
    )

    args = parser.parse_args()

    # Handle profile deletion mode
    if args.delete_profile:
        await delete_profile(args.delete_profile)
        return

    # Run test suite
    tester = MCPServerTester(
        skip_cleanup=args.skip_cleanup,
        auto_delete_profile=args.auto_delete_profile,
    )
    await tester.run_all_tests()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nTest suite interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nFatal error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
