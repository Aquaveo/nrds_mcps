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

# Hint-bearing argument descriptions. Each description is the
# user-facing format hint advertised via `Field(description=...)`
# on the @mcp.prompt arg — derived from the NRDS validation types
# (`MODELS`, `FORECASTS`, `DATE_PATTERN` in `nextgen_mcp/validations.py`
# / `nextgen_mcp/utils.py`) and the `query_output_file_from_output_selector`
# field descriptions. When NRDS adds a new model/forecast/vpu, update
# both the `Field(description=...)` in `mcp_server.py` and this dict
# in lockstep.
#
# Wire shape: every arg is `required: true` with no Python-level default.
# Calling `prompts/get(name, {})` deliberately raises `-32602` (missing
# required args) — chatbox-core synthesizes `[description]` brackets
# client-side via the standard MCP arg.description metadata.
PLOT_TIMESERIES_DESCRIPTIONS = {
    "variable": "flow / velocity / streamflow",
    "feature_id": "feature id, e.g., 1019290",
    "model": "cfe_nom / lstm / routing_only",
    "forecast": "short_range / medium_range / analysis_assim_extend",
    "date": "yyyy-mm-dd",
    "cycle": "00-23, e.g., 00",
    "vpu": "06, VPU_06, or 3W",
    "index": "0-based output index, e.g., 0",
}


def _strip_fastmcp_schema_note(desc: str) -> str:
    """Strip FastMCP's auto-appended JSON-schema note from an arg
    description.

    FastMCP appends ``"\\n\\nProvide as a JSON string matching the
    following schema: {...}"`` to any prompt-arg description whose
    annotation isn't bare ``str``. The chatbox-core client mirrors this
    strip; the tests do the same so assertions can compare against the
    pristine hint string supplied via ``Field(description=...)``.
    """
    if not desc:
        return ""
    return desc.split("\n\nProvide as a JSON string")[0].strip()

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
    ``plot_timeseries`` is among them with all 8 expected argument
    names.
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


def test_all_args_required_with_hint_descriptions():
    """Every plot_timeseries arg is ``required: true`` with no
    server-side default; each carries a ``Field(description=...)``
    hint that, after stripping FastMCP's auto-appended JSON-schema
    note, matches ``PLOT_TIMESERIES_DESCRIPTIONS`` exactly.

    Locks the industry-standard wire shape (required + description,
    no defaults, strict validation) and the NRDS-side hint contract
    that chatbox-core synthesizes brackets from.
    """
    async def go():
        async with Client(mcp) as c:
            return await c.list_prompts()

    prompts = _run(go())
    by_name = {p.name: p for p in prompts}
    prompt = by_name["plot_timeseries"]

    by_arg_name = {a.name: a for a in (prompt.arguments or [])}
    for name, expected_hint in PLOT_TIMESERIES_DESCRIPTIONS.items():
        arg = by_arg_name[name]
        assert arg.required is True, (
            f"arg {name!r} should be required=True; got {arg.required!r}"
        )
        cleaned = _strip_fastmcp_schema_note(arg.description or "")
        assert cleaned == expected_hint, (
            f"arg {name!r} description mismatch — expected "
            f"{expected_hint!r}, got {cleaned!r} (raw: {arg.description!r})"
        )


# ---------------------------------------------------------------------------
# Default-rendering and substitution
# ---------------------------------------------------------------------------


def test_get_prompt_with_no_args_raises_invalid_arguments():
    """Industry-standard wire shape: calling ``prompts/get`` with empty
    arguments against a prompt whose args are all ``required: true``
    raises an MCP error. NRDS no longer follows the K4 placeholder-
    default convention from plan-005 (superseded); chatbox-core
    synthesizes brackets client-side via ``arg.description``.
    """
    async def go():
        async with Client(mcp) as c:
            return await c.get_prompt("plot_timeseries", {})

    with pytest.raises(McpError) as exc_info:
        _run(go())
    msg = str(exc_info.value)
    assert "Missing required arguments" in msg or "required" in msg.lower(), (
        f"expected missing-required-arguments error; got: {msg!r}"
    )


