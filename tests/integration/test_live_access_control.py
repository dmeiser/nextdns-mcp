#!/usr/bin/env python3
"""
Live validation test for NextDNS MCP Server Profile Access Control.

This script validates the profile access control functionality by:
1. Creating multiple test profiles
2. Testing read-only mode
3. Testing readable/writable profile restrictions
4. Verifying access denials return proper 403 responses
5. Cleaning up test profiles

Requirements:
- NEXTDNS_API_KEY environment variable must be set
- Internet connection to NextDNS API
- Valid NextDNS account

Usage:
    # Test with no restrictions (baseline)
    poetry run python tests/integration/test_live_access_control.py
    
    # Test with read-only mode
    NEXTDNS_READ_ONLY=true poetry run python tests/integration/test_live_access_control.py
    
    # Test with specific readable profiles
    NEXTDNS_READABLE_PROFILES=profile1,profile2 poetry run python tests/integration/test_live_access_control.py
    
    # Test with specific writable profiles
    NEXTDNS_WRITABLE_PROFILES=test_profile poetry run python tests/integration/test_live_access_control.py
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded environment from {env_path}")
except ImportError:
    pass

# Set up environment before importing server
if not os.getenv("NEXTDNS_API_KEY"):
    print("ERROR: NEXTDNS_API_KEY environment variable is required")
    print("Set it with: export NEXTDNS_API_KEY='your_key_here'")
    sys.exit(1)


class AccessControlTester:
    """Test profile access control functionality."""

    def __init__(self, auto_cleanup: bool = True):
        """Initialize the tester.

        Args:
            auto_cleanup: If True, automatically delete test profiles after testing
        """
        self.test_profiles: List[Dict[str, str]] = []
        self.tools: Dict = {}
        self.auto_cleanup = auto_cleanup

        # Track test results
        self.passed: List[str] = []
        self.failed: List[Dict[str, str]] = []

    async def initialize(self):
        """Load all tools from the MCP server."""
        # Import server here to allow env var configuration
        from nextdns_mcp import server

        self.tools = await server.mcp.get_tools()
        print(f"✓ Loaded {len(self.tools)} tools from MCP server\n")

    def print_header(self, text: str):
        """Print a section header."""
        print(f"\n{'=' * 80}")
        print(f"  {text}")
        print(f"{'=' * 80}\n")

    def print_test(self, name: str, status: str, details: str = ""):
        """Print test result."""
        symbols = {"PASS": "✓", "FAIL": "✗", "INFO": "ℹ"}
        colors = {"PASS": "\033[92m", "FAIL": "\033[91m", "INFO": "\033[94m"}
        reset = "\033[0m"

        symbol = symbols.get(status, "?")
        color = colors.get(status, "")
        print(f"{color}{symbol}{reset} {name:<60} [{status}]")
        if details:
            print(f"  → {details}")

    async def call_tool(self, tool_name: str, **params):
        """Call an MCP tool and return the result."""
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found in MCP server")

        tool = self.tools[tool_name]
        result = await tool.run(arguments=params)
        return result

    def extract_response_data(self, result) -> any:
        """Extract data from ToolResult response."""
        from fastmcp.tools.tool import ToolResult

        if isinstance(result, ToolResult):
            if result.structured_content:
                return result.structured_content
            if result.content:
                if isinstance(result.content, list):
                    texts = [getattr(block, "text", "") for block in result.content]
                    merged = "".join(texts)
                    try:
                        return json.loads(merged)
                    except json.JSONDecodeError:
                        return merged
                return result.content
        return result

    async def create_test_profile(self, name: str) -> Optional[str]:
        """Create a test profile and return its ID.

        Args:
            name: Name for the profile

        Returns:
            Profile ID if successful, None otherwise
        """
        try:
            result = await self.call_tool("createProfile", name=name)
            data = self.extract_response_data(result)

            if isinstance(data, dict):
                profile_id = data.get("data", {}).get("id") or data.get("id")
                if profile_id:
                    self.test_profiles.append({"id": profile_id, "name": name})
                    self.print_test(f"Create profile: {name}", "PASS", f"ID: {profile_id}")
                    return profile_id

            self.print_test(f"Create profile: {name}", "FAIL", "Could not extract profile ID")
            return None
        except Exception as e:
            self.print_test(f"Create profile: {name}", "FAIL", str(e))
            return None

    async def delete_test_profile(self, profile_id: str, name: str):
        """Delete a test profile.

        Args:
            profile_id: Profile ID to delete
            name: Profile name for logging
        """
        try:
            await self.call_tool("deleteProfile", profile_id=profile_id)
            self.print_test(f"Delete profile: {name}", "PASS", f"ID: {profile_id}")
        except Exception as e:
            self.print_test(f"Delete profile: {name}", "FAIL", str(e))

    def _record_test_success(self, test_name: str, message: str):
        """Record a successful test."""
        self.passed.append(test_name)
        self.print_test(test_name, "PASS", message)

    def _record_test_failure(self, test_name: str, error: str):
        """Record a failed test."""
        self.failed.append({"name": test_name, "error": error})
        self.print_test(test_name, "FAIL", error[:60])

    def _is_access_denied_error(self, error_msg: str) -> bool:
        """Check if error message indicates access denial."""
        return "403" in error_msg or "denied" in error_msg.lower()

    async def test_read_access(self, profile_id: str, should_succeed: bool = True):
        """Test read access to a profile.

        Args:
            profile_id: Profile ID to test
            should_succeed: Whether the access should succeed
        """
        test_name = f"Read access: {profile_id}"
        try:
            await self.call_tool("getProfile", profile_id=profile_id)
            if should_succeed:
                self._record_test_success(f"read_access_{profile_id}", "Access granted")
            else:
                self._record_test_failure(
                    f"read_access_{profile_id}", "Expected 403 but got success"
                )
        except Exception as e:
            error_msg = str(e)
            if not should_succeed and self._is_access_denied_error(error_msg):
                self._record_test_success(f"read_access_{profile_id}", "Access denied as expected")
            else:
                self._record_test_failure(f"read_access_{profile_id}", error_msg)

    async def test_write_access(self, profile_id: str, should_succeed: bool = True):
        """Test write access to a profile.

        Args:
            profile_id: Profile ID to test
            should_succeed: Whether the access should succeed
        """
        test_name = f"Write access: {profile_id}"
        try:
            # Try to update the profile (a write operation)
            await self.call_tool(
                "updateProfile",
                profile_id=profile_id,
                name=f"Test Update {datetime.now().strftime('%H:%M:%S')}",
            )
            if should_succeed:
                self._record_test_success(f"write_access_{profile_id}", "Access granted")
            else:
                self._record_test_failure(
                    f"write_access_{profile_id}", "Expected 403 but got success"
                )
        except Exception as e:
            error_msg = str(e)
            if not should_succeed and self._is_access_denied_error(error_msg):
                self._record_test_success(f"write_access_{profile_id}", "Access denied as expected")
            else:
                self._record_test_failure(f"write_access_{profile_id}", error_msg)

    async def test_list_profiles(self):
        """Test that listProfiles works regardless of restrictions."""
        try:
            result = await self.call_tool("listProfiles")
            data = self.extract_response_data(result)
            if isinstance(data, dict) and "data" in data:
                profile_count = len(data["data"])
                self.passed.append("list_profiles")
                self.print_test("List all profiles", "PASS", f"Found {profile_count} profiles")
            else:
                self.failed.append({"name": "list_profiles", "error": "Unexpected response format"})
                self.print_test("List all profiles", "FAIL", "Unexpected response format")
        except Exception as e:
            self.failed.append({"name": "list_profiles", "error": str(e)})
            self.print_test("List all profiles", "FAIL", str(e)[:60])

    async def run_baseline_tests(self):
        """Run baseline tests with no access restrictions."""
        self.print_header("BASELINE TESTS (No Access Restrictions)")

        # Create 3 test profiles
        profile1 = await self.create_test_profile(
            f"AC Test Profile 1 {datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        await asyncio.sleep(0.5)  # Rate limiting
        profile2 = await self.create_test_profile(
            f"AC Test Profile 2 {datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        await asyncio.sleep(0.5)
        profile3 = await self.create_test_profile(
            f"AC Test Profile 3 {datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )

        if not all([profile1, profile2, profile3]):
            print("\n⚠️  Failed to create test profiles - cannot continue")
            return False

        print("\n--- Testing Global Operations ---")
        await self.test_list_profiles()

        print("\n--- Testing Read Access (should all succeed) ---")
        await self.test_read_access(profile1, should_succeed=True)
        await self.test_read_access(profile2, should_succeed=True)
        await self.test_read_access(profile3, should_succeed=True)

        print("\n--- Testing Write Access (should all succeed) ---")
        await self.test_write_access(profile1, should_succeed=True)
        await asyncio.sleep(0.5)
        await self.test_write_access(profile2, should_succeed=True)
        await asyncio.sleep(0.5)
        await self.test_write_access(profile3, should_succeed=True)

        return True

    async def reinitialize_server_with_env(self, env_vars: Dict[str, str]):
        """Reinitialize the MCP server with new environment variables.

        Args:
            env_vars: Dictionary of environment variables to set
        """
        # Set environment variables
        for key, value in env_vars.items():
            os.environ[key] = value

        # Reload the config module to pick up new environment variables
        import importlib

        from nextdns_mcp import config

        importlib.reload(config)

        # Reinitialize the server
        from nextdns_mcp import server

        importlib.reload(server)

        # Get new tools
        self.tools = await server.mcp.get_tools()
        print(f"  ✓ Reinitialized server with {len(self.tools)} tools")

    async def run_read_only_tests(self):
        """Test read-only mode."""
        if not self.test_profiles or len(self.test_profiles) < 3:
            print("\n⚠️  Need at least 3 test profiles")
            return

        self.print_header("READ-ONLY MODE TESTS")

        # Reinitialize with read-only mode
        await self.reinitialize_server_with_env({"NEXTDNS_READ_ONLY": "true"})

        print("\n--- Configuration ---")
        print("Read-only mode: ENABLED")
        print("Expected: All reads succeed, all writes fail")

        print("\n--- Testing Global Operations ---")
        await self.test_list_profiles()

        print("\n--- Testing Read Access (all should succeed) ---")
        for profile in self.test_profiles[:3]:
            await self.test_read_access(profile["id"], should_succeed=True)

        print("\n--- Testing Write Access (all should fail) ---")
        for profile in self.test_profiles[:3]:
            await self.test_write_access(profile["id"], should_succeed=False)
            await asyncio.sleep(0.3)

    async def run_readable_restriction_tests(self):
        """Test readable profile restrictions."""
        if not self.test_profiles or len(self.test_profiles) < 3:
            print("\n⚠️  Need at least 3 test profiles")
            return

        self.print_header("READABLE PROFILE RESTRICTION TESTS")

        # Allow reading only first 2 profiles
        allowed_profiles = [self.test_profiles[0]["id"], self.test_profiles[1]["id"]]
        restricted_profile = self.test_profiles[2]["id"]

        await self.reinitialize_server_with_env(
            {
                "NEXTDNS_READABLE_PROFILES": ",".join(allowed_profiles),
                "NEXTDNS_WRITABLE_PROFILES": "",
                "NEXTDNS_READ_ONLY": "false",
            }
        )

        print("\n--- Configuration ---")
        print(f"Readable profiles: {', '.join(allowed_profiles)}")
        print(f"Restricted profile: {restricted_profile}")
        print("Expected: Allowed profiles readable/writable, restricted profile denied")

        print("\n--- Testing Global Operations ---")
        await self.test_list_profiles()

        print("\n--- Testing Read Access to Allowed Profiles (should succeed) ---")
        for profile_id in allowed_profiles:
            await self.test_read_access(profile_id, should_succeed=True)

        print("\n--- Testing Read Access to Restricted Profile (should fail) ---")
        await self.test_read_access(restricted_profile, should_succeed=False)

        print("\n--- Testing Write Access to Allowed Profiles (should succeed) ---")
        for profile_id in allowed_profiles:
            await self.test_write_access(profile_id, should_succeed=True)
            await asyncio.sleep(0.3)

    async def run_writable_restriction_tests(self):
        """Test writable profile restrictions."""
        if not self.test_profiles or len(self.test_profiles) < 3:
            print("\n⚠️  Need at least 3 test profiles")
            return

        self.print_header("WRITABLE PROFILE RESTRICTION TESTS")

        # Allow writing only to first profile
        writable_profile = self.test_profiles[0]["id"]
        readonly_profiles = [self.test_profiles[1]["id"], self.test_profiles[2]["id"]]

        await self.reinitialize_server_with_env(
            {
                "NEXTDNS_READABLE_PROFILES": "",
                "NEXTDNS_WRITABLE_PROFILES": writable_profile,
                "NEXTDNS_READ_ONLY": "false",
            }
        )

        print("\n--- Configuration ---")
        print(f"Writable profile: {writable_profile}")
        print(f"Read-only profiles: {', '.join(readonly_profiles)}")
        print("Expected: All readable, only writable profile can be modified")

        print("\n--- Testing Global Operations ---")
        await self.test_list_profiles()

        print("\n--- Testing Read Access to All Profiles (all should succeed) ---")
        for profile in self.test_profiles[:3]:
            await self.test_read_access(profile["id"], should_succeed=True)

        print("\n--- Testing Write Access to Writable Profile (should succeed) ---")
        await self.test_write_access(writable_profile, should_succeed=True)
        await asyncio.sleep(0.3)

        print("\n--- Testing Write Access to Read-Only Profiles (should fail) ---")
        for profile_id in readonly_profiles:
            await self.test_write_access(profile_id, should_succeed=False)
            await asyncio.sleep(0.3)

    async def run_combined_restriction_tests(self):
        """Test combined read and write restrictions."""
        if not self.test_profiles or len(self.test_profiles) < 3:
            print("\n⚠️  Need at least 3 test profiles")
            return

        self.print_header("COMBINED RESTRICTION TESTS")

        # Profile 1: Read and write
        # Profile 2: Read only
        # Profile 3: No access
        read_write_profile = self.test_profiles[0]["id"]
        read_only_profile = self.test_profiles[1]["id"]
        no_access_profile = self.test_profiles[2]["id"]

        await self.reinitialize_server_with_env(
            {
                "NEXTDNS_READABLE_PROFILES": f"{read_write_profile},{read_only_profile}",
                "NEXTDNS_WRITABLE_PROFILES": read_write_profile,
                "NEXTDNS_READ_ONLY": "false",
            }
        )

        print("\n--- Configuration ---")
        print(f"Read+Write: {read_write_profile}")
        print(f"Read-only: {read_only_profile}")
        print(f"No access: {no_access_profile}")

        print("\n--- Testing Global Operations ---")
        await self.test_list_profiles()

        print("\n--- Testing Read+Write Profile ---")
        await self.test_read_access(read_write_profile, should_succeed=True)
        await self.test_write_access(read_write_profile, should_succeed=True)
        await asyncio.sleep(0.3)

        print("\n--- Testing Read-Only Profile ---")
        await self.test_read_access(read_only_profile, should_succeed=True)
        await self.test_write_access(read_only_profile, should_succeed=False)
        await asyncio.sleep(0.3)

        print("\n--- Testing No-Access Profile ---")
        await self.test_read_access(no_access_profile, should_succeed=False)
        await self.test_write_access(no_access_profile, should_succeed=False)

    async def cleanup_profiles(self):
        """Delete all test profiles."""
        if not self.test_profiles:
            return

        self.print_header("CLEANUP")

        if not self.auto_cleanup:
            response = input(f"\nDelete {len(self.test_profiles)} test profiles? (y/N): ")
            if response.lower() != "y":
                print("Skipping cleanup - profiles remain:")
                for profile in self.test_profiles:
                    print(f"  - {profile['name']} (ID: {profile['id']})")
                return

        # Reinitialize server with no restrictions for cleanup
        print("Reinitializing server without restrictions for cleanup...")
        await self.reinitialize_server_with_env(
            {
                "NEXTDNS_READ_ONLY": "false",
                "NEXTDNS_READABLE_PROFILES": "",
                "NEXTDNS_WRITABLE_PROFILES": "",
            }
        )

        print(f"Deleting {len(self.test_profiles)} test profiles...")
        for profile in self.test_profiles:
            await self.delete_test_profile(profile["id"], profile["name"])
            await asyncio.sleep(0.5)  # Rate limiting

        self.test_profiles.clear()

    def print_summary(self):
        """Print test summary."""
        self.print_header("TEST SUMMARY")

        total = len(self.passed) + len(self.failed)
        pass_rate = (len(self.passed) / total * 100) if total > 0 else 0

        print(f"Total tests: {total}")
        print(f"Passed: {len(self.passed)} (\033[92m{pass_rate:.1f}%\033[0m)")
        print(f"Failed: {len(self.failed)} (\033[91m{len(self.failed)}\033[0m)")

        if self.failed:
            print("\n\033[91mFailed tests:\033[0m")
            for failure in self.failed:
                print(f"  - {failure['name']}: {failure['error']}")

        print()


async def main():
    """Main test execution."""
    parser = argparse.ArgumentParser(description="Test NextDNS MCP Server Profile Access Control")
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip automatic cleanup (prompt before deleting profiles)",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip baseline tests (use existing profiles from environment)",
    )
    args = parser.parse_args()

    tester = AccessControlTester(auto_cleanup=not args.no_cleanup)

    try:
        # Initialize MCP server connection
        print("Initializing MCP server connection...")
        await tester.initialize()

        # Run baseline tests (creates profiles)
        if not args.skip_baseline:
            success = await tester.run_baseline_tests()
            if not success:
                print("\n⚠️  Baseline tests failed - cannot continue with restricted tests")
                return 1

        # Run all access control test scenarios
        if tester.test_profiles and len(tester.test_profiles) >= 3:
            print("\n" + "=" * 80)
            print("  RUNNING ACCESS CONTROL TEST SCENARIOS")
            print("=" * 80)

            await tester.run_read_only_tests()
            await tester.run_readable_restriction_tests()
            await tester.run_writable_restriction_tests()
            await tester.run_combined_restriction_tests()
        else:
            print("\n⚠️  Not enough test profiles for access control tests")

        # Print summary
        tester.print_summary()

        # Cleanup
        await tester.cleanup_profiles()

        # Return exit code
        return 0 if len(tester.failed) == 0 else 1

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        await tester.cleanup_profiles()
        return 130
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        import traceback

        traceback.print_exc()
        await tester.cleanup_profiles()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
