"""Contract tests for NRDS @mcp.prompt slash-command templates.

Uses the in-process ``Client(mcp)`` pattern from FastMCP. The shipped
server module ``nextgen_mcp.mcp_server`` exposes the FastMCP instance
as ``mcp``; tests construct a Client against it without standing up a
real HTTP transport.

v1 ships a single prompt — ``plot_timeseries`` — driving the
timeseries-chart workflow against
``query_output_file_from_output_selector``. These tests lock the prompt
shape, the placeholder-default convention (K4), substitution semantics,
the argument-name parity contract with the underlying selector tool,
the intentionally narrative-only ``variable`` / ``feature_id`` args,
and the not-found error path.
"""

import asyncio

import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError

from nextgen_mcp.mcp_server import mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine to completion in a fresh event loop.

    Avoids the conftest-less fixture overhead while keeping each test
    independent.
    """
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _concat_text(messages) -> str:
    """Concatenate ``.text`` from text-typed message contents.

    Mirrors the chatbox-core insert handler's R7a behavior — only
    ``content.type == "text"`` participates; non-text content is
    silently dropped.
    """
    parts = []
    for m in messages:
        content = m.content
        ctype = getattr(content, "type", None)
        if ctype == "text":
            parts.append(getattr(content, "text", ""))
    return "".join(parts)


PLOT_TIMESERIES_ARG_NAMES = (
    "variable",
    "feature_id",
    "model",
    "forecast",
    "date",
    "cycle",
    "vpu",
    "index",
)

# Hint-bearing default values per arg. Each default is a `[bracketed]`
# string that doubles as a user-facing format hint — derived from the
# NRDS validation types (`MODELS`, `FORECASTS`, `DATE_PATTERN` in
# `nextgen_mcp/validations.py` / `nextgen_mcp/utils.py`) and the
# `query_output_file_from_output_selector` field descriptions. When NRDS
# adds a new model/forecast/vpu, update both the prompt default in
# `mcp_server.py` and this dict in lockstep.
PLOT_TIMESERIES_DEFAULTS = {
    "variable": "[flow/velocity/streamflow]",
    "feature_id": "[feature id, e.g., 1019290]",
    "model": "[cfe_nom/lstm/routing_only]",
    "forecast": "[short_range/medium_range/analysis_assim_extend]",
    "date": "[yyyy-mm-dd]",
    "cycle": "[00-23, e.g., 00]",
    "vpu": "[06, VPU_06, or 3W]",
    "index": "[0-based output index, e.g., 0]",
}

# Args shared with query_output_file_from_output_selector (lock parity).
OVERLAPPING_ARG_NAMES = ("model", "date", "forecast", "cycle", "vpu", "index")

# Args intentionally narrative-only — must NOT appear in the selector
# tool's schema. Locks the partial-alignment design.
NARRATIVE_ONLY_ARG_NAMES = ("variable", "feature_id")


# ---------------------------------------------------------------------------
# Shape tests
# ---------------------------------------------------------------------------


def test_list_prompts_returns_plot_timeseries():
    """``Client(mcp).list_prompts()`` returns at least one Prompt and
    ``plot_timeseries`` is among them with all 8 expected argument names.
    """
    async def go():
        async with Client(mcp) as c:
            return await c.list_prompts()

    prompts = _run(go())
    assert len(prompts) >= 1, "expected at least one prompt registered"

    by_name = {p.name: p for p in prompts}
    assert "plot_timeseries" in by_name, (
        f"plot_timeseries missing from prompts/list; got {sorted(by_name)}"
    )

    prompt = by_name["plot_timeseries"]
    arg_names = {a.name for a in (prompt.arguments or [])}
    assert arg_names == set(PLOT_TIMESERIES_ARG_NAMES), (
        f"plot_timeseries args mismatch — expected "
        f"{set(PLOT_TIMESERIES_ARG_NAMES)}, got {arg_names}"
    )


# ---------------------------------------------------------------------------
# Default-rendering and substitution
# ---------------------------------------------------------------------------


def test_get_prompt_with_no_args_renders_all_bracket_placeholders():
    """``client.get_prompt("plot_timeseries", {})`` returns ``messages``
    whose concatenated text contains every hint-bearing bracket default
    verbatim — all 8 brackets intact (K4 placeholder-default convention).
    Each default is a user-facing format hint derived from the NRDS
    validation types, not the bare arg name (e.g., ``[yyyy-mm-dd]``
    rather than ``[date]``).
    """
    async def go():
        async with Client(mcp) as c:
            return await c.get_prompt("plot_timeseries", {})

    result = _run(go())
    text = _concat_text(result.messages)

    for name, hint in PLOT_TIMESERIES_DEFAULTS.items():
        assert hint in text, (
            f"expected hint default {hint!r} for arg {name!r} verbatim "
            f"in rendered prompt; got: {text!r}"
        )


def test_get_prompt_substitutes_supplied_args_only():
    """Supplying ``{"variable": "flow", "feature_id": "1019290"}``
    substitutes those two args; the OTHER 6 hint-bearing brackets
    remain intact.
    """
    async def go():
        async with Client(mcp) as c:
            return await c.get_prompt(
                "plot_timeseries",
                {"variable": "flow", "feature_id": "1019290"},
            )

    result = _run(go())
    text = _concat_text(result.messages)

    # Substituted values present.
    assert "flow" in text, f"expected 'flow' in rendered prompt; got: {text!r}"
    assert "1019290" in text, (
        f"expected '1019290' in rendered prompt; got: {text!r}"
    )

    # The two substituted hint defaults are gone.
    for substituted in ("variable", "feature_id"):
        hint = PLOT_TIMESERIES_DEFAULTS[substituted]
        assert hint not in text, (
            f"hint {hint!r} for {substituted!r} should have been "
            f"substituted; got: {text!r}"
        )

    # The remaining 6 hint defaults survive.
    for name in ("model", "forecast", "date", "cycle", "vpu", "index"):
        hint = PLOT_TIMESERIES_DEFAULTS[name]
        assert hint in text, (
            f"expected unsubstituted hint {hint!r} for {name!r} to remain "
            f"in rendered prompt; got: {text!r}"
        )


# ---------------------------------------------------------------------------
# Argument-name parity with query_output_file_from_output_selector
# ---------------------------------------------------------------------------


def _selector_tool_schema():
    """Resolve the selector tool's input-schema property names."""

    async def go():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = _run(go())
    selector = next(
        (t for t in tools if t.name == "query_output_file_from_output_selector"),
        None,
    )
    assert selector is not None, (
        "query_output_file_from_output_selector missing from tools/list — "
        "the parity contract cannot be evaluated"
    )
    schema = getattr(selector, "inputSchema", None) or {}
    return set((schema.get("properties") or {}).keys())


