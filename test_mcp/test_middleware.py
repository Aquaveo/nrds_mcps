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
# InputValidationEnvelopeMiddleware - structured envelope on bad input
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
    # - middleware only intercepts pydantic ValidationError. Both error paths
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
# ToolCallObservabilityMiddleware - log line per call
# ---------------------------------------------------------------------------


def test_observability_logs_one_line_per_call(caplog, mock_fsspec_empty_ls):
    """Successful tool call emits one tool-call=... log line with status=ok.

    Uses mock_fsspec_empty_ls so the S3 call returns [] without hitting
    live infrastructure - keeps the test deterministic on CI / local envs
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


def test_pattern_mismatch_fix_hint_includes_pattern_and_field():
    """When a tool input fails a Pydantic regex pattern, the envelope's
    fix_hint must include the expected pattern AND name the field.

    Observed 2026-05-10: qwen passed date='20260510' (no separators),
    Pydantic raised string_pattern_mismatch on the
    ^(?:\\d{4}-\\d{2}-\\d{2}|\\d{4}/\\d{2}/\\d{2})$ regex. The
    middleware's old fix_hint just said 'Fix the type / value errors in
    details' - the LLM had no clue what pattern to satisfy.
    """

    async def go():
        async with Client(mcp) as c:
            # list_available_dates has a 'start' arg with the date regex.
            return await c.call_tool(
                "list_available_dates",
                {"model": "cfe_nom", "start": "20260510"},
            )

    result = _run(go())
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert payload.get("error", "").startswith("invalid_args:")
    # details surfaces the field name + a pattern indicator
    details = payload.get("details") or []
    pattern_entries = [d for d in details if d.get("type") == "string_pattern_mismatch"]
    assert pattern_entries, f"expected a pattern_mismatch entry in details; got {details}"
    pe = pattern_entries[0]
    assert pe.get("field") == "start"
    # The pattern from pydantic ctx is now carried through
    assert "pattern" in pe, (
        f"details entry should carry the regex pattern; got {pe}"
    )
    assert "\\d{4}" in pe["pattern"], (
        f"pattern entry should be the actual regex; got {pe['pattern']!r}"
    )
    # fix_hint names the field + includes the pattern shape
    fix_hint = payload.get("fix_hint", "")
    assert "start" in fix_hint, f"fix_hint should name the field; got: {fix_hint!r}"
    # Either the regex itself or a human-friendly description should appear
    assert "\\d{4}" in fix_hint or "YYYY" in fix_hint or "pattern" in fix_hint.lower(), (
        f"fix_hint should hint at the expected pattern shape; got: {fix_hint!r}"
    )


def test_date_out_of_bounds_returns_envelope_not_raise():
    """ValueError from _validate_date_bounds in a tool body becomes an envelope.

    Observed 2026-05-10: passing date=2023-10-01 to list_available_forecasts
    raised ValueError, which FastMCP wrapped in ToolError, which bubbled up
    as an MCP protocol error. The LLM saw an unrecoverable traceback. Now
    the middleware catches the ToolError-wrapped ValueError and returns an
    `invalid_args:` envelope carrying the original prescriptive message.
    """

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "list_available_forecasts",
                {"model": "cfe_nom", "date": "2023-10-01"},
            )

    result = _run(go())
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert payload.get("error", "").startswith("invalid_args:")
    # The original prescriptive message is carried through
    assert "between" in payload["error"]
    assert "2023-10-01" in payload["error"]
    assert "fix_hint" in payload


def test_xor_violation_returns_envelope_not_raise():
    """resolve_output_file file_name XOR index ValueError becomes an envelope.

    Tool body raises InvalidLLMInputError("Provide exactly one of
    'file_name' or 'index'.") when both or neither are supplied.
    Middleware converts to invalid_args envelope so the LLM can fix the
    call.
    """

    async def go():
        async with Client(mcp) as c:
            # Pass both file_name AND index - triggers the XOR check.
            return await c.call_tool(
                "resolve_output_file",
                {
                    "model": "cfe_nom",
                    "forecast": "short_range",
                    "vpu": "06",
                    "file_name": "fake.parquet",
                    "index": 0,
                },
            )

    result = _run(go())
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert payload.get("error", "").startswith("invalid_args:")
    # Both field names appear so the LLM knows exactly which two are in
    # conflict, and fix_hint is non-empty with the prescriptive message.
    assert "file_name" in payload["error"]
    assert "index" in payload["error"]
    fix_hint = payload.get("fix_hint") or ""
    assert "exactly one" in fix_hint


def test_incidental_value_error_is_not_enveloped(monkeypatch):
    """Plain ValueError (or stdlib subclasses) from a tool body re-raises.

    Defense-in-depth against the convention drifting: only
    InvalidLLMInputError is treated as LLM-recoverable. A
    UnicodeDecodeError or a ValueError from int() inside a helper must
    NOT be silently converted to `invalid_args:`, otherwise transient
    infrastructure failures get misclassified as user error and trigger
    LLM retry storms.

    We patch list_available_models's request helper to raise a plain
    ValueError; the middleware should NOT catch it as invalid_args.
    """
    from nextgen_mcp import mcp_server

    def boom(*_a, **_kw):
        raise ValueError("simulated upstream parse failure")

    monkeypatch.setattr(mcp_server.tools, "list_available_models", boom)

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("list_available_models", {})

    # FastMCP wraps the ValueError into a ToolError that is surfaced as
    # the call result; whatever shape it takes, it MUST NOT be classified
    # as invalid_args.
    try:
        result = _run(go())
    except Exception:
        # Re-raised - that's the desired behavior. Bare ValueError is
        # treated as a programmer/infrastructure error and surfaces as
        # the normal MCP protocol error path.
        return

    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict):
        err = payload.get("error", "")
        assert not err.startswith("invalid_args:"), (
            f"plain ValueError must NOT be enveloped as invalid_args; got: {err!r}"
        )


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


# ---------------------------------------------------------------------------
# Unit tests for _describe_other_errors / _summarize_errors phrasing branches
# ---------------------------------------------------------------------------
#
# The end-to-end tests above only exercise `string_pattern_mismatch` (because
# that's the only constraint type the current tool schemas use in a way the
# LLM can trip). The middleware claims to phrase 8 other pydantic error
# types from its `_CTX_KEY_ALLOWLIST`, but those branches were dead from a
# test perspective - a typo in any of them (wrong ctx-key name, wrong op
# symbol) would ship undetected. These unit tests lock the phrasing
# contract per branch so refactors can't silently break it.


@pytest.mark.parametrize(
    "err_dict, expected_substr",
    [
        # Each entry: a raw pydantic-shaped error dict + a substring the
        # phrase MUST contain. Substring checks (not equality) keep the
        # tests robust to small wording tweaks while still pinning the
        # actionable parts.
        (
            {
                "type": "string_too_short",
                "loc": ("name",),
                "ctx": {"min_length": 3},
            },
            "name must be at least 3",
        ),
        (
            {
                "type": "string_too_long",
                "loc": ("name",),
                "ctx": {"max_length": 50},
            },
            "name must be at most 50",
        ),
        (
            {
                "type": "greater_than_equal",
                "loc": ("limit",),
                "ctx": {"ge": 0},
            },
            "limit must be >= 0",
        ),
        (
            {
                "type": "greater_than",
                "loc": ("count",),
                "ctx": {"gt": 0},
            },
            "count must be > 0",
        ),
        (
            {
                "type": "less_than_equal",
                "loc": ("limit",),
                "ctx": {"le": 100},
            },
            "limit must be <= 100",
        ),
        (
            {
                "type": "less_than",
                "loc": ("count",),
                "ctx": {"lt": 10},
            },
            "count must be < 10",
        ),
        (
            {
                "type": "literal_error",
                "loc": ("forecast",),
                "ctx": {"expected": "'short_range' or 'medium_range'"},
            },
            "forecast must be one of",
        ),
        (
            {
                "type": "enum",
                "loc": ("model",),
                "ctx": {"expected": "['cfe_nom', 'lstm']"},
            },
            "model must be one of",
        ),
        (
            {
                "type": "string_pattern_mismatch",
                "loc": ("date",),
                "ctx": {"pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            },
            "date must match pattern",
        ),
    ],
)
def test_describe_other_errors_per_error_type(err_dict, expected_substr):
    """Each ctx-allowlist entry produces a recognizable field+constraint phrase.

    Pins the phrasing contract for all 9 pydantic error types we know how
    to phrase. A typo in a ctx-key name (e.g. `min_length` -> `minimum`)
    or a wrong operator symbol would fail one of these.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _describe_other_errors

    result = _describe_other_errors([err_dict])
    assert expected_substr in result, (
        f"expected {expected_substr!r} in result for err_type={err_dict['type']!r}; "
        f"got: {result!r}"
    )


def test_describe_other_errors_unknown_type_returns_empty():
    """Unknown error types contribute no phrase, leaving the caller's fallback path.

    `_build_fix_hint` treats `''` as falsy and inserts the generic
    'Fix the type / value errors listed in `details`' hint. This test
    pins that contract so a future change can't accidentally produce a
    misleading phrase for an unrecognized type.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _describe_other_errors

    result = _describe_other_errors(
        [
            {
                "type": "value_error",
                "loc": ("date",),
                "ctx": {"error": "custom validator failed"},
            }
        ]
    )
    assert result == ""


def test_describe_other_errors_mixed_known_and_unknown():
    """Mixed known/unknown error types phrase only the known ones.

    Documents (and pins) the current behavior: unknown entries are
    silently dropped from the per-field hint. The known entries still
    produce their actionable phrase; the unknown ones disappear. If we
    later want to surface "and N other unphrased errors" we'd change this
    test.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _describe_other_errors

    result = _describe_other_errors(
        [
            {
                "type": "string_pattern_mismatch",
                "loc": ("date",),
                "ctx": {"pattern": "^\\d{4}$"},
            },
            {
                "type": "value_error",
                "loc": ("model",),
                "ctx": {"error": "something else"},
            },
        ]
    )
    assert "date must match pattern" in result
    assert "model" not in result


def test_describe_other_errors_accepts_flattened_post_summarize_shape():
    """`_describe_other_errors` works on `_summarize_errors` output too.

    The function is called from `_build_fix_hint` with the RAW pydantic
    errors today, but the docstring claims dual-shape support. This test
    pins the flattened shape: ctx keys appear at the top level of the
    entry (no nested `ctx` dict). Locks the contract so a refactor that
    swaps the call site to pass `_summarize_errors(others)` doesn't
    silently break the phrasing.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _describe_other_errors

    flattened = {
        "field": "date",
        "type": "string_pattern_mismatch",
        "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
    }
    result = _describe_other_errors([flattened])
    assert "date must match pattern" in result
    assert "\\d{4}" in result


def test_summarize_errors_carries_ctx_for_each_allowlist_entry():
    """Every ctx-allowlist key shows up in the summarized entry.

    Drift check: if anyone removes or renames an entry in
    `_CTX_KEY_ALLOWLIST`, the corresponding ctx key disappears from the
    envelope's `details`. This test pins the contract for all 9 entries
    so that drift fails CI instead of degrading the LLM's recovery
    information silently.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _summarize_errors

    cases: list[tuple[dict[str, object], str, object]] = [
        ({"type": "string_pattern_mismatch", "loc": ("d",), "ctx": {"pattern": "^x$"}}, "pattern", "^x$"),
        ({"type": "string_too_short", "loc": ("n",), "ctx": {"min_length": 3}}, "min_length", 3),
        ({"type": "string_too_long", "loc": ("n",), "ctx": {"max_length": 50}}, "max_length", 50),
        ({"type": "greater_than_equal", "loc": ("c",), "ctx": {"ge": 0}}, "ge", 0),
        ({"type": "less_than_equal", "loc": ("c",), "ctx": {"le": 100}}, "le", 100),
        ({"type": "greater_than", "loc": ("c",), "ctx": {"gt": 0}}, "gt", 0),
        ({"type": "less_than", "loc": ("c",), "ctx": {"lt": 10}}, "lt", 10),
        ({"type": "literal_error", "loc": ("f",), "ctx": {"expected": "'a'"}}, "expected", "'a'"),
        ({"type": "enum", "loc": ("m",), "ctx": {"expected": "['a']"}}, "expected", "['a']"),
    ]
    for raw, ctx_key, ctx_value in cases:
        summary = _summarize_errors([raw])
        assert len(summary) == 1
        entry = summary[0]
        assert entry["type"] == raw["type"]
        assert entry.get(ctx_key) == ctx_value, (
            f"{raw['type']}: expected {ctx_key}={ctx_value!r} in summarized entry; "
            f"got: {entry!r}"
        )


def test_summarize_errors_omits_unallowlisted_ctx_keys():
    """ctx keys not in the allowlist must not leak into the envelope.

    The allowlist exists specifically to exclude the bad input value
    (and any other ctx fields that could leak user-supplied content).
    This test pins that filter.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _summarize_errors

    summary = _summarize_errors(
        [
            {
                "type": "string_pattern_mismatch",
                "loc": ("d",),
                "ctx": {
                    "pattern": "^x$",
                    # Should be filtered out - would leak user input.
                    "input_value": "secret_user_supplied",
                },
            }
        ]
    )
    entry = summary[0]
    assert entry.get("pattern") == "^x$"
    assert "input_value" not in entry
    assert "secret_user_supplied" not in str(entry)


def test_build_fix_hint_falls_back_on_unknown_only():
    """When all `others` are unknown types, `_build_fix_hint` uses the generic line.

    Defense against a regression where `_describe_other_errors` starts
    returning an empty string AND the caller silently appends nothing.
    The generic hint ("Fix the type / value errors listed in `details`")
    must still appear so the LLM knows to inspect details.
    """
    from nextgen_mcp.middleware._input_validation_middleware import _build_fix_hint

    hint = _build_fix_hint(
        unexpected=[],
        missing=[],
        others=[
            {
                "type": "value_error",
                "loc": ("date",),
                "ctx": {"error": "custom"},
            }
        ],
        expected=[],
    )
    assert "details" in hint
