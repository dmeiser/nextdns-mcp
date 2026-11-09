"""What this server says about itself in the MCP ``initialize`` handshake.

SPDX-License-Identifier: MIT

The instructions field is the spec's own server-to-client channel: clients put it in
the model's context, so this is where the server describes itself — what it exposes
and what its responses look like. It stays client-agnostic and it does not instruct
a model how to behave; that is not a DNS server's business, and a line like "do not
undo your own writes" would be a test harness's failure mode speaking through the
product.

The detailed guide this module used to publish (as a prompt and a ``nextdns://usage``
resource) walked the eight grouped tools and their ``operation`` argument. That
catalog is gone, and a guide describing tools the caller was never given is worse
than no guide: the contract now lives in the OpenAPI document each tool is generated
from, and each tool's own description carries what its endpoint accepts.
"""

import textwrap
from collections.abc import Sequence

# Below this many tools, the instructions describe the catalog instead of
# pointing at search: the hop costs a caller more than the context it saves.
ADVERTISE_CATALOG_MAX_TOOLS = 12


def _catalog_advertisement(tool_names: Sequence[str]) -> list[str]:
    """Instruction lines telling a disclosing client where tool names live.

    A catalog of ``ADVERTISE_CATALOG_MAX_TOOLS`` or fewer gets its names stated
    outright; above that the instructions point at ``search_tools``.
    """
    if tool_names and len(tool_names) <= ADVERTISE_CATALOG_MAX_TOOLS:
        listed = textwrap.fill(
            ", ".join(f"`{name}`" for name in tool_names),
            width=72,
            initial_indent="  ",
            subsequent_indent="  ",
        )
        return [
            "- Its tools, each by the exact name `call_tool` takes it:",
            listed,
            "- `search_tools` matches the words of a task to the tools that fit",
            "  it and returns each one with its full schema.",
        ]
    return [
        "- This server does not advertise tool names. `search_tools` matches the",
        "  words of a task to the tools that fit it and returns their exact names",
        "  and schemas; `call_tool` runs one of those by that exact name.",
    ]


def server_instructions(tool_search_mode: str, tool_names: Sequence[str] = ()) -> str:
    """Short instructions sent in the MCP ``initialize`` handshake.

    Whether the instructions name their tools is a size question, not a secret.
    Progressive disclosure exists so a large catalog does not eat context; with a
    catalog of ``ADVERTISE_CATALOG_MAX_TOOLS`` or fewer, the names are cheaper to
    state than the search every caller would otherwise have to make to find them,
    so they are stated. Above that, the instructions point at ``search_tools`` and
    ``tools/list`` stays the only place the names appear. Either way this is the
    server choosing what it advertises about itself.

    ``tool_search_mode`` is the resolved NEXTDNS_TOOL_SEARCH value, so the
    discovery mechanism is only described when discovery is actually required.
    """
    lines = [
        "# NextDNS MCP server",
        "",
        "This server exposes the NextDNS API as one tool per endpoint. A tool is",
        "named for the API operation it performs (`getAllowlist`, `addToDenylist`,",
        "`getAnalyticsDevices`) and takes that endpoint's own arguments: `profile_id`",
        "plus the path, query and body fields the endpoint documents. The name says",
        "what the call does, so there is no operation to choose.",
        "",
        "- Composite visual tools (`overviewDashboard`, specialized widgets, `plotAnalytics`)",
        "  render interactive charts, tables, and dashboards directly in the client interface;",
        "  `dohLookup` resolves a name through a profile's DNS-over-HTTPS resolver. Visual tools",
        "  render directly where charts and tables are already visible.",
        "  When writing Prefab UI code, place charts in Column, Card, or Grid containers",
        "  rather than inside Tabs to ensure proper SVG dimensions.",
        "- A failed call returns a JSON payload with an error `code`, plus a `hint`",
        "  whenever the server can say what to send instead.",
    ]
    if tool_search_mode and tool_search_mode != "off":
        lines += _catalog_advertisement(tool_names)
    return "\n".join(lines)
