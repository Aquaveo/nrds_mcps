"""
Server-protocol middleware that converts pydantic ValidationError raised
during tool input validation into a structured tool-result envelope.

Without this middleware, a hallucinated kwarg on an NRDS tool call
causes pydantic's TypeAdapter to raise ``ValidationError`` inside
``Tool._run`` (FastMCP 3.2.x), which propagates out as an MCP-protocol
error and produces a server traceback that any MCP-compatible client
(chatbox-core, Claude Desktop, Cursor, Cline, etc.) cannot recover from
cleanly.

With this middleware in the FastMCP server's middleware stack, the same
call returns a structured envelope as a normal tool result with
``error``, ``expected_kwargs``, ``fix_hint``, and per-class details. The
LLM has enough information to retry the call correctly in one turn.

# FastMCP 3.2.x-specific. Re-validate on FastMCP upgrade.

Envelope quality:

  - All applicable error classes are reported in ONE envelope (no
    short-circuit on `unexpected_kwargs` that hides simultaneous
    missing args from the LLM).
  - `expected_kwargs` lists every property name from the tool's input
    schema, so the LLM has the correct kwarg list right there in the
    response without re-fetching `tools/list`.
  - `fix_hint` tailors a natural-language recovery instruction to the
    error class(es) that fired.

The envelope shape is additive - keys appear only when their bucket is
non-empty, so consumers asserting on the ``unexpected_kwargs``-only or
``details``-only shapes continue to pass for those single-class cases.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools.base import ToolResult
import mcp.types as mt


LOGGER = logging.getLogger("nextgen_mcp")


class InvalidLLMInputError(ValueError):
    """Marker exception: bad LLM/user-supplied input that can be retried.

    Tool bodies raise this for cases that are not caught by pydantic schema
    validation but ARE recoverable by re-calling with corrected arguments
    (date out of allowed bounds, start>end, file_name XOR index, etc.).

    The middleware catches this specifically and emits an ``invalid_args:``
    envelope. Plain ``ValueError`` (or its stdlib subclasses like
    ``UnicodeDecodeError`` / ``JSONDecodeError``) raised from inside a tool
    body is treated as a programmer / infrastructure error and re-raised
    unchanged - otherwise transient S3 / parse failures would be silently
    re-classified as LLM-input mistakes and trigger fruitless retry loops.
    """


# Pydantic error types whose `ctx` carries actionable info we want to
# carry through into the envelope. Each entry names which ctx keys are
# safe to include (excludes the bad input value). Shared by
# `_summarize_errors` (which decides what to copy from ctx into `details`)
# and `_describe_other_errors` (which phrases the constraints). Keeping
# them in one place avoids drift between the two surfaces.
_CTX_KEY_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "string_pattern_mismatch": ("pattern",),
    "string_too_short": ("min_length",),
    "string_too_long": ("max_length",),
    "greater_than_equal": ("ge",),
    "less_than_equal": ("le",),
    "greater_than": ("gt",),
    "less_than": ("lt",),
    "literal_error": ("expected",),
    "enum": ("expected",),
}


class InputValidationEnvelopeMiddleware(Middleware):
    """Catch pydantic ValidationError on tool input and emit a typed envelope.

    Aggregates every validation-error class in one envelope so the LLM can
    fix all hallucinated kwargs, missing required args, and value-shape
    issues on a single retry instead of one-class-at-a-time.

    The LLM-facing payload omits the tool name (the MCP protocol already
    associates a tool result with the call that produced it; including the
    name would also leak BM25-invisible tool names in some scenarios). The
    server-side log line is the only place the tool name appears.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ValidationError as exc:
            tool_name = getattr(context.message, "name", "<unknown>")
            unexpected_kwargs, missing_kwargs, other_errors = _classify_errors(exc)
            expected_kwargs = await _expected_kwargs_for_tool(context, tool_name)

            envelope: dict[str, Any] = {
                "error": _build_error_message(
                    unexpected_kwargs, missing_kwargs, other_errors
                ),
            }
            if unexpected_kwargs:
                envelope["unexpected_kwargs"] = unexpected_kwargs
            if missing_kwargs:
                envelope["missing_kwargs"] = missing_kwargs
            if other_errors:
                envelope["details"] = _summarize_errors(other_errors)
            if expected_kwargs:
                envelope["expected_kwargs"] = expected_kwargs
            envelope["fix_hint"] = _build_fix_hint(
                unexpected_kwargs, missing_kwargs, other_errors, expected_kwargs
            )

            LOGGER.warning(
                "tool input rejected: tool=%s unexpected=%s missing=%s other=%d",
                tool_name,
                unexpected_kwargs or "[]",
                missing_kwargs or "[]",
                len(other_errors),
            )

            return ToolResult(structured_content=envelope)
        except ToolError as exc:
            # FastMCP server.py:1263 wraps any tool-body Exception in a
            # ToolError before middleware sees it. Only InvalidLLMInputError
            # (an explicit sentinel ValueError subclass) is treated as
            # LLM-recoverable invalid input. Plain ValueError and its
            # stdlib subclasses (UnicodeDecodeError, JSONDecodeError) raised
            # incidentally from helpers like int(), datetime.fromisoformat,
            # or pandas parsers are NOT recoverable - they signal upstream
            # data corruption or programmer error, and re-raising preserves
            # observability and avoids fruitless LLM retry loops.
            cause = exc.__cause__
            if not isinstance(cause, InvalidLLMInputError):
                raise
            tool_name = getattr(context.message, "name", "<unknown>")
            message = str(cause) or "tool input failed validation"
            envelope = {
                "error": f"invalid_args: {message}",
                "fix_hint": (
                    f"{message} Adjust the offending argument and retry."
                ),
            }
            LOGGER.warning(
                "tool input rejected (InvalidLLMInputError): tool=%s message=%s",
                tool_name,
                message,
            )
            return ToolResult(structured_content=envelope)