def _plot_timeseries_arg_names():
    async def go():
        async with Client(mcp) as c:
            return await c.list_prompts()

    prompts = _run(go())
    by_name = {p.name: p for p in prompts}
    return {a.name for a in (by_name["plot_timeseries"].arguments or [])}


@pytest.mark.parametrize("arg_name", OVERLAPPING_ARG_NAMES)
def test_overlapping_arg_names_present_on_both_surfaces(arg_name):
    """Each of the 6 overlapping arg names exists on
    ``plot_timeseries`` AND on
    ``query_output_file_from_output_selector``'s schema. Locks the
    contract one arg at a time so a future rename trips a precise test.
    """
    prompt_args = _plot_timeseries_arg_names()
    selector_args = _selector_tool_schema()

    assert arg_name in prompt_args, (
        f"{arg_name!r} expected on plot_timeseries; got {prompt_args}"
    )
    assert arg_name in selector_args, (
        f"{arg_name!r} expected on query_output_file_from_output_selector; "
        f"got {selector_args}"
    )


@pytest.mark.parametrize("arg_name", NARRATIVE_ONLY_ARG_NAMES)
def test_narrative_only_args_present_on_prompt_absent_on_selector(arg_name):
    """``variable`` and ``feature_id`` are intentionally narrative-only:
    present on ``plot_timeseries`` (they help the LLM build the SQL
    ``query`` value), absent from
    ``query_output_file_from_output_selector``'s schema. Locks the
    intentional partial-alignment so a future refactor doesn't silently
    drop the narrative args or accidentally promote them.
    """
    prompt_args = _plot_timeseries_arg_names()
    selector_args = _selector_tool_schema()

    assert arg_name in prompt_args, (
        f"{arg_name!r} expected on plot_timeseries (narrative-only); "
        f"got {prompt_args}"
    )
    assert arg_name not in selector_args, (
        f"{arg_name!r} unexpectedly present on "
        f"query_output_file_from_output_selector — narrative-only args "
        f"must not be promoted to selector tool args without review"
    )


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_get_prompt_unknown_name_raises_mcp_error():
    """``client.get_prompt("nonexistent_prompt", {})`` raises the
    FastMCP not-found error class (``McpError``).
    """

    async def go():
        async with Client(mcp) as c:
            await c.get_prompt("nonexistent_prompt", {})

    with pytest.raises(McpError):
        _run(go())
