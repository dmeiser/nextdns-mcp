# ---
# Type coercion helpers for MCP tool arguments and JSON request bodies.
#
# Docker MCP CLI passes values as strings (e.g. "true" instead of true). The
# helpers here coerce those strings to proper Python types.
# ruff: noqa: E402
"""Type coercion utilities for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Annotated, Any, Optional

# Pydantic import for allow_extra_fields_component_fn and BeforeValidator
try:
    from pydantic import BeforeValidator
except ImportError:  # pragma: no cover
    BeforeValidator = None  # type: ignore


# Profile IDs from docker MCP CLI may arrive as integers when the 6-char hex ID
# happens to contain only decimal digits (e.g., "315244"). Use BeforeValidator
# to coerce int inputs to str while preserving None for the default-profile fallback.
def _coerce_profile_id(v: object) -> object:
    """Coerce non-None profile_id values to str; leave None as-is."""
    return str(v) if v is not None else v


_coerce_to_str = BeforeValidator(_coerce_profile_id) if BeforeValidator is not None else lambda x: x
OptionalProfileId = Annotated[Optional[str], _coerce_to_str]
ProfileId = Annotated[str, _coerce_to_str]


def _coerce_string_to_bool(value: str) -> bool | None:
    """Try to coerce a string to boolean.

    Args:
        value: String value to coerce

    Returns:
        Boolean value or None if not a boolean string
    """
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _is_integer(value: str) -> bool:
    """Check if string represents an integer."""
    return value.isdigit() or (value.startswith("-") and value[1:].isdigit())


def _try_parse_float(value: str) -> float | None:
    """Try to parse string as float."""
    if value.replace(".", "", 1).replace("-", "", 1).isdigit():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_string_to_number(value: str) -> int | float | None:
    """Try to coerce a string to int or float.

    Args:
        value: String value to coerce

    Returns:
        Int, float, or None if not a number string
    """
    if _is_integer(value):
        return int(value)
    return _try_parse_float(value)


def _coerce_string(value: str) -> Any:
    """Coerce a string to bool or number if possible."""
    bool_value = _coerce_string_to_bool(value)
    if bool_value is not None:
        return bool_value

    num_value = _coerce_string_to_number(value)
    if num_value is not None:
        return num_value

    return value


def _coerce_dict(data: dict[Any, Any]) -> dict[Any, Any]:
    """Recursively coerce dictionary values."""
    return {key: coerce_json_types(value) for key, value in data.items()}


def _coerce_list(data: list[Any]) -> list[Any]:
    """Recursively coerce list items."""
    return [coerce_json_types(item) for item in data]


def coerce_json_types(data: Any) -> Any:
    """Coerce string representations to proper JSON types.

    This handles type coercion for parameters passed as strings by Docker MCP CLI.
    FastMCP's OpenAPI integration doesn't coerce types when making HTTP requests,
    so we need to do it here.

    Args:
        data: Input data (dict, list, or primitive)

    Returns:
        Data with coerced types
    """
    if isinstance(data, dict):
        return _coerce_dict(data)
    if isinstance(data, list):
        return _coerce_list(data)
    if isinstance(data, str):
        return _coerce_string(data)
    return data


def _coerce_json_arg(value: Any) -> Any:
    """Parse a JSON object/array string argument into its Python equivalent.

    The Docker MCP CLI passes object/array parameters as strings (e.g.
    ``'{"key": true}'``). This helper transparently converts those strings so
    the grouped tools can accept either a JSON string or the native Python type.
    Primitive strings (entry IDs, domains, etc.) are left unchanged to avoid
    silently coercing values like ``"true"`` or ``"123"`` into non-string types.
    """
    import json

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
    return value
