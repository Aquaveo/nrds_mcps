# mcp_server.py
import logging
import os
from typing import Optional, Dict, Any, List, Literal
from typing_extensions import Annotated
from pydantic import Field
from fastmcp import FastMCP
from datetime import datetime
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from .utils import (
    _get_json_raw,
    _prefer_id_objects,
    _as_id,
    _parse_iso_date,
    DEFAULT_TZ,
    DEFAULT_START,
    DATE_PATTERN,
    _date_from_item,
)
from .validations import (
    FORECASTS,
    MODELS
)
from ._input_validation_middleware import (
    InputValidationEnvelopeMiddleware,
    InvalidLLMInputError,
)
from ._observability_middleware import ToolCallObservabilityMiddleware

# Middleware order:
#   - ToolCallObservabilityMiddleware OUTERMOST so it observes the final
#     envelope after validation middleware has converted ValidationError
#     to a structured tool result.
#   - InputValidationEnvelopeMiddleware INNER so it catches pydantic
#     ValidationError before it bubbles out.
mcp = FastMCP(
    "NRDS MCP Server",
    middleware=[
        ToolCallObservabilityMiddleware(),
        InputValidationEnvelopeMiddleware(),
    ],
)
LOGGER = logging.getLogger("nextgen_mcp.mcp_server")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    """Liveness probe used by Docker HEALTHCHECK and container orchestrators.

    Returns 200 with a minimal payload. Does not exercise downstream
    dependencies (S3, etc.) — keep it cheap so polling stays free.
    """
    return JSONResponse({"status": "ok"})

def _preview_text(value: Optional[str], limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."

def _configure_runtime_logging() -> None:
    level_name = os.getenv("NRDS_LOG_LEVEL", "INFO").upper()
    level_value = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Configure this module logger explicitly so it always prints
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level_value)
    stream_handler.setFormatter(formatter)

    LOGGER.handlers.clear()
    LOGGER.addHandler(stream_handler)
    LOGGER.setLevel(level_value)
    LOGGER.propagate = False

    # Optional: keep related loggers at the same level
    logging.getLogger("nextgen_plugins.chatbox.rest").setLevel(level_value)
    logging.getLogger("mcp").setLevel(level_value)
    logging.getLogger("mcp.server").setLevel(level_value)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(level_value)

    LOGGER.info("Runtime logging configured with level=%s", level_name)


# ---- Date bounds helpers (DEFAULT_START .. today in DEFAULT_TZ) ----
_MIN_ALLOWED_DATE = _parse_iso_date(DEFAULT_START)


def _validate_date_bounds(d, field_name: str):
    today = datetime.now(DEFAULT_TZ).date()
    LOGGER.debug(
        "Validating date bounds for field=%s value=%s allowed_range=[%s, %s]",
        field_name,
        d,
        _MIN_ALLOWED_DATE,
        today,
    )
    if d < _MIN_ALLOWED_DATE or d > today:
        LOGGER.warning(
            "Date validation failed for field=%s value=%s allowed_range=[%s, %s]",
            field_name,
            d,
            _MIN_ALLOWED_DATE,
            today,
        )
        raise InvalidLLMInputError(
            f"'{field_name}' must be between {_MIN_ALLOWED_DATE} and {today} (got {d})"
        )
    return d


def _parse_date_or_today(date_str: Optional[str], field_name: str):
    LOGGER.debug("Parsing date for field=%s raw_value=%s", field_name, date_str)
    d = (
        _parse_iso_date(date_str)
        if date_str is not None
        else datetime.now(DEFAULT_TZ).date()
    )
    validated = _validate_date_bounds(d, field_name)
    LOGGER.debug("Parsed date for field=%s resolved_value=%s", field_name, validated)
    return validated


def _require(**kwargs):
    """Validate that required parameters are not None. Returns error dict or None."""
    missing = [k for k, v in kwargs.items() if v is None]
    if missing:
        discovery = {"model": "list_available_models", "forecast": "list_available_forecasts", "vpu": "list_available_vpus", "query": "the query is required"}
        hints = [f"{k} (use {discovery.get(k, 'discovery')})" for k in missing]
        return {"error": f"Missing required parameters: {', '.join(hints)}. Call the appropriate discovery tool first."}
    return None


@mcp.tool(name="list_available_models", description="List available NRDS models. It should not have any arguments when called.")
def list_available_models_tool() -> Dict[str, Any]:
    LOGGER.info("Tool list_available_models called")
    raw = _get_json_raw("list_available_models")
    result = _prefer_id_objects(raw, "models")
    LOGGER.info(
        "Tool list_available_models completed count=%s",
        len((result.get("models") or [])) if isinstance(result, dict) else None,
    )
    return result