async def _expected_kwargs_for_tool(
    context: MiddlewareContext[Any], tool_name: str
) -> list[str]:
    """Return the sorted list of property names from the tool's input schema.

    Falls back to ``[]`` if the FastMCP context, registry lookup, or schema
    is unavailable / malformed - the envelope is still informative without
    `expected_kwargs`, just less helpful.
    """
    fastmcp_ctx = getattr(context, "fastmcp_context", None)
    if fastmcp_ctx is None:
        return []
    try:
        fastmcp = fastmcp_ctx.fastmcp
    except RuntimeError:
        # Context dereference race - server is shutting down or detached.
        return []
    try:
        tool = await fastmcp.get_tool(tool_name)
    except Exception:  # pragma: no cover - defensive
        return []
    if tool is None:
        return []
    params = getattr(tool, "parameters", None) or {}
    properties = params.get("properties") or {}
    return sorted(properties.keys())


def _classify_errors(
    exc: ValidationError,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Split pydantic errors into (unexpected_kwargs, missing_kwargs, other_errors).

    Pydantic error-type codes used:
      - `unexpected_keyword_argument` - kwarg not in signature
      - `missing_argument` - required parameter not provided
      - everything else (type errors, value-out-of-range, etc.) → other_errors
    """
    unexpected_kwargs: list[str] = []
    missing_kwargs: list[str] = []
    other_errors: list[dict[str, Any]] = []
    for err in exc.errors():
        err_type = err.get("type")
        loc = err.get("loc") or ()
        if err_type == "unexpected_keyword_argument":
            if loc:
                unexpected_kwargs.append(str(loc[-1]))
        elif err_type == "missing_argument":
            if loc:
                missing_kwargs.append(str(loc[-1]))
        else:
            other_errors.append(err)
    return unexpected_kwargs, missing_kwargs, other_errors


def _build_error_message(
    unexpected: list[str],
    missing: list[str],
    others: list[dict[str, Any]],
) -> str:
    """Concise top-level error string naming the firing classes.

    LLMs often pattern-match on the prefix; keeping ``invalid_args:`` and
    listing only the firing classes preserves both the existing
    contract and the improved single-envelope-multi-class clarity.
    """
    parts: list[str] = []
    if unexpected:
        parts.append("unexpected keyword arguments")
    if missing:
        parts.append("missing required arguments")
    if others:
        parts.append("argument validation failed")
    if not parts:
        return "invalid_args: tool input failed validation"
    return "invalid_args: " + ", ".join(parts)


def _build_fix_hint(
    unexpected: list[str],
    missing: list[str],
    others: list[dict[str, Any]],
    expected: list[str],
) -> str:
    """Tailored natural-language recovery instruction.

    Names the specific kwargs to drop / provide so the LLM doesn't have
    to re-derive them from the bucket lists. Lists `expected_kwargs`
    last as the canonical reference. For type-specific errors with
    actionable context (regex pattern, numeric bounds), names the
    field AND the constraint so the LLM has the info needed to satisfy
    the validator on retry.
    """
    bits: list[str] = []
    if unexpected:
        bits.append(f"Drop the unexpected kwargs: {unexpected}.")
    if missing:
        bits.append(f"Provide the missing required args: {missing}.")
    if others:
        # Build a per-field constraint description from the pydantic error
        # type + carried-through ctx. Falls back to a generic hint when
        # the type isn't one we know how to phrase.
        per_field_hints = _describe_other_errors(others)
        if per_field_hints:
            bits.append(per_field_hints)
        else:
            bits.append(
                "Fix the type / value errors listed in `details` "
                "(field + pydantic error type)."
            )
    if expected:
        bits.append(f"Valid kwargs for this tool: {expected}.")
    if not bits:
        return "Re-call this tool with arguments matching the tool's schema."
    return " ".join(bits)


def _describe_other_errors(others: list[dict[str, Any]]) -> str:
    """Build a per-field natural-language constraint description.

    Reads pydantic error dicts (post-`_summarize_errors`-equivalent shape,
    or raw - both supported) and produces a single phrase per field naming
    the field AND the constraint it failed. Returns "" when none of the
    error types have a known phrasing, so the caller can fall back to the
    generic hint.

    Each phrase is shaped to be directly actionable by an LLM on retry:
    "<field> must match pattern '<regex>'" beats "field <field> failed
    string_pattern_mismatch".
    """
    bits: list[str] = []
    for err in others:
        loc = err.get("loc") or ()
        field = err.get("field") or (
            ".".join(str(p) for p in loc) if loc else "<value>"
        )
        err_type = err.get("type")
        # Merge raw ctx (raw pydantic error path) with the entry's top-level
        # keys (post-_summarize_errors path). Top-level wins on collision -
        # that's the flattened shape's intended source of truth.
        merged = {**(err.get("ctx") or {}), **err}

        if err_type == "string_pattern_mismatch":
            pattern = merged.get("pattern")
            if pattern:
                bits.append(f"{field} must match pattern {pattern!r}.")
        elif err_type == "string_too_short":
            min_length = merged.get("min_length")
            if min_length is not None:
                bits.append(
                    f"{field} must be at least {min_length} character(s) long."
                )
        elif err_type == "string_too_long":
            max_length = merged.get("max_length")
            if max_length is not None:
                bits.append(
                    f"{field} must be at most {max_length} character(s) long."
                )
        elif err_type in ("greater_than_equal", "greater_than"):
            bound = (
                merged.get("ge") if err_type == "greater_than_equal"
                else merged.get("gt")
            )
            op = ">=" if err_type == "greater_than_equal" else ">"
            if bound is not None:
                bits.append(f"{field} must be {op} {bound}.")
        elif err_type in ("less_than_equal", "less_than"):
            bound = (
                merged.get("le") if err_type == "less_than_equal"
                else merged.get("lt")
            )
            op = "<=" if err_type == "less_than_equal" else "<"
            if bound is not None:
                bits.append(f"{field} must be {op} {bound}.")
        elif err_type in ("literal_error", "enum"):
            expected = merged.get("expected")
            if expected is not None:
                bits.append(f"{field} must be one of: {expected}.")

    return " ".join(bits)


def _summarize_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip pydantic error dicts to a stable, value-free summary.
    Names + types + actionable context (regex pattern, numeric bounds);
    no user-supplied values. Carries forward the parts of pydantic's
    ``ctx`` that tell the LLM what to satisfy (e.g. the actual regex
    pattern on string_pattern_mismatch) without leaking the bad input.
    """
    summary: list[dict[str, Any]] = []
    for err in errors:
        loc = err.get("loc") or ()
        err_type = err.get("type")
        entry: dict[str, Any] = {
            "field": ".".join(str(p) for p in loc) if loc else None,
            "type": err_type,
        }
        ctx = err.get("ctx") or {}
        allowed_keys = _CTX_KEY_ALLOWLIST.get(err_type or "", ())
        for key in allowed_keys:
            if key in ctx:
                entry[key] = ctx[key]
        summary.append(entry)
    return summary
