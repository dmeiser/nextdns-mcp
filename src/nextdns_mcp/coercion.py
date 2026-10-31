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

from typing import Annotated

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
