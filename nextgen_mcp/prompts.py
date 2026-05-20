from ._mcp import mcp
from pydantic import Field
from typing import Annotated
from .validation import (
    MODEL_HINT,
    FORECAST_HINT,
    DATE_HINT,
    CYCLE_HINT,
    VPU_HINT,
)

@mcp.prompt
def plot_timeseries(
    variable: Annotated[str, Field(description="flow / velocity / streamflow")],
    feature_id: Annotated[str, Field(description="feature id, e.g., 1019290")],
    model: Annotated[str, Field(description=MODEL_HINT)],
    forecast: Annotated[
        str,
        Field(description=FORECAST_HINT),
    ],
    date: Annotated[str, Field(description=DATE_HINT)],
    cycle: Annotated[str, Field(description=CYCLE_HINT)],
    vpu: Annotated[str, Field(description=VPU_HINT)],
) -> str:
    """Plot a NRDS output-file timeseries as a line chart.

    Renders a natural-language request that drives a downstream
    ``query_files_by_selector`` invocation followed by a line-chart
    visualization of the resulting time series. Designed for the
    chatbox slash-command surface.

    All 7 arguments are ``required: true`` with no Python-level
    defaults; each carries a ``Field(description=...)`` advertising
    the valid format or enum (e.g., ``cfe_nom / lstm / routing_only``,
    ``yyyy-mm-dd``). Calling ``prompts/get(name, {})`` with empty args
    deliberately raises a ``-32602 Invalid arguments`` error - the
    standard MCP wire shape used by third-party servers.

    Slash-command UX: ``chatbox-core`` synthesizes
    ``{argName: "[" + arg.description + "]"}`` for every required
    argument when calling ``prompts/get`` from the popover. The
    rendered prompt then contains the hints inline as
    ``[bracket]`` tokens for the user to replace.

    Hints are derived from the validation types on
    ``query_files_by_selector`` and the NRDS Literal types in
    ``validation.py`` (``MODELS``, ``FORECASTS``, ``DATE_PATTERN``).
    When NRDS adds a new model, forecast, or vpu, update the
    description string here in lockstep.

    Argument names ``model``, ``forecast``, ``date``, ``cycle``, and
    ``vpu`` align with the selector args of ``query_files_by_selector``.
    ``variable`` and ``feature_id`` are narrative-only - they help the
    LLM build the DuckDB ``query`` value but have no first-class
    counterpart in the selector tool's schema.

    The rendered prompt instructs the LLM to omit ``file_name`` and
    ``index`` so the query unions ALL parquet files for the selector;
    ``WHERE feature_id = ...`` in the SQL filters to the single feature
    across the full time series.
    """
    return (
        f"Retrieve a line chart plotting the {variable} time series "
        f"for feature id {feature_id} for the "
        f"{forecast} forecast on {model} model and date {date}, "
        f"cycle {cycle}, and vpu {vpu}. "
        f"Use query_files_by_selector with no file_name or index so "
        f"all parquet files for the selector are unioned. "
        f"Use a query like: SELECT time, {variable} FROM output "
        f"WHERE feature_id = {feature_id} ORDER BY time"
    )


# ---------------------------------------------------------------------------
# Discovery prompt templates - one per list_available_* tool plus
# a zero-arg list_models entry.
#
# Pattern mirrors plot_timeseries above:
#   - Argument names mirror the underlying tool's argument names exactly.
#   - Each routing arg is required:true on the prompt even when the
#     underlying tool would default it. Editors should be explicit
#     about routing decisions when invoking a slash command.
#   - Hint copy is drawn from canonical Literal types in validation.py
#     (MODELS, FORECASTS, DATE_PATTERN). LOCKSTEP RULE: when validation.py
#     adds a new model, forecast, or vpu format, update both the tool's
#     Field(description=...) AND the @mcp.prompt arg description here.
#   - Prose is imperative declarative ("List the available ..."); verb-first
#     matches the underlying tool-name verb and gives small models a clean
#     syntactic anchor.
# ---------------------------------------------------------------------------


@mcp.prompt
def list_models() -> str:
    """List the available NRDS models.

    Drives the ``list_available_models`` tool. Zero-arg by design - the
    underlying tool takes no arguments. FastMCP 3.2.4 silently ignores
    extra kwargs on no-arg prompts (test pinned).
    """
    return "List the available NRDS models."


