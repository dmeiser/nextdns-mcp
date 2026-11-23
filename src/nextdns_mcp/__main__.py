"""NextDNS MCP Server package entrypoint: ``python -m nextdns_mcp``.

Applies the documented environment defaults *before* importing the server
module. This ordering matters because FastMCP builds its settings object at
package import time and reads ``FASTMCP_CHECK_FOR_UPDATES`` only then:
defaulting it to ``"off"`` works without mutating FastMCP's live settings
object only if the variable is already set when ``fastmcp`` is first imported
(issue #643). The ``python -m nextdns_mcp.server`` module entrypoint cannot
do that (it imports fastmcp before its own ``__main__`` block runs), so it
re-executes through this entrypoint whenever the variable was not exported by
the parent shell. An explicit operator value always wins, and ``configure()``
applies the same default for callers that use it directly.

Importing this module is inert; only ``main()`` (the ``__main__`` path) loads
the server and its FastMCP dependency.

SPDX-License-Identifier: MIT
"""

import os

from dotenv import load_dotenv


def main() -> None:
    """Load the operator's .env, default the update check off, and run the server."""
    load_dotenv()
    os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
    from nextdns_mcp.server import _run_server

    _run_server()


if __name__ == "__main__":  # pragma: no cover
    # Note: excluded from coverage because:
    # 1. It only executes when running `python -m nextdns_mcp`
    # 2. When pytest imports the module, __name__ != "__main__"
    # The body of main() IS covered: tests/unit/test_server_dotenv.py calls it
    # directly, and the import-before-FastMCP ordering it exists for is
    # exercised in a fresh interpreter there.
    main()