def _synth_bracket_args() -> dict:
    """Mirror the chatbox-core client-side synth: every required arg
    gets ``[<cleaned description>]`` (or ``[<name>]`` if description
    is missing).
    """
    return {
        name: f"[{hint}]" for name, hint in PLOT_TIMESERIES_DESCRIPTIONS.items()
    }


def test_get_prompt_with_synthesized_brackets_renders_all_hints():
    """Calling ``prompts/get`` with the chatbox-core-synthesized
    ``{name: "[<description>]"}`` args produces a rendered prompt
    containing every hint inline — the wire-equivalent of the
    previous server-side K4 placeholder-default convention.
    """
    async def go():
        async with Client(mcp) as c:
            return await c.get_prompt("plot_timeseries", _synth_bracket_args())

    result = _run(go())
    text = _concat_text(result.messages)

    for name, hint in PLOT_TIMESERIES_DESCRIPTIONS.items():
        bracketed = f"[{hint}]"
        assert bracketed in text, (
            f"expected synthesized bracket {bracketed!r} for arg {name!r} "
            f"in rendered prompt; got: {text!r}"
        )


def test_get_prompt_substitutes_supplied_args_only():
    """Supplying ``{"variable": "flow", "feature_id": "1019290"}`` for
    two args + synthesized brackets for the remaining 6 substitutes the
    real values for variable + feature_id while leaving the other 6
    hint brackets intact.
    """
    args = _synth_bracket_args()
    args["variable"] = "flow"
    args["feature_id"] = "1019290"

    async def go():
        async with Client(mcp) as c:
            return await c.get_prompt("plot_timeseries", args)

    result = _run(go())
    text = _concat_text(result.messages)

    # Substituted values present.
    assert "flow" in text, f"expected 'flow' in rendered prompt; got: {text!r}"
    assert "1019290" in text, (
        f"expected '1019290' in rendered prompt; got: {text!r}"
    )

    # The two substituted hints are gone.
    for substituted in ("variable", "feature_id"):
        hint_bracket = f"[{PLOT_TIMESERIES_DESCRIPTIONS[substituted]}]"
        assert hint_bracket not in text, (
            f"hint {hint_bracket!r} for {substituted!r} should have been "
            f"substituted; got: {text!r}"
        )

    # The remaining 6 synthesized hint brackets survive.
    for name in ("model", "forecast", "date", "cycle", "vpu", "index"):
        hint_bracket = f"[{PLOT_TIMESERIES_DESCRIPTIONS[name]}]"
        assert hint_bracket in text, (
            f"expected unsubstituted hint bracket {hint_bracket!r} for "
            f"{name!r} to remain in rendered prompt; got: {text!r}"
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


# ---------------------------------------------------------------------------
# Discovery prompts (Phase 2a) — list_models, list_dates, list_forecasts,
# list_cycles, list_vpus, list_output_files
#
# Each multi-arg discovery prompt carries the same five-test pattern as
# plot_timeseries plus a parametrized arg-name parity block verifying
# every prompt argument exists on the underlying list_available_* tool's
# input schema. list_models is zero-arg and gets a three-test pattern.
# ---------------------------------------------------------------------------


# Hint description per arg name. Drawn from the canonical Literal types in
# nextgen_mcp/validations.py (MODELS, FORECASTS) and the underlying tools'
# field descriptions. Lockstep rule: when validations.py changes, update
# both the tool's Field(description=...) AND this map AND the @mcp.prompt
# arg description.
DISCOVERY_HINTS = {
    "model": "cfe_nom / lstm / routing_only",
    "date": "yyyy-mm-dd",
    "forecast": "short_range / medium_range / analysis_assim_extend",
    "cycle": "00-23, e.g., 00",
    "vpu": "06, VPU_06, or 3W",
}

# Per-prompt arg lists (R6: every routing arg surfaced as required even
# when the underlying tool would default).
DISCOVERY_PROMPTS = {
    "list_dates": ("model",),
    "list_forecasts": ("model", "date"),
    "list_cycles": ("model", "date", "forecast"),
    "list_vpus": ("model", "date", "forecast", "cycle"),
    "list_output_files": ("model", "date", "forecast", "cycle", "vpu"),
}

# Underlying tool name per prompt — used by the parity tests.
DISCOVERY_PROMPT_TO_TOOL = {
    "list_models": "list_available_models",
    "list_dates": "list_available_dates",
    "list_forecasts": "list_available_forecasts",
    "list_cycles": "list_available_cycles",
    "list_vpus": "list_available_vpus",
    "list_output_files": "list_available_output_files",
}


def _list_prompts():
    async def go():
        async with Client(mcp) as c:
            return await c.list_prompts()

    return _run(go())


def _get_prompt(name, args):
    async def go():
        async with Client(mcp) as c:
            return await c.get_prompt(name, args)

    return _run(go())


def _tool_schema_properties(tool_name):
    async def go():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = _run(go())
    tool = next((t for t in tools if t.name == tool_name), None)
    assert tool is not None, (
        f"{tool_name!r} missing from tools/list — parity contract cannot be evaluated"
    )
    schema = getattr(tool, "inputSchema", None) or {}
    return set((schema.get("properties") or {}).keys())


def _synth_brackets(arg_names):
    return {name: f"[{DISCOVERY_HINTS[name]}]" for name in arg_names}


# ---- list_models (zero-arg) -----------------------------------------------


def test_list_prompts_includes_list_models():
    prompts = _list_prompts()
    by_name = {p.name: p for p in prompts}
    assert "list_models" in by_name, (
        f"list_models missing from prompts/list; got {sorted(by_name)}"
    )
    arg_names = {a.name for a in (by_name["list_models"].arguments or [])}
    assert arg_names == set(), (
        f"list_models should have zero arguments; got {arg_names}"
    )


def test_list_models_get_with_no_args_succeeds():
    """Zero-arg prompts: prompts/get(name, {}) must succeed and return
    rendered prose, not raise -32602.
    """
    result = _get_prompt("list_models", {})
    text = _concat_text(result.messages)
    assert text, "list_models rendered prose was empty"
    # Imperative declarative prose includes the verb 'list' and the noun 'models'
    assert "list" in text.lower()
    assert "model" in text.lower()


def test_list_models_get_with_spurious_args_silently_ignored():
    """FastMCP 3.2.4 (pinned in requirements.lock) silently ignores
    extra kwargs on no-arg prompts. Pin that observed behavior so a
    future FastMCP upgrade tightening the contract surfaces as a test
    failure rather than a silent regression.
    """
    result = _get_prompt("list_models", {"unrelated_key": "value"})
    text = _concat_text(result.messages)
    assert text, "list_models rendered prose was empty when spurious args supplied"
    # The spurious key value should NOT leak into the prose
    assert "value" not in text


# ---- list_dates / list_forecasts / list_cycles / list_vpus /
# ---- list_output_files (multi-arg, share a parametrized harness) ----------


@pytest.mark.parametrize("prompt_name", list(DISCOVERY_PROMPTS.keys()))
def test_discovery_prompt_listed_with_expected_args(prompt_name):
    """prompts/list includes the prompt with the expected argument names."""
    prompts = _list_prompts()
    by_name = {p.name: p for p in prompts}
    assert prompt_name in by_name, (
        f"{prompt_name!r} missing from prompts/list; got {sorted(by_name)}"
    )
    arg_names = {a.name for a in (by_name[prompt_name].arguments or [])}
    expected = set(DISCOVERY_PROMPTS[prompt_name])
    assert arg_names == expected, (
        f"{prompt_name!r} args mismatch — expected {expected}, got {arg_names}"
    )


@pytest.mark.parametrize("prompt_name", list(DISCOVERY_PROMPTS.keys()))
def test_discovery_prompt_all_args_required_with_hint_descriptions(prompt_name):
    """Every argument is required:true with a non-empty description that
    matches the canonical hint after stripping FastMCP's auto-appended
    JSON-schema note.
    """
    prompts = _list_prompts()
    by_name = {p.name: p for p in prompts}
    prompt = by_name[prompt_name]
    by_arg = {a.name: a for a in (prompt.arguments or [])}
    for name in DISCOVERY_PROMPTS[prompt_name]:
        arg = by_arg[name]
        assert arg.required is True, (
            f"{prompt_name}.{name!r} should be required=True; got {arg.required!r}"
        )
        cleaned = _strip_fastmcp_schema_note(arg.description or "")
        assert cleaned == DISCOVERY_HINTS[name], (
            f"{prompt_name}.{name!r} description mismatch — expected "
            f"{DISCOVERY_HINTS[name]!r}, got {cleaned!r}"
        )


@pytest.mark.parametrize("prompt_name", list(DISCOVERY_PROMPTS.keys()))
def test_discovery_prompt_get_with_no_args_raises(prompt_name):
    """prompts/get(name, {}) raises -32602 when args are required."""
    with pytest.raises(McpError) as exc_info:
        _get_prompt(prompt_name, {})
    msg = str(exc_info.value)
    assert "Missing required arguments" in msg or "required" in msg.lower(), (
        f"{prompt_name}: expected missing-required-arguments error; got: {msg!r}"
    )


@pytest.mark.parametrize("prompt_name", list(DISCOVERY_PROMPTS.keys()))
def test_discovery_prompt_synthesized_brackets_render_all_hints(prompt_name):
    """Calling prompts/get with chatbox-core-synthesized {name: '[hint]'}
    args produces a rendered prompt containing every hint bracket inline.
    """
    arg_names = DISCOVERY_PROMPTS[prompt_name]
    result = _get_prompt(prompt_name, _synth_brackets(arg_names))
    text = _concat_text(result.messages)
    for name in arg_names:
        bracketed = f"[{DISCOVERY_HINTS[name]}]"
        assert bracketed in text, (
            f"{prompt_name}: expected synthesized bracket {bracketed!r} for "
            f"arg {name!r} in rendered prompt; got: {text!r}"
        )


@pytest.mark.parametrize("prompt_name", list(DISCOVERY_PROMPTS.keys()))
def test_discovery_prompt_substitutes_supplied_args_only(prompt_name):
    """Supplying real values for some args + synthesized brackets for the
    rest substitutes the real values while leaving unsupplied hints intact.
    """
    arg_names = DISCOVERY_PROMPTS[prompt_name]
    if not arg_names:
        pytest.skip("no args to substitute")
    args = _synth_brackets(arg_names)
    # Substitute the first arg with a real value
    first = arg_names[0]
    real_values = {
        "model": "cfe_nom",
        "date": "2026-05-03",
        "forecast": "medium_range",
        "cycle": "06",
        "vpu": "06",
    }
    args[first] = real_values[first]

    result = _get_prompt(prompt_name, args)
    text = _concat_text(result.messages)

    # Substituted value present
    assert real_values[first] in text, (
        f"{prompt_name}: expected substituted {real_values[first]!r} for arg "
        f"{first!r} in rendered prompt; got: {text!r}"
    )
    # The substituted hint is gone
    substituted_hint = f"[{DISCOVERY_HINTS[first]}]"
    assert substituted_hint not in text, (
        f"{prompt_name}: hint {substituted_hint!r} for {first!r} should have "
        f"been substituted; got: {text!r}"
    )
    # Remaining hints survive
    for name in arg_names[1:]:
        bracketed = f"[{DISCOVERY_HINTS[name]}]"
        assert bracketed in text, (
            f"{prompt_name}: expected unsubstituted hint {bracketed!r} for "
            f"{name!r} to remain; got: {text!r}"
        )


# ---- Per-prompt arg-name parity with the underlying list_available_* tool


def _parametrize_per_prompt_arg_pairs():
    """Yield (prompt_name, arg_name) tuples for the parametrize decorator
    so each (prompt, arg) pair gets its own test case — failures point at
    a precise drift, not a bulk mismatch.
    """
    for prompt_name, arg_names in DISCOVERY_PROMPTS.items():
        for arg_name in arg_names:
            yield (prompt_name, arg_name)


@pytest.mark.parametrize(
    "prompt_name,arg_name", list(_parametrize_per_prompt_arg_pairs())
)
def test_discovery_prompt_arg_name_parity_with_underlying_tool(
    prompt_name, arg_name
):
    """Each prompt argument name exists on the underlying list_available_*
    tool's input schema. Catches arg-name drift between prompt and tool —
    the #1 risk in this plan (per feedback_input_output_name_alignment.md).
    """
    tool_name = DISCOVERY_PROMPT_TO_TOOL[prompt_name]
    tool_args = _tool_schema_properties(tool_name)
    assert arg_name in tool_args, (
        f"{prompt_name}.{arg_name!r} not found on tool {tool_name!r} "
        f"input schema; tool args: {tool_args}"
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
