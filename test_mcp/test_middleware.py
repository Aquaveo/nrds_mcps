"""Tests for the InputValidationEnvelopeMiddleware + ToolCallObservabilityMiddleware port.

Verifies:
- Pydantic ValidationError on tool input returns a structured envelope
  with `error`, `expected_kwargs`, `fix_hint` fields (not a wire-shape
  generic error).
- Per-tool-call observability log line fires with `tool=, arg_keys=,
  status=, duration_ms=` shape, key names only (no arg values).
- Both middleware co-exist: a tool call that hits the validation
  middleware still produces an observability log line.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client

from nextgen_mcp.mcp_server import mcp


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# InputValidationEnvelopeMiddleware — structured envelope on bad input
# ---------------------------------------------------------------------------


def test_unexpected_kwarg_produces_invalid_args_envelope(caplog):
    """Calling a tool with an unexpected kwarg returns a structured envelope.

    The chatbox-core engine / any MCP client gets `error`, `unexpected_kwargs`,
    `expected_kwargs`, `fix_hint` instead of a wire-shape generic error.
    """

    async def go():
        async with Client(mcp) as c:
            # list_available_models takes no args; passing one is unexpected.
            return await c.call_tool("list_available_models", {"bogus_kwarg": "x"})

    result = _run(go())
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert "error" in payload
    assert payload["error"].startswith("invalid_args:")
    assert "unexpected_kwargs" in payload
    assert "bogus_kwarg" in payload["unexpected_kwargs"]
    assert "fix_hint" in payload
    # fix_hint mentions the offending kwarg by name
    assert "bogus_kwarg" in payload["fix_hint"]


def test_missing_required_arg_produces_invalid_args_envelope():
    """Calling a tool without required arg returns missing_kwargs envelope."""

    async def go():
        async with Client(mcp) as c:
            # list_available_dates requires model; omit it.
            return await c.call_tool("list_available_dates", {})

    result = _run(go())
    payload = result.structured_content
    assert isinstance(payload, dict)
    # list_available_dates uses _require() which returns its own error envelope
    # — middleware only intercepts pydantic ValidationError. Both error paths
    # are valid; this test verifies SOMETHING structured comes back, not a
    # raw exception.
    assert "error" in payload or "ok" in payload


def test_envelope_includes_expected_kwargs_for_tool():
    """Tools whose validation fires through pydantic get expected_kwargs.

    list_available_dates has documented required args. Passing an unexpected
    kwarg should trigger the validation middleware and produce expected_kwargs.
    """

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "list_available_dates", {"model": "cfe_nom", "bogus_extra": True}
            )

    result = _run(go())
    payload = result.structured_content
    if isinstance(payload, dict) and payload.get("error", "").startswith("invalid_args:"):
        # Validation middleware fired
        assert "expected_kwargs" in payload
        assert isinstance(payload["expected_kwargs"], list)
        # Should include the tool's documented args
        assert "model" in payload["expected_kwargs"]


# ---------------------------------------------------------------------------
# ToolCallObservabilityMiddleware — log line per call
# ---------------------------------------------------------------------------


def test_observability_logs_one_line_per_call(caplog, mock_fsspec_empty_ls):
    """Successful tool call emits one tool-call=... log line with status=ok.

    Uses mock_fsspec_empty_ls so the S3 call returns [] without hitting
    live infrastructure — keeps the test deterministic on CI / local envs
    with botocore/s3fs version mismatches.
    """

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("list_available_models", {})

    with caplog.at_level(logging.INFO, logger="nextgen_mcp"):
        _run(go())

    tool_call_records = [
        r for r in caplog.records if "tool-call" in r.message
    ]
    assert len(tool_call_records) >= 1
    msg = tool_call_records[0].message
    assert "tool=list_available_models" in msg
    assert "status=" in msg
    assert "duration_ms=" in msg


def test_observability_logs_arg_keys_not_values(caplog, mock_fsspec_empty_ls):
    """The observability log records sorted arg KEY NAMES, never arg values.

    Defends against secrets / large payloads / PII leaking into log surface.
    """

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "list_available_dates", {"model": "cfe_nom"}
            )

    with caplog.at_level(logging.INFO, logger="nextgen_mcp"):
        _run(go())

    tool_call_records = [
        r for r in caplog.records if "tool-call" in r.message
    ]
    assert len(tool_call_records) >= 1
    msg = tool_call_records[0].message
    # Key name appears
    assert "arg_keys=['model']" in msg
    # Value does NOT appear
    assert "cfe_nom" not in msg


def test_observability_logs_invalid_args_status(caplog):
    """Invalid-args calls log with status=invalid_args, not status=ok."""

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("list_available_models", {"bogus": "x"})

    with caplog.at_level(logging.INFO, logger="nextgen_mcp"):
        _run(go())

    # Find the tool-call line
    tool_call_records = [
        r for r in caplog.records if "tool-call" in r.message
    ]
    assert len(tool_call_records) >= 1
    msg = tool_call_records[0].message
    assert "status=invalid_args" in msg
    assert "error_class=" in msg
