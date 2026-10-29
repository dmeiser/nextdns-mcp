# ---
# Type coercion helpers for MCP tool arguments.
#
# CLI tools and some MCP clients pass values as strings (e.g. "true" instead of true). The
# helpers here coerce those strings to proper Python types. JSON request
# bodies are NOT coerced: the HTTP client passes string body values through
# unchanged (see AccessControlledClient.request), and schema-aware coercion
# of tool arguments already happens in StripExtraFieldsMiddleware.
"""Type coercion utilities for NextDNS MCP Server.

SPDX-License-Identifier: MIT
"""

from typing import Annotated, Any

# Pydantic import for BeforeValidator
try:
    from pydantic import BeforeValidator
except ImportError:  # pragma: no cover
    BeforeValidator = None  # type: ignore


# Profile IDs from CLI or MCP clients may arrive as integers when an accepted ID
# shape (see SAFE_PROFILE_ID_PATTERN in client.py) happens to contain only decimal
# digits (e.g., "315244"). Use BeforeValidator to coerce int inputs to str while
# preserving None for the default-profile fallback.
def _coerce_profile_id(v: object) -> object:
    """Coerce non-None profile_id values to str; leave None as-is."""
    return str(v) if v is not None else v


_coerce_to_str = BeforeValidator(_coerce_profile_id) if BeforeValidator is not None else lambda x: x
OptionalProfileId = Annotated[str | None, _coerce_to_str]
ProfileId = Annotated[str, _coerce_to_str]


def before_validator(coerce: Any) -> Any:
    """Wrap an argument coercion for use in an Annotated tool parameter.

    Returns the plain callable when pydantic is unavailable, so a tool module
    can declare its tolerances without caring how the schema layer is wired.
    """
    return BeforeValidator(coerce) if BeforeValidator is not None else coerce


def operation_aliases(valid: tuple[str, ...], aliases: dict[str, str]) -> Any:
    """Accept unambiguous synonyms for a tool's ``operation`` parameter.

    Callers reach for the verb that names the intent rather than the one the
    schema happens to use: across three live runs the model asked
    ``manageLists`` for ``list`` (7 calls, where the operation is ``get``) and
    ``manageProfiles`` for ``rename``, ``set``, ``replace`` and ``add``. Each of
    those has exactly one meaning for the tool in question, so it is resolved.

    Only put a verb in the table when it names an operation *of this tool* and
    being wrong is recoverable. ``remove`` was dropped from ``manageProfiles``
    after a live run showed a caller doing "remove X from the denylist" sending
    ``manageProfiles(operation="remove", profile_id=…)``: the alias turned that
    wrong-tool call into a deleted profile.

    Anything else - an unknown verb, or one that is not in the alias table - is
    returned untouched so the schema error still lists the valid operations.
    The published enum is never widened.
    """

    def coerce(value: Any) -> Any:
        if not isinstance(value, str) or value in valid:
            return value
        target = aliases.get(value.strip().lower())
        return target if target in valid else value

    return coerce


def _coerce_json_arg(value: Any) -> Any:
    """Parse a JSON object/array string argument into its Python equivalent.

    CLI clients may pass object/array parameters as strings (e.g.
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
