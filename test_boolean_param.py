#!/usr/bin/env python3
"""Test boolean parameter handling via MCP protocol."""

import asyncio
import json
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from nextdns_mcp.server import mcp


async def test_boolean_param():
    """Test updateBlockPageSettings with boolean parameter."""
    profile_id = os.environ.get("TEST_PROFILE_ID", "9d13f9")
    
    print(f"Testing updateBlockPageSettings with profile_id={profile_id}")
    
    # Get the tool
    tools = await mcp._list_tools()
    update_tool = next((t for t in tools if t.name == "updateBlockPageSettings"), None)
    if not update_tool:
        print("ERROR: updateBlockPageSettings tool not found")
        print(f"Available tools: {[t.name for t in tools[:5]]}")
        return False
    
    print(f"\nTool schema:")
    print(json.dumps(update_tool.inputSchema, indent=2))
    
    # Test with boolean True
    print(f"\n=== Test 1: enabled=True (boolean) ===")
    try:
        result = await mcp._call_tool(
            "updateBlockPageSettings",
            {"profile_id": profile_id, "enabled": True}
        )
        print(f"SUCCESS: {result}")
    except Exception as e:
        print(f"FAILED: {e}")
        return False
    
    # Test with boolean False
    print(f"\n=== Test 2: enabled=False (boolean) ===")
    try:
        result = await mcp._call_tool(
            "updateBlockPageSettings",
            {"profile_id": profile_id, "enabled": False}
        )
        print(f"SUCCESS: {result}")
    except Exception as e:
        print(f"FAILED: {e}")
        return False
    
    # Test with string "true" (should fail or auto-convert)
    print(f"\n=== Test 3: enabled='true' (string) - should fail ===")
    try:
        result = await mcp._call_tool(
            "updateBlockPageSettings",
            {"profile_id": profile_id, "enabled": "true"}
        )
        print(f"Result: {result}")
        print("NOTE: String was accepted (FastMCP auto-converted)")
    except Exception as e:
        print(f"FAILED as expected: {e}")
    
    return True


if __name__ == "__main__":
    success = asyncio.run(test_boolean_param())
    sys.exit(0 if success else 1)