@mcp.tool(
    name="list_available_dates",
    description=(
        "List available dates for a given model (returns id + label). "
        "Supports date-range filtering with start/end (ISO YYYY-MM-DD or YYYY/MM/DD). "
        "Defaults: start=earliest available date, end=today (America/Denver). "
        "Pagination is applied after filtering using offset/limit."
    ),
)
def list_available_dates_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    offset: Annotated[
        int, Field(ge=0, description="Number of items to skip for pagination (default 0)")
    ] = 0,
    limit: Annotated[
        int,
        Field(ge=0, description="Maximum number of items to return (default 0 for all)"),
    ] = 0,
    start: Annotated[
        str,
        Field(
            default=DEFAULT_START,
            pattern=DATE_PATTERN,
            description="Start date (inclusive). ISO YYYY-MM-DD or YYYY/MM/DD. Defaults to earliest available date.",
        ),
    ] = DEFAULT_START,
    end: Annotated[
        Optional[str],
        Field(
            default=None,
            pattern=DATE_PATTERN,
            description="End date (inclusive). ISO YYYY-MM-DD or YYYY/MM/DD. Default is today's date.",
        ),
    ] = None,
) -> Dict[str, Any]:
    err = _require(model=model)
    if err:
        return err
    LOGGER.info(
        "Tool list_available_dates called model=%s offset=%s limit=%s start=%s end=%s",
        model,
        offset,
        limit,
        start,
        end,
    )

    start_date = _validate_date_bounds(_parse_iso_date(start), "start")
    end_date = _parse_date_or_today(end, "end")

    if start_date > end_date:
        LOGGER.warning(
            "Invalid date range in list_available_dates model=%s start=%s end=%s",
            model,
            start_date,
            end_date,
        )
        raise InvalidLLMInputError(
            f"'start' must be <= 'end' (got start={start_date}, end={end_date})"
        )

    raw = _get_json_raw("list_available_dates", params={"model": model})
    raw = _prefer_id_objects(raw, "dates")

    dates = raw.get("dates") or []

    filtered: list[dict[str, Any]] = []
    for item in dates:
        di = _date_from_item(item)
        if di is None:
            continue
        if start_date <= di <= end_date:
            filtered.append(item)

    total_count = len(filtered)

    if offset or limit:
        filtered = filtered[offset : (offset + limit) if limit else None]

    raw["dates"] = filtered
    raw["count"] = len(filtered)
    raw["total_count"] = total_count

    result = _prefer_id_objects(raw, "dates")
    LOGGER.info(
        "Tool list_available_dates completed model=%s returned_count=%s total_filtered=%s",
        model,
        raw["count"],
        total_count,
    )
    return result


@mcp.tool(
    name="list_available_forecasts",
    description="List available forecasts for a given model and date",
)
def list_available_forecasts_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(
            description="YYYY-MM-DD or YYYY/MM/DD",
            pattern=r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})$",
        ),
    ] = None,
) -> Dict[str, Any]:
    err = _require(model=model)
    if err:
        return err
    LOGGER.info("Tool list_available_forecasts called model=%s date=%s", model, date)
    end_date = _parse_date_or_today(date, "date")
    raw = _get_json_raw(
        "list_available_forecasts", params={"model": model, "date": end_date.isoformat()}
    )
    result = _prefer_id_objects(raw, "forecasts")
    LOGGER.info(
        "Tool list_available_forecasts completed model=%s date=%s count=%s",
        model,
        end_date.isoformat(),
        len((result.get("forecasts") or [])) if isinstance(result, dict) else None,
    )
    return result


@mcp.tool(
    name="list_available_cycles",
    description="List available cycles for a given model, date, and forecast",
)
def list_available_cycles_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(
            description="YYYY-MM-DD or YYYY/MM/DD",
            pattern=r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})$",
        ),
    ] = None,
    forecast: Annotated[
        FORECASTS,
        Field(
            description="Forecast id — call list_available_forecasts to discover valid values",
            pattern=r"^(short_range|medium_range|analysis_assim_extend)$",
        ),
    ] = None,
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast)
    if err:
        return err
    forecast_id = _as_id(forecast)
    LOGGER.info(
        "Tool list_available_cycles called model=%s date=%s forecast=%s",
        model,
        date,
        forecast_id,
    )
    end_date = _parse_date_or_today(date, "date")
    raw = _get_json_raw(
        "list_available_cycles",
        params={"model": model, "date": end_date.isoformat(), "forecast": forecast_id},
    )
    result = _prefer_id_objects(raw, "cycles")
    LOGGER.info(
        "Tool list_available_cycles completed model=%s date=%s forecast=%s count=%s",
        model,
        end_date.isoformat(),
        forecast_id,
        len((result.get("cycles") or [])) if isinstance(result, dict) else None,
    )
    return result


