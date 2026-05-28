# mcp_server.py

from ._mcp import mcp, LOGGER
from typing import Optional, Dict, Any, Callable
from typing_extensions import Annotated
from pydantic import Field
from pydantic.functional_validators import BeforeValidator
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
    _summarize_tool_result,
    _coerce_none_string,
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
    query_files_by_selector,
    lookup_hydrofabric_feature as _lookup_hydrofabric_feature,
    get_hydrofabric_pmtiles_layers
)
from ._tool_descriptions import (
    LIST_AVAILABLE_OUTPUT_FILES_DESCRIPTION,
    QUERY_FILES_BY_SELECTOR_DESCRIPTION,
)

from .middleware._input_validation_middleware import InvalidLLMInputError


def _run_list(
    *,
    log_name: str,
    envelope_key: str,
    required: Optional[Dict[str, Any]],
    log_fields: Dict[str, Any],
    call: Callable[[], Any],
) -> Dict[str, Any]:
    """Shared boilerplate for list_available_* tool bodies.

    Centralizes the call/completion log lines, the `_require()` envelope
    return on missing args, and the `_prefer_id_objects()` envelope-key
    wrapping. Tools that need to normalize args (e.g., `_as_id`,
    `_parse_date_or_today`) before logging can run their own `_require`
    up-front and pass `required=None` to skip the re-check here.

    `list_available_dates` does NOT use this helper — its pagination + range
    filtering need a distinct body shape.
    """
    if required is not None:
        err = _require(**required)
        if err:
            return err
    fields_str = " ".join(f"{k}={v}" for k, v in log_fields.items())
    LOGGER.info("Tool %s called %s", log_name, fields_str)
    raw = call()
    result = _prefer_id_objects(raw, envelope_key)
    count = (
        len((result.get(envelope_key) or []))
        if isinstance(result, dict)
        else None
    )
    LOGGER.info("Tool %s completed %s count=%s", log_name, fields_str, count)
    return result


@mcp.tool(name="list_available_models", description="List available NRDS models. It should not have any arguments when called.")
def list_available_models_tool() -> Dict[str, Any]:
    return _run_list(
        log_name="list_available_models",
        envelope_key="models",
        required=None,
        log_fields={},
        call=list_available_models,
    )

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
    end_date = _parse_date_or_today(date, "date")
    iso_date = end_date.isoformat()
    return _run_list(
        log_name="list_available_forecasts",
        envelope_key="forecasts",
        required={"model": model},
        log_fields={"model": model, "date": iso_date},
        call=lambda: list_available_forecasts(model=model, date=iso_date),
    )


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
    iso_date = _parse_date_or_today(date, "date").isoformat()
    return _run_list(
        log_name="list_available_cycles",
        envelope_key="cycles",
        required=None,
        log_fields={"model": model, "date": iso_date, "forecast": forecast_id},
        call=lambda: list_available_cycles(
            model=model, date=iso_date, forecast=forecast_id
        ),
    )


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
    iso_date = _parse_date_or_today(date, "date").isoformat()
    return _run_list(
        log_name="list_available_vpus",
        envelope_key="vpus",
        required=None,
        log_fields={
            "model": model,
            "date": iso_date,
            "forecast": forecast_id,
            "cycle": cycle,
        },
        call=lambda: list_available_vpus(
            model=model, date=iso_date, forecast=forecast_id, cycle=cycle
        ),
    )


@mcp.tool(
    name="list_available_output_files",
    description=LIST_AVAILABLE_OUTPUT_FILES_DESCRIPTION,
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
        Optional[str],
        BeforeValidator(_coerce_none_string),
        Field(
            description=(
                "Ensemble member for medium_range forecast. Current data uses "
                "ensemble 1; defaults to 1 when omitted. Ignored for "
                "short_range and analysis_assim_extend (no ensemble dimension)."
            ),
        ),
    ] = None,
) -> Dict[str, Any]:
    err = _require(model=model, forecast=forecast, vpu=vpu)
    if err:
        return err
    iso_date = _parse_date_or_today(date, "date").isoformat()
    params: Dict[str, Any] = {
        "model": model,
        "date": iso_date,
        "forecast": _as_id(forecast),
        "cycle": cycle,
        "vpu": _as_id(vpu),
    }
    if ensemble is not None:
        params["ensemble"] = int(ensemble)
    return _run_list(
        log_name="list_available_output_files",
        envelope_key="files",
        required=None,
        log_fields={
            "model": params["model"],
            "date": params["date"],
            "forecast": params["forecast"],
            "cycle": params["cycle"],
            "vpu": params["vpu"],
            "ensemble": ensemble,
        },
        call=lambda: list_available_output_files(data=params),
    )


@mcp.tool(
    name="query_files_by_selector",
    description=QUERY_FILES_BY_SELECTOR_DESCRIPTION,
)
def query_files_by_selector_tool(
    model: Annotated[
        MODELS,
        Field(description="Model id - call list_available_models to discover valid values"),
    ] = None,
    date: Annotated[
        Optional[str],
        Field(description="YYYY-MM-DD or YYYY/MM/DD", pattern=DATE_PATTERN),
    ] = None,
    forecast: Annotated[
        FORECASTS,
        Field(description="Forecast id - call list_available_forecasts to discover valid values"),
    ] = None,
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
            description=(
                "VPU identifier - call list_available_vpus to discover valid values. "
                "Accepts formats like '06', 'VPU_06', or '3W'"
            ),
        ),
    ] = None,
    query: Annotated[
        str,
        Field(
            description=(
                "DuckDB SQL query against table `output` (parquet files unioned with "
                "filename + source_path provenance columns). Single read-only SELECT "
                "or WITH...SELECT statement only. Must read FROM output. Prefer WHERE "
                "filtering over LIMIT for data extraction; LIMIT silently drops rows."
            ),
            pattern=r"(?is)^\s*(?:WITH\b.*?\bSELECT\b|SELECT\b).*$",
        ),
    ] = "SELECT filename, COUNT(*) AS rows_per_file FROM output GROUP BY filename ORDER BY filename",
    ensemble: Annotated[
        Optional[str],
        BeforeValidator(_coerce_none_string),
        Field(
            description=(
                "Ensemble member for medium_range forecast. Current data uses "
                "ensemble 1; defaults to 1 when omitted. Ignored for "
                "short_range and analysis_assim_extend (no ensemble dimension)."
            ),
        ),
    ] = None,
    file_name: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional filter to one file by exact name. Mutually exclusive with "
                "index. Omit both to query all parquet files for the selector."
            ),
            min_length=1,
        ),
    ] = None,
    index: Annotated[
        Optional[int],
        Field(
            description=(
                "Optional filter to one file by 0-based index into the sorted "
                "parquet file list for the selector. Mutually exclusive with "
                "file_name."
            ),
            ge=0,
        ),
    ] = None,
) -> Dict[str, Any]:
    LOGGER.info(
        "Tool query_files_by_selector called model=%s date=%s forecast=%s cycle=%s "
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
    result = query_files_by_selector(
        model=model,
        date=end_date.isoformat(),
        forecast=_as_id(forecast),
        cycle=cycle,
        vpu=_as_id(vpu),
        query=query,
        ensemble=ensemble,
        file_name=file_name,
        index=index,
    )

    LOGGER.info(
        "Tool query_files_by_selector completed model=%s date=%s forecast=%s "
        "cycle=%s vpu=%s result=%s",
        model,
        end_date.isoformat(),
        _as_id(forecast),
        cycle,
        _as_id(vpu),
        _summarize_tool_result(result),
    )
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