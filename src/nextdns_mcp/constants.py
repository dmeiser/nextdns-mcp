"""Cross-layer constants shared by the client, transport and observability layers.

SPDX-License-Identifier: MIT

These live here so the direction of dependency is a straight line: the access
control layer (``client``), the API transport (``transport``) and the call log
(``call_log``) all read these definitions, and none of them has to import
another to say what a profile id looks like, which HTTP methods change state,
or which tool names belong to the disclosure transform. A shared home breaks
the old ``utils`` -> ``client`` edge and keeps the synthetic-tool name set a
single definition shared by the log and the transform.
"""

import re

# Safe identifier patterns to prevent path traversal and ACL bypass.
# Profile IDs must match what api.nextdns.io actually issues, not the stale
# nextdns-openapi.yaml ProfileId parameter (which once claimed 6 characters).
# The live API returns 10-character IDs for newly created profiles (observed
# 2026-10-03 in CI run 37139687479) while legacy profiles keep 6-character IDs,
# so both shapes are accepted and everything else still fails fast locally
# instead of 404ing upstream.
SAFE_PROFILE_ID_PATTERN = re.compile(r"^(?:[a-z0-9]{6}|[a-z0-9]{10})$")

# The hint every profile-id failure repeats: the shape, and where a real id
# comes from. One string so the generated tools, the delegation guard and the
# 404 diagnosis can never word the same contract differently.
PROFILE_ID_HINT = "the profile id is 6 or 10 characters; listProfiles() returns the ids this API key can access"

# HTTP methods that change server state. A tool bound to one of these wrote,
# whatever it was named; the call log's ``write`` field is derived from this.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Synthetic entries of the progressive-disclosure transform. The shape-repair
# middleware answers ``call_tool`` itself and the call log records the real
# tool the wrapper dispatches to, so logging these names would double-count
# discovery. This is the one definition; the log and the transform read it.
SYNTHETIC_TOOL_NAMES = frozenset({"search_tools", "call_tool"})


def is_write_operation(method: str) -> bool:
    """Check if an HTTP method is a write operation.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE)

    Returns:
        True if it's a write operation, False otherwise
    """
    return method.upper() in WRITE_METHODS
