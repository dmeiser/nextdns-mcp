"""Which tool surface this server process publishes.

SPDX-License-Identifier: MIT

Two surfaces exist:

- ``grouped`` (the default): eight area tools, each taking an ``operation``
  argument that selects the API call (``manageLists(operation="add")``).
- ``atomic``: one tool per NextDNS API operation, named by the spec's own
  ``operationId`` (``addToDenylist``), taking that endpoint's own arguments.

The choice is process-wide because everything that describes the catalog to a
caller has to agree with it: the ``initialize`` instructions, the hints in error
payloads, and the registry the call log reads the bound endpoint from. A caller
told "``manageProfiles(operation="list")`` returns the profile ids" on a surface
that has no such tool is being lied to by the server, so those messages are
written from this module rather than from the tool modules.
"""

import os

from .config import ConfigurationError

TOOL_SURFACE_ENV = "NEXTDNS_TOOL_SURFACE"

GROUPED = "grouped"
ATOMIC = "atomic"

_SURFACES = (GROUPED, ATOMIC)


def validate_tool_surface() -> str:
    """The configured surface, rejecting a value that is not one of the two.

    Called where the server is built, for the same reason ``NEXTDNS_TOOL_SEARCH``
    rejects an unknown mode: guessing here would mean serving a different tool
    contract from the one the operator asked for, quietly.
    """
    value = os.environ.get(TOOL_SURFACE_ENV, "").strip().lower()
    if not value:
        return GROUPED
    if value not in _SURFACES:
        raise ConfigurationError(f"Invalid {TOOL_SURFACE_ENV}: {value!r}. Expected one of: {', '.join(_SURFACES)}.")
    return value


def get_tool_surface() -> str:
    """The configured surface, ``grouped`` unless the variable says ``atomic``.

    Deliberately more forgiving than ``validate_tool_surface``, because this is
    the accessor the guidance path uses — an error payload's hint is built while
    answering a call, and failing to name the right tool there would replace a
    useful 404 with an exception. Startup validation is what rejects a typo.
    """
    value = os.environ.get(TOOL_SURFACE_ENV, "").strip().lower()
    return value if value in _SURFACES else GROUPED


def list_profiles_call(surface: str | None = None) -> str:
    """The call this server's own messages name for finding a profile id.

    Used wherever an error payload points a caller at profile discovery, so the
    pointer is a call the caller can actually make on this surface.
    """
    current = get_tool_surface() if surface is None else surface
    return "listProfiles()" if current == ATOMIC else 'manageProfiles(operation="list")'
