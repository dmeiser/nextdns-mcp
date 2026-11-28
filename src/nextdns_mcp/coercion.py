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

from pydantic import BeforeValidator


# Profile IDs from CLI or MCP clients may arrive as integers when an accepted ID
# shape (see SAFE_PROFILE_ID_PATTERN in client.py) happens to contain only decimal
# digits (e.g., "315244"). Use BeforeValidator to coerce int inputs to str while
# preserving None for the default-profile fallback. Only int is coerced: any other
# type (bool, list, dict, ...) passes through unchanged so the `str | None`
# validator rejects it instead of it becoming a bogus string like "True" or "[]".
def _coerce_profile_id(v: object) -> object:
    """Coerce int profile_id values to str; leave everything else as-is."""
    # bool is a subclass of int but is never a profile id; refuse to stringify it.
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return v


_coerce_to_str = BeforeValidator(_coerce_profile_id)
OptionalProfileId = Annotated[str | None, _coerce_to_str]
