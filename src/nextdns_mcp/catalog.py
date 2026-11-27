"""The tool-name catalog: the composite names, the generative-UI partition, and its gating decisions.

SPDX-License-Identifier: MIT

The three hand-written composite operations are named here as static metadata,
so a reader of the catalog - the initialize instructions, the schema validator,
the container pre-flight - derives the published names without importing the
tool modules, whose render path pulls the Prefab UI component tree into the
process (issue #780).

Generative-UI tools partitioned by what they answer with. A content tool answers with an
interactive Prefab UI view, so no payload exists for a schema to describe and schema
validation excuses it from response checks; a data tool answers with a plain JSON payload
whose shape validation pins, because no endpoint returns it. The partition is the server's
classification to make, and it is declared once here, so validation imports it instead of
restating the names.

This module also owns the two gating decisions ``server.py`` used to make: whether the
generative-UI names join the published catalog, and whether the ``PrefabGenerativeUI``
provider is registered on the server. The entry point composes both through
:func:`enabled_generative_ui_tool_names` and :func:`register_generative_ui_provider` and
no longer branches on ``is_generative_ui_enabled()`` itself.
"""

from fastmcp import FastMCP

from .config import is_generative_ui_enabled

# The three composite operations, in registration order. ``server`` imports the
# tool functions at the registration site and asserts the registered functions
# still carry these names, so this copy cannot drift from what is published.
_COMPOSITE_TOOL_NAMES = ("plotAnalytics", "dohLookup", "overviewDashboard")

_GENERATIVE_UI_CONTENT_TOOLS = ("generate_prefab_ui",)
_GENERATIVE_UI_DATA_TOOLS = ("search_prefab_components",)
_GENERATIVE_UI_TOOLS = (*_GENERATIVE_UI_CONTENT_TOOLS, *_GENERATIVE_UI_DATA_TOOLS)


def composite_tool_names() -> tuple[str, ...]:
    """The composite tool names the server registers beside the spec tools.

    Static metadata: reading it imports nothing from the tool layer, so
    schema, catalog, and E2E consumers stay free of the Prefab UI component
    tree (issue #780).
    """
    return _COMPOSITE_TOOL_NAMES


def _generative_ui_enabled(enabled: bool | None) -> bool:
    """Use the decision a server already made, or read it for a direct caller."""
    return is_generative_ui_enabled() if enabled is None else enabled


def generative_ui_tool_names() -> tuple[str, ...]:
    """The generative-UI tool names this server publishes, from its single source.

    Consumers that read the catalog (schema validation's completeness checks, the
    container pre-flight's expected-tool list) go through ``published_tool_names()``;
    this accessor is for the tests and scripts that need these names as a set of
    their own, so no second copy of the tuple can drift from the server that registers them.
    """
    return _GENERATIVE_UI_TOOLS


def generative_ui_content_tool_names() -> tuple[str, ...]:
    """The generative-UI tools that answer with UI content rather than a JSON payload.

    Schema validation imports this for its content-tool excuse: the content/data split
    lives only on the module that owns the catalog partition.
    """
    return _GENERATIVE_UI_CONTENT_TOOLS


def generative_ui_data_tool_names() -> tuple[str, ...]:
    """The generative-UI tools that answer with a plain JSON payload rather than UI content.

    Their answer has no endpoint that returns it, so validation pins the shape; importing
    the names keeps each pin attached to the tool that is actually registered.
    """
    return _GENERATIVE_UI_DATA_TOOLS


def enabled_generative_ui_tool_names(enabled: bool | None = None) -> tuple[str, ...]:
    """The generative-UI names the published catalog includes, decided here.

    The catalog-inclusion gate the entry point used to branch on itself:
    the names join the catalog only when generative UI is enabled, so a
    server built with the feature off publishes exactly the spec and
    composite tools and nothing generative. ``enabled`` is the decision the
    server already made for this build; a direct caller without one still
    reads the setting itself.
    """
    if _generative_ui_enabled(enabled):
        return _GENERATIVE_UI_TOOLS
    return ()


def register_generative_ui_provider(server: FastMCP, enabled: bool | None = None) -> None:
    """Register the generative-UI provider on ``server``, decided here.

    The provider-registration gate the entry point used to branch on itself:
    ``PrefabGenerativeUI`` is imported and added only when generative UI is
    enabled, so the heavy generative stack is never pulled in for a server
    that will not use it. The import stays inside the gate for the same
    reason. The server passes its one already-made decision so the setting is
    not read a second time for this build.
    """
    if _generative_ui_enabled(enabled):
        from .generative import PrefabGenerativeUI

        server.add_provider(PrefabGenerativeUI())