@mcp.tool(
    name="list_available_vpus",
    description="List available VPUs for a given model, date, forecast, and cycle",
)
def list_available_vpus_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(
            description="YYYY-MM-DD or YYYY/MM/DD",
            pattern=r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})$",
        ),
    ] = None,
    forecast: Annotated[
        FORECASTS,
        Field(
            description="Forecast id — call list_available_forecasts to discover valid values",
            pattern=r"^(short_range|medium_range|analysis_assim_extend)$",
        ),
    ] = None,
    cycle: Annotated[
        str,
        Field(
            description="Hourly cycle (00-23). short_range forecast (hourly, every hour), "
            "medium_range forecast (4 times per day, every 6 hours, first member), "
            "analysis_assim_extend forecast (once per day at 16z)",
            pattern=r"^(?:[01]\d|2[0-3])$",
        ),
    ] = "00",
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast)
    if err:
        return err
    forecast_id = _as_id(forecast)
    LOGGER.info(
        "Tool list_available_vpus called model=%s date=%s forecast=%s cycle=%s",
        model,
        date,
        forecast_id,
        cycle,
    )
    end_date = _parse_date_or_today(date, "date")
    raw = _get_json_raw(
        "list_available_vpus",
        params={"model": model, "date": end_date.isoformat(), "forecast": forecast_id, "cycle": cycle},
    )
    result = _prefer_id_objects(raw, "vpus")
    LOGGER.info(
        "Tool list_available_vpus completed model=%s date=%s forecast=%s cycle=%s count=%s",
        model,
        end_date.isoformat(),
        forecast_id,
        cycle,
        len((result.get("vpus") or [])) if isinstance(result, dict) else None,
    )
    return result


@mcp.tool(
    name="list_available_output_files",
    description="List available output files for a given model, date, forecast, cycle, and VPU (accepts id or label, including subregion VPUs). Optional ensemble member for applicable forecast.",
)
def list_available_output_files_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(
            description="YYYY-MM-DD or YYYY/MM/DD",
            pattern=r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})$",
        ),
    ] = None,
    forecast: Annotated[
        FORECASTS,
        Field(
            description="Forecast id — call list_available_forecasts to discover valid values",
            pattern=r"^(short_range|medium_range|analysis_assim_extend)$",
        ),
    ] = None,
    cycle: Annotated[
        str,
        Field(
            description="Hourly cycle (00-23). short_range forecast (hourly, every hour), "
            "medium_range forecast (4 times per day, every 6 hours, first member), "
            "analysis_assim_extend forecast (once per day at 16z)",
            pattern=r"^(?:[01]\d|2[0-3])$",
        ),
    ] = "00",
    vpu: Annotated[
        str,
        Field(
            description="VPU identifier — call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
        ),
    ] = None,
    ensemble: Annotated[
        Optional[str], Field(description="Optional ensemble member (1 or 16)", pattern=r"^(?:1|16)$")
    ] = None,
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast, vpu=vpu)
    if err:
        return err
    LOGGER.info(
        "Tool list_available_output_files called model=%s date=%s forecast=%s cycle=%s vpu=%s ensemble=%s",
        model,
        date,
        _as_id(forecast),
        cycle,
        _as_id(vpu),
        ensemble,
    )
    end_date = _parse_date_or_today(date, "date")
    params: Dict[str, Any] = {
        "model": model,
        "date": end_date.isoformat(),
        "forecast": _as_id(forecast),
        "cycle": cycle,
        "vpu": _as_id(vpu),
    }
    if ensemble is not None:
        params["ensemble"] = int(ensemble)

    raw = _get_json_raw("list_available_output_files", params=params)
    result = _prefer_id_objects(raw, "files")
    LOGGER.info(
        "Tool list_available_output_files completed model=%s date=%s forecast=%s cycle=%s vpu=%s count=%s",
        model,
        end_date.isoformat(),
        params["forecast"],
        cycle,
        params["vpu"],
        len((result.get("files") or [])) if isinstance(result, dict) else None,
    )
    return result


