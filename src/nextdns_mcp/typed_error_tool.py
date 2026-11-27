"""Function tools that keep the typed JSON error contract on argument refusals.

FastMCP validates a function tool's arguments with a pydantic model built from
its signature before the tool body runs. An argument failure therefore escapes
as a bare ``ValidationError`` text result with ``structured_content=None``,
and the middleware that annotates tool results never sees a result to annotate.
This subclass converts exactly that failure class — body-raised pydantic
errors are re-tagged by FastMCP as ``_ToolBodyError`` and pass through — into
the flagged typed ``invalid_argument`` payload callers branch on. Validation
runs through the signature's own model, so declared coercions and the published
JSON schema (enums included) stay exactly as they were.

SPDX-License-Identifier: MIT
"""

from typing import Any

from fastmcp.exceptions import ValidationError
from fastmcp.tools.base import ToolResult
from fastmcp.tools.function_tool import FunctionTool

from .errors import ErrorCode, error_payload


class TypedArgumentTool(FunctionTool):
    """A function tool whose argument validation refusals answer the typed contract."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Run as usual, answering argument-validation failures with the typed payload."""
        try:
            return await super().run(arguments)
        except ValidationError as exc:
            return ToolResult(
                structured_content=error_payload(ErrorCode.INVALID_ARGUMENT, str(exc)),
                is_error=True,
            )
