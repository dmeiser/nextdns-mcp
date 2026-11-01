"""Doc-surface tests for the early-access security toggles (issue: spec discoverability).

The five keys below are community-reported NextDNS dashboard toggles that the
official API docs do not list yet. They are reachable because
``updateSecuritySettings`` PATCHes whatever the caller sends, and the document
is the tool contract: the only thing that can regress is their discoverability
in the vendored OpenAPI schema and the tool schema the server serves from it.
These tests pin that surface. No live API is involved.
"""

from pathlib import Path

import yaml

from nextdns_mcp.spec_tools import spec_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "src" / "nextdns_mcp" / "nextdns-openapi.yaml"

EARLY_ACCESS_TOGGLES = [
    "dnsDataExfiltration",
    "dnsPayloadDelivery",
    "fastFluxNetworks",
    "highRiskTlds",
    "newlyActiveDomains",
]

DOCUMENTED_TOGGLES = [
    "threatIntelligenceFeeds",
    "aiThreatDetection",
    "googleSafeBrowsing",
    "cryptojacking",
    "dnsRebinding",
    "homographs",
    "typosquatting",
    "dga",
    "nrd",
    "parked",
    "csam",
]


def _load_spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def _security_schemas() -> dict[str, dict]:
    """Return the security settings schemas keyed by operation (get response, patch body)."""
    spec = _load_spec()
    security = spec["paths"]["/profiles/{profile_id}/security"]
    return {
        "get": security["get"]["responses"]["200"]["content"]["application/json"]["schema"],
        "patch": security["patch"]["requestBody"]["content"]["application/json"]["schema"],
    }


def test_security_schemas_declare_early_access_toggles():
    """Both the GET response and PATCH request schemas list all five new keys."""
    for operation, schema in _security_schemas().items():
        properties = schema["properties"]
        for key in EARLY_ACCESS_TOGGLES:
            assert key in properties, f"{key} missing from {operation} security schema"
            assert properties[key]["type"] == "boolean", f"{key} must be boolean in {operation}"


def test_early_access_toggles_match_existing_toggle_style():
    """New keys follow the existing toggle style: bare booleans, no defaults, not required.

    The documented toggles carry no ``default`` and are not listed as ``required``,
    so the early-access keys must not introduce either, and must not be typed
    differently from the keys they sit next to.
    """
    for operation, schema in _security_schemas().items():
        properties = schema["properties"]
        reference = {key: properties[key] for key in DOCUMENTED_TOGGLES}
        assert all(node.get("type") == "boolean" for node in reference.values())
        assert all("default" not in node for node in reference.values())

        for key in EARLY_ACCESS_TOGGLES:
            node = properties[key]
            assert node.get("type") == "boolean"
            assert "default" not in node, f"{key} must not declare a default in {operation}"

        assert "required" not in schema or not set(EARLY_ACCESS_TOGGLES) & set(schema["required"])


def test_early_access_toggles_are_flagged_as_undocumented():
    """Each new key's description says it is early-access and not in the official docs."""
    for operation, schema in _security_schemas().items():
        for key in EARLY_ACCESS_TOGGLES:
            description = schema["properties"][key].get("description", "")
            assert "early-access" in description, f"{key} description must mark it early-access ({operation})"
            assert "not yet" in description, f"{key} description must state it is undocumented ({operation})"


def test_served_update_tool_schema_surfaces_toggles():
    """The generated tool schema the server publishes — what an LLM actually reads —
    carries all five toggles as arguments, flattened from the document's body."""
    routes = {route.operation_id: route for route in spec_routes()}
    properties = routes["updateSecuritySettings"].flat_param_schema["properties"]
    for key in EARLY_ACCESS_TOGGLES:
        assert key in properties, f"updateSecuritySettings schema must expose {key}"
        assert properties[key]["type"] == "boolean", f"{key} must be boolean in the served schema"
