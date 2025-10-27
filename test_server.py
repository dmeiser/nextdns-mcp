#!/usr/bin/env python3
"""Quick test script to verify the NextDNS MCP server is working."""

import asyncio
import os
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

# Load environment
load_dotenv()

NEXTDNS_API_KEY = os.getenv("NEXTDNS_API_KEY")
NEXTDNS_BASE_URL = "https://api.nextdns.io"


async def test_nextdns_api():
    """Test direct connection to NextDNS API."""
    print("Testing direct NextDNS API connection...")
    print(f"  Base URL: {NEXTDNS_BASE_URL}")
    print(f"  API Key: {NEXTDNS_API_KEY[:10]}..." if NEXTDNS_API_KEY else "  API Key: NOT SET")

    if not NEXTDNS_API_KEY:
        print("❌ NEXTDNS_API_KEY not set in .env file")
        return False

    async with httpx.AsyncClient(
        base_url=NEXTDNS_BASE_URL, headers={"X-Api-Key": NEXTDNS_API_KEY}, timeout=30.0
    ) as client:
        try:
            # Test listing profiles
            response = await client.get("/profiles")
            response.raise_for_status()
            data = response.json()

            profiles = data.get("data", [])
            print(f"✓ Successfully connected to NextDNS API")
            print(f"  Found {len(profiles)} profile(s)")

            if profiles:
                for profile in profiles:
                    print(f"    - {profile.get('id')}: {profile.get('name', 'Unnamed')}")

            return True

        except httpx.HTTPStatusError as e:
            print(f"❌ HTTP error: {e.response.status_code} {e.response.text}")
            return False
        except Exception as e:
            print(f"❌ Error: {str(e)}")
            return False


def verify_openapi_spec():
    """Verify the OpenAPI spec is valid."""
    print("\nVerifying OpenAPI specification...")

    spec_path = Path(__file__).parent / "nextdns-openapi.yaml"
    if not spec_path.exists():
        print(f"❌ OpenAPI spec not found at {spec_path}")
        return False

    try:
        with open(spec_path, "r") as f:
            spec = yaml.safe_load(f)

        # Verify key fields
        assert spec.get("openapi") == "3.0.3", "Not OpenAPI 3.0.3"
        assert "info" in spec, "Missing info section"
        assert "paths" in spec, "Missing paths section"

        num_paths = len(spec["paths"])
        print(f"✓ OpenAPI spec is valid")
        print(f"  Version: {spec['openapi']}")
        print(f"  Title: {spec['info']['title']}")
        print(f"  Paths: {num_paths}")

        # Count operations
        num_operations = 0
        for path, methods in spec["paths"].items():
            num_operations += len(
                [m for m in methods if m in ["get", "post", "patch", "delete", "put"]]
            )

        print(f"  Operations: {num_operations}")

        return True

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False


def verify_docker_image():
    """Verify Docker image exists."""
    print("\nVerifying Docker image...")

    import subprocess

    try:
        result = subprocess.run(
            ["docker", "images", "nextdns-mcp:latest", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            check=True,
        )

        if "nextdns-mcp:latest" in result.stdout:
            print("✓ Docker image 'nextdns-mcp:latest' exists")

            # Get image details
            details = subprocess.run(
                ["docker", "images", "nextdns-mcp:latest", "--format", "{{.Size}}"],
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"  Size: {details.stdout.strip()}")

            return True
        else:
            print("❌ Docker image 'nextdns-mcp:latest' not found")
            return False

    except subprocess.CalledProcessError as e:
        print(f"❌ Error checking Docker image: {str(e)}")
        return False
    except FileNotFoundError:
        print("❌ Docker command not found")
        return False


async def main():
    """Run all tests."""
    print("=" * 70)
    print("NextDNS MCP Server - Verification Tests")
    print("=" * 70)

    results = {
        "openapi_spec": verify_openapi_spec(),
        "nextdns_api": await test_nextdns_api(),
        "docker_image": verify_docker_image(),
    }

    print("\n" + "=" * 70)
    print("Test Results Summary")
    print("=" * 70)

    for test_name, passed in results.items():
        status = "✓ PASS" if passed else "❌ FAIL"
        print(f"  {test_name.replace('_', ' ').title()}: {status}")

    all_passed = all(results.values())

    print("\n" + "=" * 70)
    if all_passed:
        print("✓ All tests passed! The NextDNS MCP Server is ready to use.")
    else:
        print("❌ Some tests failed. Please check the errors above.")
    print("=" * 70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
