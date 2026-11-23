"""Cross-layer constants shared by the client, transport and observability layers.

SPDX-License-Identifier: MIT

These live here so the direction of dependency is a straight line: the access
control layer (``client``), the API transport (``transport``), the call log
(``call_log``) and the run-options module (``run_options``) all read these
definitions, and none of them has to import another to say what a profile id
looks like, which HTTP methods change state, which tool names belong to the
disclosure transform, or which hosts count as loopback. A shared home breaks
the old ``utils`` -> ``client`` edge and keeps the synthetic-tool name set a
single definition shared by the log and the transform. The DoH answer
vocabulary (``VALID_DNS_RECORD_TYPES``, ``DNS_STATUS_CODES``) lives here for the
same reason: ``config`` publishes those names to its callers and must never
reach up into the tool layer to spell them, and this module imports nothing from
the package, so the edge cannot close a cycle.
"""

import re

# The loopback host names. The client allows them as request destinations so
# local test doubles and loopback mock servers stay usable (no shipped tool
# ever targets them), and the run-options check reads the same set to decide
# whether an ``MCP_HOST`` bind is loopback-only. Membership is exact: ``127.0.0.2``
# or ``evil.localhost`` is not in here and is not treated as loopback.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

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

# The answer vocabulary of the DoH lookup tool: the record types it accepts and
# the rcode descriptions it reports back. These are data the tool formats, not
# a policy it enforces, and ``config`` re-exports both names to its callers, so
# the definition sits below both layers instead of in the tool that reads them.
VALID_DNS_RECORD_TYPES = [
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "NS",
    "PTR",
    "SOA",
    "TXT",
    "SRV",
    "CAA",
    "DNSKEY",
    "DS",
]

DNS_STATUS_CODES = {
    0: "NOERROR - Success",
    1: "FORMERR - Format error",
    2: "SERVFAIL - Server failure",
    3: "NXDOMAIN - Non-existent domain",
    4: "NOTIMP - Not implemented",
    5: "REFUSED - Query refused",
}


def is_write_operation(method: str) -> bool:
    """True when a request with this method changes server state."""
    return method.upper() in WRITE_METHODS
