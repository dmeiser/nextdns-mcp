"""Tests for server-side validation of generated Prefab trees (SEC-3).

The sandbox may only return passive apps: no embedded actions, no bridge
actions, no external stylesheets, no reserved state keys. This part pins the
allowlist and the typed error for every rejected shape. The provider half
lives in ``test_generative.py`` and the sandbox worker in
``test_generative_sandbox.py``.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prefab_ui.app import PrefabApp

from nextdns_mcp import generative as generative_module
from nextdns_mcp.generative import (
    SANDBOX_PRELUDE,
    PrefabGenerativeUI,
    _validate_generated_tree,
)


async def _tool_map(gen_ui: PrefabGenerativeUI) -> dict[str, Any]:
    """Every tool this provider publishes, keyed by name."""
    return {tool.name: tool for tool in await gen_ui.list_tools()}


def _stub_worker() -> MagicMock:
    """A worker double exposing the lifecycle surface the provider calls."""
    worker = MagicMock(name="worker")
    worker.start = AsyncMock()
    return worker


def _passive_app() -> PrefabApp:
    """A passive PrefabApp shaped like real execute output (from_json of a wire dict)."""
    return PrefabApp.from_json(
        {
            "view": {
                "type": "Column",
                "children": [
                    {"type": "Text", "content": "hello"},
                    {"type": "Link", "content": "docs", "href": "https://nextdns.io"},
                ],
            },
            "state": {"count": 1},
        }
    )


# --- Generated-tree validation (SEC-3, nextdns-mcp#749) ---


def _wire_app(wire: dict[str, Any]) -> PrefabApp:
    """Build the app exactly the way execute does: from_json over a raw wire dict."""
    return PrefabApp.from_json(wire)


def test_sandbox_prelude_is_a_passive_allowlist():
    """The prelude names every import and never exposes Embed to generated code."""
    assert "import *" not in SANDBOX_PRELUDE
    namespace: dict[str, Any] = {}
    exec(SANDBOX_PRELUDE, namespace)  # noqa: S102 - the prelude is a module constant
    assert "Embed" not in namespace
    for name in ("PrefabApp", "Column", "Text", "DataTable", "BarChart", "ChartSeries", "ForEach", "If"):
        assert name in namespace, f"prelude lost {name}"
    # Upstream removed the shadcn tab shims; the native Tabs/Tab names remain.
    assert "TabsList" not in namespace
    assert "TabsTrigger" not in namespace
    assert namespace["Tabs"] is not None
    assert namespace["Tab"] is not None


def test_validate_generated_tree_accepts_passive_app():
    _validate_generated_tree(_passive_app())  # should not raise


def test_validate_generated_tree_rejects_embed_anywhere_in_the_tree():
    wire = {
        "view": {
            "type": "Column",
            "children": [
                {"type": "Text", "content": "ok"},
                {"type": "Row", "children": [{"type": "Embed", "html": "<script>alert(1)</script>"}]},
            ],
        }
    }
    with pytest.raises(ValueError, match="'Embed' is not allowed"):
        _validate_generated_tree(_wire_app(wire))


@pytest.mark.parametrize("action", ["toolCall", "sendMessage", "updateContext", "fetch"])
def test_validate_generated_tree_rejects_bridge_actions(action: str):
    wire = {"view": {"type": "Button", "label": "go", "onClick": {"action": action}}}
    with pytest.raises(ValueError, match=f"action '{action}' is not allowed"):
        _validate_generated_tree(_wire_app(wire))


@pytest.mark.parametrize("action", ["toolCall", "sendMessage", "updateContext", "fetch"])
def test_validate_generated_tree_rejects_bridge_actions_on_mount(action: str):
    """on_mount actions serialize onto the root view's onMount list and are caught there."""
    wire = {
        "view": {
            "type": "Div",
            "onMount": [{"action": action}],
            "children": [{"type": "Text", "content": "ok"}],
        }
    }
    with pytest.raises(ValueError, match=f"action '{action}' is not allowed"):
        _validate_generated_tree(_wire_app(wire))


