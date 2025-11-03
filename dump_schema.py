#!/usr/bin/env python3
"""Dump tool schemas from MCP server."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from mcp.server import stdio_server
from nextdns_mcp.server import mcp


async def dump_schemas():
    """Dump all tool schemas."""
    # Use server's list_tools handler
    from mcp.server.models import ListToolsRequest
    from mcp.types import Tool
    
    # Get tools via internal API
    tools_result = await mcp.server.list_tools()
    
    # Find updateBlockPageSettings
    for tool in tools_result.tools:
        if tool.name == "updateBlockPageSettings":
            print(f"Tool: {tool.name}")
            print(f"Description: {tool.description}")
            print(f"\nInput Schema:")
            print(json.dumps(tool.inputSchema, indent=2))
            break
    else:
        print("Tool not found")
        print(f"Available tools: {[t.name for t in tools_result.tools[:10]]}")


if __name__ == "__main__":
    asyncio.run(dump_schemas())
