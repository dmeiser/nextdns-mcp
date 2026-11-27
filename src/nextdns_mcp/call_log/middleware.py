"""The FastMCP hook: one row per executed call, never a failed call.

SPDX-License-Identifier: MIT

The only module in the package that knows FastMCP exists. It skips the
synthetic ``search_tools`` / ``call_tool`` so one disclosed call is not
double-counted, keeps the call safe (a recording fault is a warning), and
delegates row construction to ``record.build_row`` and the write to ``store``.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from ..constants import SYNTHETIC_TOOL_NAMES
from .record import build_row, result_error
from .store import _append_row, call_log_path

logger = logging.getLogger(__name__)


class CallLogMiddleware(Middleware):
    """Append one record per tool call this server executes.

    ``endpoints`` maps a generated tool name to the ``(method, path template)``
    it executes; the server builds it once from the parsed routes. It is
    middleware state, not a module registry, so importing this module never
    depends on which other modules have been loaded or in what order.
    """

    def __init__(
        self,
        path: str | None = None,
        endpoints: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        self._path = call_log_path() if path is None else path
        self._endpoints: dict[str, tuple[str, str]] = {
            name: (method.upper(), template) for name, (method, template) in (endpoints or {}).items()
        }

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = str(getattr(context.message, "name", ""))
        if name in SYNTHETIC_TOOL_NAMES:
            return await call_next(context)
        args = getattr(context.message, "arguments", None) or {}
        try:
            result = await call_next(context)
        except Exception as exc:  # schema validation, unknown tool, transport faults
            await self._record_failure(name, args, exc)
            raise
        error = result_error(result)
        try:
            await self._record(name, args, ok=not error, error=error, result=result)
        except Exception as exc:  # noqa: BLE001 - logging is optional and must never fail the call
            logger.warning("call log recording failed: %s", exc)
        return result

    async def _record_failure(self, name: str, args: Any, exc: Exception) -> None:
        """Record a failed call; a recording fault never masks the original error.

        The raise of the call's own exception happens in the caller, after this
        returns: a defect in row construction surfaces as a warning, not as a
        replacement for the error the caller must see.
        """
        try:
            await self._record(name, args, ok=False, error=f"{type(exc).__name__}: {exc}")
        except Exception as record_exc:  # noqa: BLE001 - logging is optional and must never fail the call
            logger.warning("call log recording failed: %s", record_exc)

    def _record_row(self, tool: str, args: Any, ok: bool, error: str, result: Any) -> dict[str, Any]:
        """Bind this middleware's endpoint map to ``record.build_row`` (row construction lives there)."""
        return build_row(tool, args, ok, error, result, self._endpoints)

    def _record_row_into(self, path: str, tool: str, args: Any, ok: bool, error: str, result: Any) -> None:
        """Build the row and append it; both off the event loop (``_record`` offloads here)."""
        _append_row(path, self._record_row(tool, args, ok, error, result))

    async def _record(self, tool: str, args: Any, ok: bool, error: str, result: Any = None) -> None:
        """Append one row, off the event loop; a failed write never fails the call.

        The whole row - projection and JSON of the arguments and the result,
        not just the disk write - is built on the worker thread: projecting a
        2,000-point series or a 10,000-entry allowlist is the expensive part,
        and doing it inline made the log slow the call it merely observes.
        """
        if not self._path:
            return
        await asyncio.to_thread(self._record_row_into, self._path, tool, args, ok, error, result)
