#!/usr/bin/env python3
"""
Unified Integration Test Runner for NextDNS MCP Server.

This script runs all integration tests in a coordinated manner:
1. Server initialization tests (pytest-based)
2. Live API validation (all 76 tools)
3. Access control validation (all scenarios)

Usage:
    # Run all integration tests
    poetry run python tests/integration/run_integration_tests.py

    # Run only specific test suites
    poetry run python tests/integration/run_integration_tests.py --only-server-init
    poetry run python tests/integration/run_integration_tests.py --only-api
    poetry run python tests/integration/run_integration_tests.py --only-access-control

    # Skip specific test suites
    poetry run python tests/integration/run_integration_tests.py --skip-server-init
    poetry run python tests/integration/run_integration_tests.py --skip-api
    poetry run python tests/integration/run_integration_tests.py --skip-access-control

    # Auto-cleanup without prompts
    poetry run python tests/integration/run_integration_tests.py --auto-cleanup
"""

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass  # python-dotenv not installed, skip

# ANSI color codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


class IntegrationTestRunner:
    """Orchestrates all integration tests."""

    def __init__(self, args):
        self.args = args
        self.results: Dict[str, bool] = {}
        self.tests_dir = Path(__file__).parent
        self.project_root = self.tests_dir.parent.parent

    def print_header(self, text: str, color: str = BLUE):
        """Print a section header."""
        print(f"\n{color}{'=' * 80}{RESET}")
        print(f"{color}{BOLD}  {text}{RESET}")
        print(f"{color}{'=' * 80}{RESET}\n")

    def print_result(self, name: str, success: bool):
        """Print test result."""
        symbol = f"{GREEN}✓{RESET}" if success else f"{RED}✗{RESET}"
        status = f"{GREEN}PASSED{RESET}" if success else f"{RED}FAILED{RESET}"
        print(f"{symbol} {name:<50} [{status}]")

    def run_command(self, cmd: List[str], description: str) -> bool:
        """Run a command and return success status."""
        print(f"\n{BLUE}→ {description}{RESET}")
        print(f"  Command: {' '.join(cmd)}\n")

        try:
            result = subprocess.run(cmd, cwd=self.project_root, capture_output=False, text=True)
            return result.returncode == 0
        except Exception as e:
            print(f"{RED}Error running command: {e}{RESET}")
            return False

    def run_server_init_tests(self) -> bool:
        """Run pytest-based server initialization tests."""
        self.print_header("SERVER INITIALIZATION TESTS")

        cmd = [
            "poetry",
            "run",
            "pytest",
            "tests/integration/test_server_init.py",
            "-v",
            "--tb=short",
        ]

        success = self.run_command(cmd, "Running server initialization tests with pytest")
        self.results["Server Initialization"] = success
        return success

    async def run_api_validation(self) -> bool:
        """Run live API validation (all 76 tools)."""
        self.print_header("LIVE API VALIDATION (76 Tools)")

        # Import and run the test
        try:
            # Add script directory to path so we can import the module
            sys.path.insert(0, str(self.tests_dir))

            # Run the live API test
            from test_live_api import main as api_main

            # Build args for the API test
            api_args = []
            if self.args.auto_cleanup:
                api_args.append("--auto-delete-profile")

            # Override sys.argv for the API test
            old_argv = sys.argv
            sys.argv = ["test_live_api.py"] + api_args

            try:
                result = await api_main()
                success = result == 0
            finally:
                sys.argv = old_argv

            self.results["Live API Validation"] = success
            return success
        except Exception as e:
            print(f"{RED}Error running API validation: {e}{RESET}")
            import traceback

            traceback.print_exc()
            self.results["Live API Validation"] = False
            return False

    async def run_access_control_validation(self) -> bool:
        """Run access control validation (all scenarios)."""
        self.print_header("ACCESS CONTROL VALIDATION")

        try:
            # Import and run the test
            sys.path.insert(0, str(self.tests_dir))

            from test_live_access_control import main as ac_main

            # Build args for the access control test
            ac_args = []
            if self.args.auto_cleanup:
                # Access control test uses auto_cleanup by default
                pass
            else:
                ac_args.append("--no-cleanup")

            # Override sys.argv
            old_argv = sys.argv
            sys.argv = ["test_live_access_control.py"] + ac_args

            try:
                result = await ac_main()
                success = result == 0
            finally:
                sys.argv = old_argv

            self.results["Access Control Validation"] = success
            return success
        except Exception as e:
            print(f"{RED}Error running access control validation: {e}{RESET}")
            import traceback

            traceback.print_exc()
            self.results["Access Control Validation"] = False
            return False

    def _calculate_summary_stats(self) -> tuple[int, int]:
        """Calculate total and passed counts."""
        total = len(self.results)
        passed = sum(1 for success in self.results.values() if success)
        return total, passed

    def _print_summary_header(self, all_passed: bool):
        """Print the summary header."""
        color = GREEN if all_passed else RED
        self.print_header("INTEGRATION TEST SUMMARY", color)

    def _print_summary_stats(self, total: int, passed: int):
        """Print summary statistics."""
        failed = total - passed
        print(f"Total test suites: {total}")
        print(f"Passed: {passed} ({GREEN}{passed}/{total}{RESET})")
        fail_color = GREEN if failed == 0 else RED
        print(f"Failed: {failed} ({fail_color}{failed}{RESET})")
        print()

    def _print_summary_results(self):
        """Print individual test results."""
        for name, success in self.results.items():
            self.print_result(name, success)
        print()

    def _print_summary_conclusion(self, all_passed: bool) -> int:
        """Print conclusion and return exit code."""
        if all_passed:
            print(f"{GREEN}{BOLD}🎉 All integration tests passed!{RESET}\n")
            return 0
        else:
            print(f"{RED}{BOLD}❌ Some integration tests failed{RESET}\n")
            return 1

    def print_summary(self):
        """Print overall test summary."""
        all_passed = all(self.results.values())
        total, passed = self._calculate_summary_stats()

        self._print_summary_header(all_passed)
        self._print_summary_stats(total, passed)
        self._print_summary_results()

        return self._print_summary_conclusion(all_passed)

    def _check_prerequisites(self) -> bool:
        """Check if prerequisites are met."""
        if not os.getenv("NEXTDNS_API_KEY"):
            print(f"{RED}ERROR: NEXTDNS_API_KEY environment variable is required{RESET}")
            print("Set it with: export NEXTDNS_API_KEY='your_key_here'")
            return False
        return True

    def _determine_tests_to_run(self) -> tuple[bool, bool, bool]:
        """Determine which test suites to run based on flags.

        Returns:
            Tuple of (run_server_init, run_api, run_access_control)
        """
        # Determine which tests to run
        run_all = not any(
            [
                self.args.only_server_init,
                self.args.only_api,
                self.args.only_access_control,
            ]
        )

        run_server_init = run_all or self.args.only_server_init
        run_api = run_all or self.args.only_api
        run_access_control = run_all or self.args.only_access_control

        # Apply skip flags
        if self.args.skip_server_init:
            run_server_init = False
        if self.args.skip_api:
            run_api = False
        if self.args.skip_access_control:
            run_access_control = False

        return run_server_init, run_api, run_access_control

    async def run(self) -> int:
        """Run all selected integration tests."""
        if not self._check_prerequisites():
            return 1

        run_server_init, run_api, run_access_control = self._determine_tests_to_run()

        # Run selected tests
        if run_server_init:
            self.run_server_init_tests()

        if run_api:
            await self.run_api_validation()

        if run_access_control:
            await self.run_access_control_validation()

        # Print summary
        return self.print_summary()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Run NextDNS MCP Server Integration Tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all integration tests
  poetry run python tests/integration/run_integration_tests.py

  # Run only API validation
  poetry run python tests/integration/run_integration_tests.py --only-api

  # Run only access control tests
  poetry run python tests/integration/run_integration_tests.py --only-access-control

  # Skip server init tests
  poetry run python tests/integration/run_integration_tests.py --skip-server-init

  # Auto-cleanup test profiles without prompts
  poetry run python tests/integration/run_integration_tests.py --auto-cleanup
        """,
    )

    # Test selection
    selection_group = parser.add_argument_group("Test Selection")
    selection_group.add_argument(
        "--only-server-init", action="store_true", help="Run only server initialization tests"
    )
    selection_group.add_argument(
        "--only-api", action="store_true", help="Run only live API validation tests (76 tools)"
    )
    selection_group.add_argument(
        "--only-access-control",
        action="store_true",
        help="Run only access control validation tests",
    )

    # Test exclusion
    exclusion_group = parser.add_argument_group("Test Exclusion")
    exclusion_group.add_argument(
        "--skip-server-init", action="store_true", help="Skip server initialization tests"
    )
    exclusion_group.add_argument(
        "--skip-api", action="store_true", help="Skip live API validation tests"
    )
    exclusion_group.add_argument(
        "--skip-access-control", action="store_true", help="Skip access control validation tests"
    )

    # Options
    options_group = parser.add_argument_group("Options")
    options_group.add_argument(
        "--auto-cleanup",
        action="store_true",
        help="Automatically delete test profiles without prompting",
    )

    args = parser.parse_args()

    # Validate argument combinations
    only_flags = [args.only_server_init, args.only_api, args.only_access_control]
    skip_flags = [args.skip_server_init, args.skip_api, args.skip_access_control]

    if sum(only_flags) > 1:
        parser.error("Cannot use multiple --only-* flags together")

    if any(only_flags) and any(skip_flags):
        parser.error("Cannot use --only-* and --skip-* flags together")

    # Run tests
    runner = IntegrationTestRunner(args)
    return asyncio.run(runner.run())


if __name__ == "__main__":
    sys.exit(main())