@pytest.mark.parametrize("href", ["javascript:alert(1)", "http://insecure.example", "data:text/html,x", "{{ link }}"])
def test_validate_generated_tree_rejects_non_https_link_href(href: str):
    wire = {"view": {"type": "Link", "content": "x", "href": href}}
    with pytest.raises(ValueError, match="Link href must be an https:// URL"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_non_https_open_link_action():
    wire = {"view": {"type": "Button", "label": "go", "onClick": {"action": "openLink", "url": "javascript:alert(1)"}}}
    with pytest.raises(ValueError, match="openLink action url must be an https:// URL"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_accepts_https_open_link_action():
    wire = {"view": {"type": "Button", "label": "go", "onClick": {"action": "openLink", "url": "https://nextdns.io"}}}
    _validate_generated_tree(_wire_app(wire))  # should not raise


def test_validate_generated_tree_rejects_stylesheets():
    wire = {"view": {"type": "Text", "content": "ok"}, "stylesheets": ["https://evil.example/x.css"]}
    with pytest.raises(ValueError, match="external stylesheets are not allowed"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_inline_css():
    wire = {"view": {"type": "Text", "content": "ok"}, "css": ["body{display:none}"]}
    with pytest.raises(ValueError, match="inline CSS is not allowed"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_theme_css():
    """Theme CSS serializes into the wire css field, so it is rejected like inline CSS."""
    wire = {"view": {"type": "Text", "content": "ok"}, "theme": {"light": "body{display:none}"}}
    with pytest.raises(ValueError, match="inline CSS is not allowed"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_reserved_initial_state_keys():
    wire = {"view": {"type": "Text", "content": "ok"}, "state": {"$internal": 1}}
    with pytest.raises(ValueError, match=r"reserved '\$'-prefixed state key"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_reserved_action_state_key():
    wire = {
        "view": {"type": "Button", "label": "go", "onClick": {"action": "setState", "key": "$internal", "value": 1}}
    }
    with pytest.raises(ValueError, match=r"reserved '\$' state-key prefix"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_reserved_component_state_key():
    wire = {"view": {"type": "Slider", "name": "$internal", "value": 5}}
    with pytest.raises(ValueError, match=r"reserved '\$' state-key prefix"):
        _validate_generated_tree(_wire_app(wire))


def test_validate_generated_tree_rejects_non_app_results():
    with pytest.raises(ValueError, match="expected the sandbox to return a Prefab app"):
        _validate_generated_tree("not-an-app")


def test_validate_generated_tree_tolerates_non_string_type_fields_in_data():
    """Passive dashboards may carry list/dict "type" fields in state or rows.

    The walker visits every dict in the wire envelope, so a DNS record-type
    filter like {"type": ["A", "AAAA"]} must not crash the set-membership
    check with an unhashable-type TypeError.
    """
    wire = {
        "view": {
            "type": "DataTable",
            "columns": [{"key": "domain"}, {"key": "type"}],
            "rows": [
                {"domain": "example.com", "type": ["A", "AAAA"]},
                {"domain": "example.org", "type": {"kind": "MX"}},
            ],
        },
        "state": {"filters": {"type": ["A", "AAAA"]}},
    }
    _validate_generated_tree(_wire_app(wire))  # should not raise


def test_validate_generated_tree_ignores_crafted_non_string_component_type():
    """A crafted {"type": ["Embed"]} node is not a renderable component type.

    The renderer schema-validates node.type as a string literal, so the
    validator's contract is to neither crash nor misclassify it.
    """
    wire = {"view": {"type": "Column", "children": [{"type": ["Embed"]}, {"type": {"$ref": "Embed"}}]}}
    _validate_generated_tree(_wire_app(wire))  # should not raise


def test_validate_generated_tree_walks_deeply_nested_trees_without_recursion():
    """A tree deeper than the recursion limit is validated iteratively, not crashed."""
    node: dict[str, Any] = {"type": "Text", "content": "leaf"}
    for _ in range(5000):
        node = {"type": "Column", "children": [node]}
    _validate_generated_tree(_wire_app({"view": node}))  # should not raise

    poisoned: dict[str, Any] = {"type": "Embed", "url": "https://evil.example"}
    for _ in range(5000):
        poisoned = {"type": "Column", "children": [poisoned]}
    with pytest.raises(ValueError, match="'Embed' is not allowed"):
        _validate_generated_tree(_wire_app({"view": poisoned}))


@pytest.mark.asyncio
async def test_generate_ui_rejects_active_tree_from_sandbox():
    """The tool rejects a sandbox result carrying active content before returning it."""
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    active = _wire_app({"view": {"type": "Embed", "html": "<script>alert(1)</script>"}})
    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
    ):
        mock_exec.return_value = active
        with pytest.raises(ValueError, match="Generated UI rejected"):
            await gen_tool.fn(code="print(1)")


@pytest.mark.asyncio
async def test_generate_ui_returns_validated_passive_tree():
    gen_ui = PrefabGenerativeUI()
    gen_tool = (await _tool_map(gen_ui))["generate_prefab_ui"]
    passive = _passive_app()
    with (
        patch.object(generative_module.prefab_generative, "execute", new_callable=AsyncMock) as mock_exec,
        patch.object(gen_ui, "_new_worker", return_value=_stub_worker()),
    ):
        mock_exec.return_value = passive
        assert await gen_tool.fn(code="print(1)") is passive