@mcp.tool(
    name="resolve_output_file",
    description="Resolve a single output file path for model/date/forecast/cycle/vpu. Provide exactly one of file_name or index.",
)
def resolve_output_file_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(description="YYYY-MM-DD or YYYY/MM/DD", pattern=DATE_PATTERN),
    ] = None,
    forecast: Annotated[FORECASTS, Field(description="Forecast id — call list_available_forecasts to discover valid values")] = None,
    cycle: Annotated[str, Field(description="Cycle", pattern=r"^(?:[01]\d|2[0-3])$")] = "00",
    vpu: Annotated[
        str,
        Field(
            description="VPU identifier — call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
        ),
    ] = None,
    ensemble: Annotated[
        Optional[str],
        Field(description="Ensemble (medium_range)", pattern=r"^\d+$"),
    ] = None,
    file_name: Annotated[
        Optional[str],
        Field(description="Exact filename (e.g. troute_output_...parquet)"),
    ] = None,
    index: Annotated[
        Optional[int],
        Field(description="0-based index into sorted file list", ge=0),
    ] = 0,
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast, vpu=vpu)
    if err:
        return err
    LOGGER.info(
        "Tool resolve_output_file called model=%s date=%s forecast=%s cycle=%s vpu=%s ensemble=%s file_name=%s index=%s",
        model,
        date,
        _as_id(forecast),
        cycle,
        _as_id(vpu),
        ensemble,
        file_name,
        index,
    )
    if (file_name is None) == (index is None):
        LOGGER.warning(
            "Invalid resolve_output_file call: exactly one of file_name or index is required"
        )
        raise InvalidLLMInputError(
            "Provide exactly one of 'file_name' or 'index'."
        )

    end_date = _parse_date_or_today(date, "date")
    params: Dict[str, Any] = {
        "model": model,
        "date": end_date.isoformat(),
        "forecast": _as_id(forecast),
        "cycle": cycle,
        "vpu": _as_id(vpu),
    }
    if ensemble is not None:
        params["ensemble"] = ensemble
    if file_name is not None:
        params["file_name"] = file_name
    if index is not None:
        params["index"] = index

    result = _get_json_raw("get_output_file", params=params)
    LOGGER.info(
        "Tool resolve_output_file completed model=%s date=%s forecast=%s cycle=%s vpu=%s",
        model,
        end_date.isoformat(),
        params["forecast"],
        cycle,
        params["vpu"],
    )
    return result

