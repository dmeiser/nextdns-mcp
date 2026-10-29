"""Say what an argument failure means, in terms the caller can act on.

SPDX-License-Identifier: MIT

FastMCP validates tool arguments before the tool runs, and the failure reaches
the caller as pydantic's text::

    1 validation error for call[manageSettings]
    category
      Missing required argument [type=missing_argument, input_value=...]

That names no valid values and no meaning, so a caller that guessed wrong
guesses the same way again: observed live, ``manageSettings`` was called without
``category`` 13 times and ``dohLookup`` without ``domain`` 25 times, each
rejected identically.

This middleware leads with what the tool's own published schema already knows
about the parameter at fault - what it is for, and the values it accepts - and
keeps pydantic's text below it. Nothing is inferred about the caller's intent:
the hint is read out of the contract the server already advertises.
"""

import logging
from typing import Any

from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

# A hint that lists 40 valid values is noise; the schema error already does that.
_MAX_HINT_VALUES = 14


class ArgumentHintMiddleware(Middleware):
    """Append the schema's own explanation to an argument-validation failure."""

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        try:
            return await call_next(context)
        except FastMCPValidationError as exc:
            hint = await _hint_for(context, exc)
            if not hint:
                raise
            # The hint goes first: the call log bounds the recorded error, and a
            # caller reading the start of the message should read what to do.
            raise type(exc)(f"{hint}\n\n{exc}") from exc


async def _hint_for(context: MiddlewareContext, exc: Exception) -> str:
    """Build the hint for a validation failure, or '' when it cannot be done."""
    problems = _pydantic_errors(exc)
    if not problems:
        return ""

    name = str(getattr(getattr(context, "message", None), "name", "") or "")
    if not name:
        return ""

    fastmcp_context = getattr(context, "fastmcp_context", None)
    if fastmcp_context is None:  # pragma: no cover - a call always has a context
        return ""
    try:
        tool = await fastmcp_context.fastmcp.get_tool(name)
        parameters = (tool.parameters or {}) if tool is not None else {}
    except Exception:  # pragma: no cover - a hint must never break the error path
        logger.debug("could not fetch schema for %s to explain a validation error", name, exc_info=True)
        return ""

    properties = parameters.get("properties") or {}
    required = [str(item) for item in (parameters.get("required") or [])]

    lines: list[str] = []
    for problem in problems:
        param = _parameter_of(problem)
        if not param:
            continue
        schema = properties.get(param) or {}
        missing = problem.get("type") == "missing_argument"
        # An invalid value already comes back with the accepted ones listed, so
        # there the schema adds only what the message does not say: what the
        # parameter is for. A missing one comes back with neither.
        detail = _describe(schema, include_enum=missing)
        if detail:
            lines.append(f"{param} is required for {name}. {detail}" if missing else f"{param}: {detail}")
        if not missing:
            # The value may be perfectly valid - for a different tool. That is
            # the wrong-tool mistake this server actually makes: a caller doing
            # "remove X from the denylist" sends operation="remove" to
            # manageProfiles, where it is not an operation at all.
            note = await _other_tool_for(fastmcp_context, name, param, problem.get("input"))
            if note:
                lines.append(note)

    supplied = set(getattr(getattr(context, "message", None), "arguments", None) or {})
    if any(problem.get("type") == "missing_argument" for problem in problems) and supplied & set(required):
        lines.append(f"Required arguments for {name}: {', '.join(required)}.")

    # Deduplicated, order kept: the same parameter can fail twice.
    return "\n".join(dict.fromkeys(lines))


async def _other_tool_for(fastmcp_context: Any, name: str, param: str, value: Any) -> str:
    """Say so when the rejected value is a valid ``param`` for another tool.

    Read out of the published schemas, like every other hint here: no guess
    about what the caller meant, only which tool does accept that value.
    """
    if not isinstance(value, str) or not value:
        return ""
    try:
        tools = list(await fastmcp_context.fastmcp.list_tools())
    except Exception:  # pragma: no cover - a hint must never break the error path
        logger.debug("could not list tools to explain a rejected value for %s.%s", name, param, exc_info=True)
        return ""
    for tool in tools:
        if tool.name == name:
            continue
        schema = ((tool.parameters or {}).get("properties") or {}).get(param) or {}
        enum = schema.get("enum")
        if isinstance(enum, list) and value in enum:
            return f"'{value}' is a valid {param} for {tool.name}, not for {name}."
    return ""


def _pydantic_errors(exc: Exception) -> list[dict[str, Any]]:
    """The structured errors behind a FastMCP validation failure."""
    for candidate in (exc, exc.__cause__):
        errors = getattr(candidate, "errors", None)
        if not callable(errors):
            continue
        try:
            return [item for item in errors() if isinstance(item, dict)]
        except Exception as inner:  # a hint must never break the error path
            logger.debug("could not read the structured errors of a validation failure", exc_info=inner)
            continue
    return []


def _parameter_of(problem: dict[str, Any]) -> str:
    """The parameter a pydantic error refers to, from its location tuple."""
    loc = problem.get("loc") or ()
    for part in loc:
        if isinstance(part, str) and part:
            return part
    return ""


def _describe(schema: dict[str, Any], include_enum: bool) -> str:
    """What the published schema says about a parameter, as one sentence."""
    parts: list[str] = []
    enum = schema.get("enum")
    if include_enum and isinstance(enum, list) and enum:
        shown = [str(value) for value in enum[:_MAX_HINT_VALUES]]
        if len(enum) > _MAX_HINT_VALUES:
            shown.append("...")
        parts.append(f"It must be one of: {', '.join(shown)}.")
    description = str(schema.get("description") or "").strip()
    if description:
        parts.append(f"It is: {description}")
    return " ".join(parts)
