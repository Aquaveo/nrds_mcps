# mcp_server.py

from ._mcp import mcp, LOGGER
from typing import Optional, Dict, Any
from typing_extensions import Annotated
from pydantic import Field
from .utils import (
    _prefer_id_objects,
    _as_id,
    _parse_iso_date,
    DEFAULT_START,
    _date_from_item,
    _require,
    _parse_date_or_today,
    _validate_date_bounds,
    _preview_text,
)
from .validation import (
    DATE_PATTERN,
    FORECASTS,
    MODELS,
)
from .logic import (
    list_available_models,
    list_available_dates,
    list_available_forecasts,
    list_available_cycles,
    list_available_vpus,
    list_available_output_files,
    get_output_file,
    query_output_file,
    query_output_file_from_output_selector,
    query_output_files_from_output_selector,
    lookup_hydrofabric_feature as _lookup_hydrofabric_feature,
    get_hydrofabric_pmtiles_layers
)

from .middleware._input_validation_middleware import InvalidLLMInputError

@mcp.tool(name="list_available_models", description="List available NRDS models. It should not have any arguments when called.")
def list_available_models_tool() -> Dict[str, Any]:
    LOGGER.info("Tool list_available_models called")
    raw = list_available_models()
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
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

    raw = list_available_dates(model=model)
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
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
    raw = list_available_forecasts(model=model, date=end_date.isoformat())
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
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
            description="Forecast id - call list_available_forecasts to discover valid values",
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
    raw = list_available_cycles(
        model=model, date=end_date.isoformat(), forecast=forecast_id
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
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
            description="Forecast id - call list_available_forecasts to discover valid values",
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
    raw = list_available_vpus(
        model=model,
        date=end_date.isoformat(),
        forecast=forecast_id,
        cycle=cycle,
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
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
            description="Forecast id - call list_available_forecasts to discover valid values",
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
            description="VPU identifier - call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
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

    raw = list_available_output_files(data=params)
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(description="YYYY-MM-DD or YYYY/MM/DD", pattern=DATE_PATTERN),
    ] = None,
    forecast: Annotated[FORECASTS, Field(description="Forecast id - call list_available_forecasts to discover valid values")] = None,
    cycle: Annotated[str, Field(description="Cycle", pattern=r"^(?:[01]\d|2[0-3])$")] = "00",
    vpu: Annotated[
        str,
        Field(
            description="VPU identifier - call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
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

    result = get_output_file(
        model=params["model"],
        date=params["date"],
        forecast=params["forecast"],
        cycle=params["cycle"],
        vpu=params["vpu"],
        file_name=params.get("file_name"),
        index=params.get("index"),
        ensemble=params.get("ensemble"),
    )
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
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(description="YYYY-MM-DD or YYYY/MM/DD", pattern=DATE_PATTERN),
    ] = None,
    forecast: Annotated[FORECASTS, Field(description="Forecast id - call list_available_forecasts to discover valid values")] = None,
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
            description="VPU identifier - call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
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

    result = query_output_file_from_output_selector(
        model=params["model"],
        date=params["date"],
        forecast=params["forecast"],
        cycle=params["cycle"],
        vpu=params["vpu"],
        query=params["query"],
        ensemble=params.get("ensemble"),
        file_name=params.get("file_name"),
        index=params.get("index"),
    )

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
    name="query_output_files_from_output_selector",
    description=(
        "Run ONE read-only DuckDB SQL query across ALL parquet output files for a "
        "selector (model/date/forecast/cycle/vpu, plus ensemble for medium_range) "
        "as a single combined dataset. "
        "Use this when the question spans the whole output bundle (aggregations, "
        "ranking across files, max/min/avg) — the database does the merge in one "
        "S3-streaming pass instead of one call per file. "
        "Every result row carries a `filename` column identifying its source file. "
        "Parquet only — use `query_output_file_from_output_selector` for single-file "
        "queries or for NetCDF outputs. "
        "The SQL must be a single read-only SELECT or WITH...SELECT and must read "
        "FROM output. Add LIMIT or aggregate (COUNT, SUM, AVG) to bound response size."
    ),
)
def query_output_files_from_output_selector_tool(
    model: Annotated[MODELS, Field(description="Model id - call list_available_models to discover valid values")] = None,
    date: Annotated[
        Optional[str],
        Field(description="YYYY-MM-DD or YYYY/MM/DD", pattern=DATE_PATTERN),
    ] = None,
    forecast: Annotated[FORECASTS, Field(description="Forecast id - call list_available_forecasts to discover valid values")] = None,
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
            description="VPU identifier - call list_available_vpus to discover valid values. Accepts formats like '06', 'VPU_06', or '3W'"
        ),
    ] = None,
    query: Annotated[
        str,
        Field(
            description=(
                "DuckDB SQL query against table `output` (the union of all parquet files in the selector, "
                "with an added `filename` column). Single read-only SELECT or WITH...SELECT only. Must read FROM output."
            ),
            pattern=r"(?is)^\s*(?:WITH\b.*?\bSELECT\b|SELECT\b).*$",
        ),
    ] = "SELECT filename, * FROM output LIMIT 10",
    ensemble: Annotated[
        Optional[str],
        Field(description="Optional ensemble member for medium_range.", pattern=r"^\d+$"),
    ] = None,
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast, vpu=vpu)
    if err:
        return err
    LOGGER.info(
        "Tool query_output_files_from_output_selector called model=%s date=%s forecast=%s cycle=%s "
        "vpu=%s ensemble=%s query_preview=%s",
        model,
        date,
        _as_id(forecast),
        cycle,
        _as_id(vpu),
        ensemble,
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

    result = query_output_files_from_output_selector(
        model=params["model"],
        date=params["date"],
        forecast=params["forecast"],
        cycle=params["cycle"],
        vpu=params["vpu"],
        query=params["query"],
        ensemble=params.get("ensemble"),
    )

    LOGGER.info(
        "Tool query_output_files_from_output_selector completed model=%s date=%s forecast=%s cycle=%s vpu=%s file_count=%s",
        model,
        end_date.isoformat(),
        params["forecast"],
        cycle,
        params["vpu"],
        result.get("file_count") if isinstance(result, dict) else None,
    )
    LOGGER.info(
        "query_output_files_from_output_selector result: %s",
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
    result = query_output_file(s3_url=s3_url, query=query)
    LOGGER.info("Tool query_output_file completed s3_url=%s", s3_url)
    return result


@mcp.tool(
    name="lookup_hydrofabric_feature",
    description=(
        "Look up hydrofabric features by identifier and return data only. "
        "Searches columns id and divide_id using exact and substring matching. "
        "Returns up to `limit` matching rows from the hydrofabric index, plus "
        "the PMTiles layer name and bounding box derived from the top match. "
        "Returns an empty result (no rows, null layer, null bbox) when nothing "
        "matches. The host is responsible for any map rendering or visualization "
        "built from this data."
    ),
)
def lookup_hydrofabric_feature_tool(
    hydrofabric_id: Annotated[
        str,
        Field(description="Hydrofabric identifier to search in columns id and divide_id.")
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of matching rows to return.",
            ge=1,
            le=200,
        ),
    ] = 1,
) -> Dict[str, Any]:
    LOGGER.info(
        "Tool lookup_hydrofabric_feature called hydrofabric_id=%s limit=%s",
        hydrofabric_id,
        limit,
    )
    result = _lookup_hydrofabric_feature(hydrofabric_id=hydrofabric_id, limit=limit)
    LOGGER.info(
        "Tool lookup_hydrofabric_feature completed hydrofabric_id=%s limit=%s",
        hydrofabric_id,
        limit,
    )
    return result

@mcp.tool(
    name="get_hydrofabric_pmtiles_layers",
    description=(
        "Get the list of hydrofabric PMTiles layers available, along with their metadata. "
        "This can be used to discover which layers are available and their corresponding map layer ids for rendering."
    ),
)
def get_hydrofabric_pmtiles_layers_tool() -> Dict[str, Any]:
    LOGGER.info("Tool get_hydrofabric_pmtiles_layers called")
    result = get_hydrofabric_pmtiles_layers()
    LOGGER.info("Tool get_hydrofabric_pmtiles_layers completed layer_count=%s", len(result.get("layers", [])))
    return result