@mcp.prompt
def list_dates(
    model: Annotated[str, Field(description=MODEL_HINT)],
) -> str:
    """List the available dates for a given NRDS model.

    Drives the ``list_available_dates`` tool. The tool's truly-required
    arg is only ``model``; this prompt surfaces just that.
    """
    return f"List the available dates for the {model} model."


@mcp.prompt
def list_forecasts(
    model: Annotated[str, Field(description=MODEL_HINT)],
    date: Annotated[str, Field(description=DATE_HINT)],
) -> str:
    """List the available forecasts for a given NRDS model and date.

    Drives the ``list_available_forecasts`` tool. ``date`` is defaultable
    in the tool (server-side defaults to today via ``_parse_date_or_today``)
    but is surfaced as required on the prompt so editors are explicit
    about routing.
    """
    return f"List the available forecasts for the {model} model on {date}."


@mcp.prompt
def list_cycles(
    model: Annotated[str, Field(description=MODEL_HINT)],
    date: Annotated[str, Field(description=DATE_HINT)],
    forecast: Annotated[
        str, Field(description=FORECAST_HINT)
    ],
) -> str:
    """List the available cycles for a given NRDS model, date, and forecast.

    Drives the ``list_available_cycles`` tool. ``date`` is defaultable in
    the tool but surfaced as required here so editors are explicit
    about routing.
    """
    return (
        f"List the available cycles for the {model} model on {date}, "
        f"{forecast} forecast."
    )


@mcp.prompt
def list_vpus(
    model: Annotated[str, Field(description=MODEL_HINT)],
    date: Annotated[str, Field(description=DATE_HINT)],
    forecast: Annotated[
        str, Field(description=FORECAST_HINT)
    ],
    cycle: Annotated[str, Field(description=CYCLE_HINT)],
) -> str:
    """List the available VPUs for a given NRDS model, date, forecast, and cycle.

    Drives the ``list_available_vpus`` tool. ``date`` and ``cycle`` are
    defaultable in the tool but surfaced as required here so editors
    are explicit about routing.
    """
    return (
        f"List the available VPUs for the {model} model on {date}, "
        f"{forecast} forecast, cycle {cycle}."
    )


@mcp.prompt
def list_output_files(
    model: Annotated[str, Field(description=MODEL_HINT)],
    date: Annotated[str, Field(description=DATE_HINT)],
    forecast: Annotated[
        str, Field(description=FORECAST_HINT)
    ],
    cycle: Annotated[str, Field(description=CYCLE_HINT)],
    vpu: Annotated[str, Field(description=VPU_HINT)],
) -> str:
    """List the available output files for a given NRDS model, date, forecast,
    cycle, and VPU.

    Drives the ``list_available_output_files`` tool. ``date`` and ``cycle``
    are defaultable in the tool but surfaced as required here so editors
    are explicit about routing. The optional ``ensemble`` arg is
    intentionally not surfaced - only required-shaped routing args
    appear on the prompt.
    """
    return (
        f"List the available output files for the {model} model on {date}, "
        f"{forecast} forecast, cycle {cycle}, vpu {vpu}."
    )


# ---------------------------------------------------------------------------
# Lookup prompt templates - one per lookup tool.
#
# Pattern mirrors the discovery prompts above:
#   - Argument names mirror the underlying tool's argument names exactly.
#   - All routing args are required:true on the prompt.
#   - Hint copy is drawn from the underlying tool's Field(description=...);
#     LOCKSTEP RULE: when the tool's description changes, update both the
#     tool and the @mcp.prompt arg description here.
#   - Prose is imperative declarative.
#
# v0.5.0 deletion note: query_by_url, resolve_file_by_index, and
# resolve_file_by_name lived here previously and targeted query_output_file
# / resolve_output_file. Both target tools were deleted alongside the
# query-cluster consolidation; the prompts were removed in lockstep.
# ---------------------------------------------------------------------------


@mcp.prompt
def lookup_feature(
    hydrofabric_id: Annotated[
        str,
        Field(
            description=(
                "Hydrofabric identifier to search in columns id and divide_id"
            )
        ),
    ],
) -> str:
    """Look up a hydrofabric feature by identifier.

    Drives the ``lookup_hydrofabric_feature`` tool. The tool returns
    matching rows from the hydrofabric index plus the associated PMTiles
    layer name and a bounding box.
    """
    return f"Look up the hydrofabric feature with id {hydrofabric_id}."


