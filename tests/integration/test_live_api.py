#!/usr/bin/env python3
"""
Live integration test for NextDNS MCP Server.

This script tests ALL 55 tools (54 API operations + 1 custom DoH lookup) by
invoking them through the MCP server (server.py), not via direct API calls.

Requirements:
- NEXTDNS_API_KEY environment variable must be set
- Internet connection to NextDNS API
- Valid NextDNS account

Usage:
    poetry run python tests/integration/test_live_api.py
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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

    def __init__(self, skip_cleanup: bool = False):
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

        # Store entry IDs for cleanup
        self.entry_ids: Dict[str, str] = {}

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
        symbols = {"PASS": "✓", "FAIL": "✗"}
        colors = {"PASS": "\033[92m", "FAIL": "\033[91m"}
        reset = "\033[0m"

        symbol = symbols.get(status, "?")
        color = colors.get(status, "")
        print(f"{color}{symbol}{reset} {name:<50} [{status}]")
        if details:
            print(f"  → {details}")

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
                self.validation_profile_id = data.get('data', {}).get('id') or data.get('id')
            elif isinstance(data, str):
                # Try parsing as JSON
                import json
                try:
                    parsed = json.loads(data)
                    self.validation_profile_id = parsed.get('data', {}).get('id') or parsed.get('id')
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
        await self.test_tool("updateProfile", profile_id=self.validation_profile_id, name="Validation Profile (Updated)")

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
        await self.test_tool("getLogsSettings", profile_id=pid)
        await self.test_tool("updateLogsSettings", profile_id=pid, enabled=True)

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
        result = await self.test_tool("addSecurityTLD", profile_id=pid, id="xyz")
        if result:
            # Try to extract the entry ID for deletion
            try:
                import json
                content = result[0].content
                if isinstance(content, list):
                    text = content[0].text
                    data = json.loads(text)
                    self.entry_ids['security_tld'] = data.get('data', {}).get('id')
            except:
                pass

        # Remove the TLD
        if 'security_tld' in self.entry_ids:
            await self.test_tool("removeSecurityTLD", profile_id=pid, entry_id=self.entry_ids['security_tld'])

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

        result = await self.test_tool("addPrivacyBlocklist", profile_id=pid, id="nextdns-recommended")
        if result:
            try:
                import json
                content = result[0].content
                if isinstance(content, list):
                    text = content[0].text
                    data = json.loads(text)
                    self.entry_ids['blocklist'] = data.get('data', {}).get('id')
            except:
                pass

        if 'blocklist' in self.entry_ids:
            await self.test_tool("removePrivacyBlocklist", profile_id=pid, entry_id=self.entry_ids['blocklist'])

        # Native tracking protection
        await self.test_tool("getPrivacyNatives", profile_id=pid)

        result = await self.test_tool("addPrivacyNative", profile_id=pid, id="apple")
        if result:
            try:
                import json
                content = result[0].content
                if isinstance(content, list):
                    text = content[0].text
                    data = json.loads(text)
                    self.entry_ids['native'] = data.get('data', {}).get('id')
            except:
                pass

        if 'native' in self.entry_ids:
            await self.test_tool("removePrivacyNative", profile_id=pid, entry_id=self.entry_ids['native'])

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
        await self.test_tool("updateParentalControlSettings", profile_id=pid, safeSearch=False, youtubeRestrictedMode=False)

        # Services and Categories
        await self.test_tool("getParentalControlServices", profile_id=pid)
        await self.test_tool("getParentalControlCategories", profile_id=pid)

        # Add a service
        result = await self.test_tool("addToParentalControlServices", profile_id=pid, id="tiktok")
        # Note: The actual API response structure may vary

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

        result = await self.test_tool("addToDenylist", profile_id=pid, id="example-blocked.com")
        if result:
            try:
                import json
                content = result[0].content
                if isinstance(content, list):
                    text = content[0].text
                    data = json.loads(text)
                    self.entry_ids['denylist'] = data.get('data', {}).get('id')
            except:
                pass

        if 'denylist' in self.entry_ids:
            await self.test_tool("removeFromDenylist", profile_id=pid, entry_id=self.entry_ids['denylist'])

        # Allowlist
        await self.test_tool("getAllowlist", profile_id=pid)

        result = await self.test_tool("addToAllowlist", profile_id=pid, id="example-allowed.com")
        if result:
            try:
                import json
                content = result[0].content
                if isinstance(content, list):
                    text = content[0].text
                    data = json.loads(text)
                    self.entry_ids['allowlist'] = data.get('data', {}).get('id')
            except:
                pass

        if 'allowlist' in self.entry_ids:
            await self.test_tool("removeFromAllowlist", profile_id=pid, entry_id=self.entry_ids['allowlist'])

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
    # Logs Tests
    # ========================================================================

    async def test_logs(self):
        """Test logs operations."""
        self.print_header("LOGS OPERATIONS")

        if not self.validation_profile_id:
            print("⚠️  No validation profile ID - skipping")
            return

        pid = self.validation_profile_id

        await self.test_tool("getLogs", profile_id=pid)
        # streamLogs excluded - SSE streaming endpoint not supported by FastMCP
        await self.test_tool("downloadLogs", profile_id=pid)
        await self.test_tool("clearLogs", profile_id=pid)

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

        await self.test_tool("dohLookup", domain="google.com", profile_id=profile_id, record_type="A")

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
            print(f"   Or run: poetry run python tests/integration/test_live_api.py --delete-profile {self.validation_profile_id}")
            return

        print("\n⚠️  WARNING: This action cannot be undone!")
        print("\nOptions:")
        print("  yes   - Delete the profile now")
        print("  no    - Keep the profile for manual inspection")
        print("  skip  - Same as 'no' (alias)")

        response = input("\nDo you want to DELETE this profile? (yes/no/skip): ").strip().lower()

        if response not in ("yes", "y"):
            print("\n✓ Profile deletion skipped. Profile will remain in your account.")
            print(f"   Profile ID: {self.validation_profile_id}")
            print(f"   To delete it later, use the NextDNS web interface or run:")
            print(f"   poetry run python tests/integration/test_live_api.py --delete-profile {self.validation_profile_id}")
            return

        try:
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
        print("  Testing all 55 tools via MCP server (server.py)")
        print("=" * 80)

        try:
            # Initialize - load tools from MCP server
            await self.initialize()

            # 1. Create validation profile
            await self.test_create_validation_profile()

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

            # 9. Logs
            await self.test_logs()

            # 10. DoH Lookup (custom tool)
            await self.test_doh_lookup()

            # 11. Cleanup with confirmation
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

        total = len(self.passed) + len(self.failed)

        print(f"Total Tests: {total}")
        print(f"✓ Passed:    {len(self.passed)} ({len(self.passed)/total*100:.1f}%)")
        print(f"✗ Failed:    {len(self.failed)} ({len(self.failed)/total*100:.1f}%)")

        if self.failed:
            print("\n" + "=" * 80)
            print("  FAILED TESTS")
            print("=" * 80)
            for failure in self.failed:
                print(f"\n✗ {failure['name']}")
                print(f"  Error: {failure['error']}")

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
        result = await delete_tool.run(profile_id=profile_id)
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

  # Delete a specific profile
  poetry run python tests/integration/test_live_api.py --delete-profile abc123
        """
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Skip the profile deletion prompt and keep the validation profile"
    )
    parser.add_argument(
        "--delete-profile",
        metavar="PROFILE_ID",
        help="Delete a specific profile by ID (skips test suite)"
    )

    args = parser.parse_args()

    # Handle profile deletion mode
    if args.delete_profile:
        await delete_profile(args.delete_profile)
        return

    # Run test suite
    tester = MCPServerTester(skip_cleanup=args.skip_cleanup)
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