@mcp.tool(
    name="query_output_file_from_output_selector",
    description=(
        "Resolve one NRDS output file from model/date/forecast/cycle/vpu and run a read-only "
        "DuckDB SQL query against it in one step. "
        "Supports parquet (.parquet) and netcdf (.nc, .nc4). "
        "Use this when you know model/date/forecast/cycle/vpu instead of a direct s3_url. "
        "If file_name is provided it is used; otherwise index is used and defaults to 0 "
        "(the first sorted output file). "
        "The SQL query must be a single read-only SELECT or WITH...SELECT statement and must read FROM output."
    ),
)
def query_output_file_from_output_selector_tool(
    model: Annotated[MODELS, Field(description="Model id — call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(description="YYYY-MM-DD or YYYY/MM/DD", pattern=DATE_PATTERN),
    ] = None,
    forecast: Annotated[FORECASTS, Field(description="Forecast id — call list_available_forecasts to discover valid values")] = None,
    cycle: Annotated[
        str,
        Field(
            description="Cycle (00-23)",
            pattern=r"^(?:[01]\d|2[0-3])$",
        ),
    ] = "00",
    vpu: Annotated[
        str,
        Field(
            description="VPU identifier — call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
        ),
    ] = None,
    query: Annotated[
        str,
        Field(
            description=(
                "DuckDB SQL query against table `output`. "
                "Single read-only SELECT or WITH...SELECT statement only. Must read FROM output."
            ),
            pattern=r"(?is)^\s*(?:WITH\b.*?\bSELECT\b|SELECT\b).*$",
        ),
    ] = "SELECT * FROM output LIMIT 10",
    ensemble: Annotated[
        Optional[str],
        Field(description="Optional ensemble member for medium_range.", pattern=r"^\d+$"),
    ] = None,
    file_name: Annotated[
        Optional[str],
        Field(
            description=(
                "Exact filename to query. If provided, it is used and index is ignored."
            )
        ),
    ] = None,
    index: Annotated[
        Optional[int],
        Field(
            description=(
                "0-based index into the sorted output file list. "
                "Used only when file_name is not provided. Defaults to 0 (first file)."
            ),
            ge=0,
        ),
    ] = 0,
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast, vpu=vpu)
    if err:
        return err
    LOGGER.info(
        "Tool query_output_file_from_output_selector called model=%s date=%s forecast=%s cycle=%s "
        "vpu=%s ensemble=%s file_name=%s index=%s query_preview=%s",
        model,
        date,
        _as_id(forecast),
        cycle,
        _as_id(vpu),
        ensemble,
        file_name,
        index,
        _preview_text(query),
    )

    end_date = _parse_date_or_today(date, "date")
    params: Dict[str, Any] = {
        "model": model,
        "date": end_date.isoformat(),
        "forecast": _as_id(forecast),
        "cycle": cycle,
        "vpu": _as_id(vpu),
        "query": query,
    }

    if ensemble is not None:
        params["ensemble"] = ensemble

    if file_name is not None:
        params["file_name"] = file_name
    else:
        params["index"] = 0 if index is None else index

    result = _get_json_raw("query_output_file_from_output_selector", params=params)

    LOGGER.info(
        "Tool query_output_file_from_output_selector completed model=%s date=%s forecast=%s cycle=%s vpu=%s",
        model,
        end_date.isoformat(),
        params["forecast"],
        cycle,
        params["vpu"],
    )
    LOGGER.info(
        "query_output_file_from_output_selector result: %s",
        result,
    )
    return result

@mcp.tool(
    name="query_output_file",
    description=(
        "Run a read-only DuckDB SQL query against ONE NRDS output file in S3. "
        "Supports parquet (.parquet) and netcdf (.nc, .nc4). "
        "The file is exposed as table `output`."
    ),
)
def query_output_file_tool(
    s3_url: Annotated[
        str,
        Field(
            description="Full URL to ONE parquet or netcdf output file (s3://... or https://...)",
            pattern=r"^(?:https://|s3://).+\.(?:parquet|nc|nc4)$",
        ),
    ],
    query: Annotated[
        str,
        Field(
            description="DuckDB SQL query against table `output`.",
            pattern=r"(?is)^\s*(?:WITH\b.*?\bSELECT\b|SELECT\b).*$",
        ),
    ],
) -> Dict[str, Any]:
    LOGGER.info(
        "Tool query_output_file called s3_url=%s query_preview=%s",
        s3_url,
        _preview_text(query),
    )
    result =  _get_json_raw("query_output_file", params={"s3_url": s3_url, "query": query})
    LOGGER.info("Tool query_output_file completed s3_url=%s", s3_url)
    return result

@mcp.tool(
    name="query_hydrofabric_parquet_file",
    description=(
        "Lookup rows in the hydrofabric index parquet file in S3 by hydrofabric identifier. "
        "Provide hydrofabric_id. "
        "The tool searches columns id and divide_id using exact and substring matching. "
        "This tool does not accept s3_url or raw SQL."
    ),
)
def query_hydrofabric_parquet_file(
    hydrofabric_id: Annotated[
        str,
        Field(
            description="Hydrofabric identifier to search for in columns id and divide_id."
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of matching rows to return.",
            ge=1,
            le=200,
        ),
    ] = 50,
) -> Dict[str, Any]:
    LOGGER.info(
        "Tool query_hydrofabric_parquet_file called hydrofabric_id=%s limit=%s",
        hydrofabric_id,
        limit,
    )
    result = _get_json_raw(
        "query_hydrofabric_parquet_file",
        params={"hydrofabric_id": hydrofabric_id, "limit": limit},
    )
    LOGGER.info(
        "Tool query_hydrofabric_parquet_file completed hydrofabric_id=%s limit=%s",
        hydrofabric_id,
        limit,
    )
    return result


@mcp.tool(
    name="lookup_hydrofabric_feature",
    description=(
        "Look up a hydrofabric feature by identifier and return data only. "
        "Returns matching rows from the hydrofabric index, the associated PMTiles "
        "layer name, and a bounding box for the feature. Returns an empty result "
        "(no rows, null layer, null bbox) when nothing matches. "
        "The host is responsible for any map rendering or visualization built from this data."
    ),
)
def lookup_hydrofabric_feature(
    hydrofabric_id: Annotated[
        str,
        Field(description="Hydrofabric identifier to search in columns id and divide_id.")
    ]
) -> Dict[str, Any]:
    LOGGER.info(
        "Tool lookup_hydrofabric_feature called hydrofabric_id=%s",
        hydrofabric_id,
    )
    result = _get_json_raw(
        "lookup_hydrofabric_feature",
        params={"hydrofabric_id": hydrofabric_id},
    )
    LOGGER.info(
        "Tool lookup_hydrofabric_feature completed hydrofabric_id=%s",
        hydrofabric_id,
    )
    return result


@mcp.prompt
def plot_timeseries(
    variable: Annotated[str, Field(description="flow / velocity / streamflow")],
    feature_id: Annotated[str, Field(description="feature id, e.g., 1019290")],
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    forecast: Annotated[
        str,
        Field(description="short_range / medium_range / analysis_assim_extend"),
    ],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
    cycle: Annotated[str, Field(description="00-23, e.g., 00")],
    vpu: Annotated[str, Field(description="06, VPU_06, or 3W")],
    index: Annotated[str, Field(description="0-based output index, e.g., 0")],
) -> str:
    """Plot a NRDS output-file timeseries as a line chart.

    Renders a natural-language request that drives a downstream
    ``query_output_file_from_output_selector`` invocation followed by a
    line-chart visualization of the resulting time series. Designed for
    the chatbox slash-command surface.

    All 8 arguments are ``required: true`` with no Python-level
    defaults; each carries a ``Field(description=...)`` advertising
    the valid format or enum (e.g., ``cfe_nom / lstm / routing_only``,
    ``yyyy-mm-dd``). Calling ``prompts/get(name, {})`` with empty args
    deliberately raises a ``-32602 Invalid arguments`` error — the
    standard MCP wire shape used by third-party servers.

    Slash-command UX: ``chatbox-core`` synthesizes
    ``{argName: "[" + arg.description + "]"}`` for every required
    argument when calling ``prompts/get`` from the popover. The
    rendered prompt then contains the hints inline as
    ``[bracket]`` tokens for the user to replace.

    Hints are derived from the validation types on
    ``query_output_file_from_output_selector`` and the NRDS Literal
    types in ``validations.py`` (``MODELS``, ``FORECASTS``,
    ``DATE_PATTERN``). When NRDS adds a new model, forecast, or vpu,
    update the description string here in lockstep.

    Argument names ``model``, ``forecast``, ``date``, ``cycle``, ``vpu``,
    and ``index`` align with the selector args of
    ``query_output_file_from_output_selector``. ``variable`` and
    ``feature_id`` are narrative-only — they help the LLM build the
    DuckDB ``query`` value but have no first-class counterpart in the
    selector tool's schema.
    """
    return (
        f"Retrieve a line chart plotting the {variable} time series "
        f"for feature id {feature_id} for output index {index} for the "
        f"{forecast} forecast on {model} model and date {date}, "
        f"cycle {cycle}, and vpu {vpu}. "
        f"Use a query like: SELECT time, {variable} FROM output "
        f"WHERE feature_id = {feature_id}"
    )


# ---------------------------------------------------------------------------
# Discovery prompt templates (Phase 2a) — one per list_available_* tool plus
# a zero-arg list_models entry.
#
# Pattern mirrors plot_timeseries above:
#   - Argument names mirror the underlying tool's argument names exactly.
#   - Each routing arg is required:true on the prompt (per plan R6, even
#     when the underlying tool would default; editors should be explicit
#     about routing decisions when invoking a slash command).
#   - Hint copy is drawn from canonical Literal types in validations.py
#     (MODELS, FORECASTS, DATE_PATTERN). LOCKSTEP RULE: when validations.py
#     adds a new model, forecast, or vpu format, update both the tool's
#     Field(description=...) AND the @mcp.prompt arg description here.
#   - Prose is imperative declarative ("List the available …"); verb-first
#     matches the underlying tool-name verb and gives small models a clean
#     syntactic anchor.
# ---------------------------------------------------------------------------


@mcp.prompt
def list_models() -> str:
    """List the available NRDS models.

    Drives the ``list_available_models`` tool. Zero-arg by design — the
    underlying tool takes no arguments. FastMCP 3.2.4 silently ignores
    extra kwargs on no-arg prompts (test pinned).
    """
    return "List the available NRDS models."


@mcp.prompt
def list_dates(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
) -> str:
    """List the available dates for a given NRDS model.

    Drives the ``list_available_dates`` tool. The tool's truly-required
    arg is only ``model``; this prompt surfaces just that.
    """
    return f"List the available dates for the {model} model."


@mcp.prompt
def list_forecasts(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
) -> str:
    """List the available forecasts for a given NRDS model and date.

    Drives the ``list_available_forecasts`` tool. ``date`` is defaultable
    in the tool (server-side defaults to today via ``_parse_date_or_today``)
    but is surfaced as required on the prompt per plan R6 — editors
    should be explicit about routing.
    """
    return f"List the available forecasts for the {model} model on {date}."


@mcp.prompt
def list_cycles(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
    forecast: Annotated[
        str, Field(description="short_range / medium_range / analysis_assim_extend")
    ],
) -> str:
    """List the available cycles for a given NRDS model, date, and forecast.

    Drives the ``list_available_cycles`` tool. ``date`` is defaultable in
    the tool but surfaced as required here per plan R6.
    """
    return (
        f"List the available cycles for the {model} model on {date}, "
        f"{forecast} forecast."
    )


@mcp.prompt
def list_vpus(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
    forecast: Annotated[
        str, Field(description="short_range / medium_range / analysis_assim_extend")
    ],
    cycle: Annotated[str, Field(description="00-23, e.g., 00")],
) -> str:
    """List the available VPUs for a given NRDS model, date, forecast, and cycle.

    Drives the ``list_available_vpus`` tool. ``date`` and ``cycle`` are
    defaultable in the tool but surfaced as required here per plan R6.
    """
    return (
        f"List the available VPUs for the {model} model on {date}, "
        f"{forecast} forecast, cycle {cycle}."
    )


@mcp.prompt
def list_output_files(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
    forecast: Annotated[
        str, Field(description="short_range / medium_range / analysis_assim_extend")
    ],
    cycle: Annotated[str, Field(description="00-23, e.g., 00")],
    vpu: Annotated[str, Field(description="06, VPU_06, or 3W")],
) -> str:
    """List the available output files for a given NRDS model, date, forecast,
    cycle, and VPU.

    Drives the ``list_available_output_files`` tool. ``date`` and ``cycle``
    are defaultable in the tool but surfaced as required here per plan R6.
    The optional ``ensemble`` arg is intentionally not surfaced — only
    required-shaped routing args appear on the prompt (see plan Scope
    Boundaries: "No optional-argument hint surfaces").
    """
    return (
        f"List the available output files for the {model} model on {date}, "
        f"{forecast} forecast, cycle {cycle}, vpu {vpu}."
    )


# ---------------------------------------------------------------------------
# Query/lookup prompt templates (Phase 2b) — one per query/lookup tool plus
# a second variant for resolve_output_file's XOR.
#
# Pattern mirrors the discovery prompts above:
#   - Argument names mirror the underlying tool's argument names exactly.
#   - All routing args are required:true on the prompt (per plan R6).
#   - Hint copy is drawn from the underlying tool's Field(description=...);
#     LOCKSTEP RULE: when the tool's description changes, update both the
#     tool and the @mcp.prompt arg description here.
#   - Prose is imperative declarative.
#   - The two resolve_file_* variants split the file_name XOR index
#     constraint so the user picks intent at the slash level — see each
#     variant's "do NOT also supply" instruction.
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


@mcp.prompt
def query_hydrofabric(
    hydrofabric_id: Annotated[
        str,
        Field(
            description=(
                "Hydrofabric identifier to search in columns id and divide_id"
            )
        ),
    ],
) -> str:
    """Query the hydrofabric index parquet for a given identifier.

    Drives the ``query_hydrofabric_parquet_file`` tool. The optional
    ``limit`` arg is intentionally not surfaced — only required-shaped
    args appear on the prompt (see plan Scope Boundaries).
    """
    return (
        f"Query the hydrofabric index parquet for the feature with id "
        f"{hydrofabric_id}."
    )


@mcp.prompt
def query_by_url(
    s3_url: Annotated[
        str,
        Field(
            description=(
                "Full URL to ONE parquet or netcdf output file "
                "(s3://... or https://...)"
            )
        ),
    ],
    query: Annotated[
        str, Field(description="DuckDB SQL query against table output")
    ],
) -> str:
    """Run a DuckDB SQL query against a single NRDS output file.

    Drives the ``query_output_file`` tool. The arg name ``s3_url`` is
    surfaced as-is on the prompt (it mirrors the underlying tool's arg
    name per the parity contract); the description is the user-friendly
    explanation.
    """
    return (
        f"Run the DuckDB SQL query {query} against the output file at "
        f"{s3_url}."
    )


@mcp.prompt
def resolve_file_by_index(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
    forecast: Annotated[
        str,
        Field(description="short_range / medium_range / analysis_assim_extend"),
    ],
    cycle: Annotated[str, Field(description="00-23, e.g., 00")],
    vpu: Annotated[str, Field(description="06, VPU_06, or 3W")],
    index: Annotated[str, Field(description="0-based output index, e.g., 0")],
) -> str:
    """Resolve a single output file by index in the sorted output-file list.

    Drives the ``resolve_output_file`` tool. The XOR constraint on
    ``resolve_output_file`` (file_name XOR index) is resolved at the
    slash level — this variant supplies ``index`` and the LLM should NOT
    also supply ``file_name``. ``index`` defaults to 0 on the underlying
    tool but is surfaced as required on the prompt per plan R6.
    """
    return (
        f"Resolve the output file by index {index} for the {model} model "
        f"on {date}, {forecast} forecast, cycle {cycle}, vpu {vpu}. "
        f"Do NOT also supply file_name."
    )


@mcp.prompt
def resolve_file_by_name(
    model: Annotated[str, Field(description="cfe_nom / lstm / routing_only")],
    date: Annotated[str, Field(description="yyyy-mm-dd")],
    forecast: Annotated[
        str,
        Field(description="short_range / medium_range / analysis_assim_extend"),
    ],
    cycle: Annotated[str, Field(description="00-23, e.g., 00")],
    vpu: Annotated[str, Field(description="06, VPU_06, or 3W")],
    file_name: Annotated[
        str, Field(description="Exact filename (e.g. troute_output_...parquet)")
    ],
) -> str:
    """Resolve a single output file by exact filename.

    Drives the ``resolve_output_file`` tool. The XOR constraint on
    ``resolve_output_file`` (file_name XOR index) is resolved at the
    slash level — this variant supplies ``file_name`` and the LLM should
    NOT also supply ``index``. The underlying tool's ``index`` defaults
    to 0 (not None), so explicitly passing both file_name and index would
    fail the XOR check; the docstring instruction tells the LLM to omit
    index in this variant.
    """
    return (
        f"Resolve the output file by exact filename {file_name} for the "
        f"{model} model on {date}, {forecast} forecast, cycle {cycle}, "
        f"vpu {vpu}. Do NOT also supply index."
    )


def _parse_allowed_origins() -> List[str]:
    """Read ALLOWED_ORIGINS from env (comma-separated). Defaults to wildcard.

    Production deployments behind a known origin (tethysdash, an MCP playground,
    a custom client) should set this explicitly via the deploy YAML's
    --set-env-vars=ALLOWED_ORIGINS=https://example.com[,https://other.com] to
    tighten the surface. The wildcard default matches the test deployment's
    "no auth, public ingress" posture.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


ALLOWED_ORIGINS = _parse_allowed_origins()
# CORS spec forbids `allow_credentials=True` together with `allow_origins=["*"]`.
# We don't use cookies/Authorization in this MCP server (the SSE endpoint is
# public unauthenticated by design), so drop credentials to allow the wildcard.
ALLOW_CREDENTIALS = ALLOWED_ORIGINS != ["*"]


def _patch_sse_transport_for_cors():
    """Monkey-patch SseServerTransport.handle_post_message to handle OPTIONS.

    MCP SDK v1.26+ validates Content-Type on all requests routed to
    handle_post_message, including CORS preflight OPTIONS (which have no
    Content-Type). This patch intercepts OPTIONS and returns 200 with
    CORS headers before the SDK's validation runs.
    """
    from mcp.server.sse import SseServerTransport

    original_handle = SseServerTransport.handle_post_message

    async def patched_handle(self, scope, receive, send):
        if scope.get("method") == "OPTIONS":
            origin = dict(scope.get("headers", [])).get(b"origin", b"").decode()
            if ALLOWED_ORIGINS == ["*"]:
                allow_origin = "*"
            else:
                allow_origin = origin if origin in ALLOWED_ORIGINS else ""
            headers = {
                "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "content-type, x-csrftoken, authorization",
                "access-control-max-age": "86400",
            }
            if allow_origin:
                headers["access-control-allow-origin"] = allow_origin
            if ALLOW_CREDENTIALS and allow_origin and allow_origin != "*":
                headers["access-control-allow-credentials"] = "true"
            response = Response(status_code=200, headers=headers)
            await response(scope, receive, send)
            return
        await original_handle(self, scope, receive, send)

    SseServerTransport.handle_post_message = patched_handle

_patch_sse_transport_for_cors()


CORS_MIDDLEWARE = [
    Middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
    ),
]


def main() -> None:
    _configure_runtime_logging()
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "9000"))
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    LOGGER.info("Starting NRDS MCP Server on %s:%d with %s transport", host, port, transport)
    mcp.run(
        transport=transport,
        host=host,
        port=port,
        middleware=CORS_MIDDLEWARE,
    )


if __name__ == "__main__":
    main()