"""The generative-UI tool catalog partition.

SPDX-License-Identifier: MIT

Generative-UI tools partitioned by what they answer with. A content tool answers with an
interactive Prefab UI view, so no payload exists for a schema to describe and schema
validation excuses it from response checks; a data tool answers with a plain JSON payload
whose shape validation pins, because no endpoint returns it. The partition is the server's
classification to make, and it is declared once here, so validation imports it instead of
restating the names. ``server.py`` decides catalog inclusion from these names and
registers the provider; the names themselves live here so the classification is attached
to one module only.
"""

_GENERATIVE_UI_CONTENT_TOOLS = ("generate_prefab_ui",)
_GENERATIVE_UI_DATA_TOOLS = ("search_prefab_components",)
_GENERATIVE_UI_TOOLS = (*_GENERATIVE_UI_CONTENT_TOOLS, *_GENERATIVE_UI_DATA_TOOLS)


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
