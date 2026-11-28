"""Task targets for the NextDNS MCP E2E harness.

SPDX-License-Identifier: MIT

:class:`TaskTargets` names the fixed, verifiable outcomes the task asks the
actor to reach. Every target is something the measuring half re-verifies
through the raw REST API after the actor exits, independent of the MCP path.
Split out of ``ai_e2e_harness.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts import ai_e2e_common as common
else:
    import ai_e2e_common as common


@dataclass
class TaskTargets:
    """The fixed, verifiable outcomes the task asks the actor to reach.

    Every target is something the measuring half can re-verify through the raw
    REST API after the actor exits, independent of the MCP path.
    """

    profile_name: str
    tag: str

    @property
    def renamed_name(self) -> str:
        return f"{self.profile_name}-renamed"

    @property
    def list_entries(self) -> dict[str, str]:
        """The entry the actor must leave present in each list type.

        Derived from ``common.LIST_ENTRIES`` — the same table the REST
        read-back resolves — so the prompt the actor is given and the check
        that judges it cannot name different entries after either is edited.
        """
        return {name: template.format(tag=self.tag) for name, template in common.LIST_ENTRIES.items()}

    @property
    def rewrite_name(self) -> str:
        return f"ai-e2e-a-{self.tag}.example.com"

    @property
    def rewrite_content(self) -> str:
        return "192.0.2.100"
