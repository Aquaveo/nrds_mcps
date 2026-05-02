# nextgen_plugins/chatbox/rest.py
import fsspec
import os
import json
import logging
import pandas as pd

from datetime import datetime
from typing import Dict, List, Any, Optional
from .validators import OutputsFilesQuery
from pydantic import ValidationError
from .utils_rest import (
    _extract_yyyymmdd_from_date_folder,
    _label_from_id,
    _normalize_date_folder,
    _duckdb_query_parquet,
    _duckdb_query_netcdf,
    _get_troute_df,
    _duckdb_query_hydrofabric_parquet,
    _duckdb_lookup_hydrofabric_feature,
    _normalize_record,
    _get_feature_center,
    _pick_filter_value,
    HYDROFABRIC_LAYER_CONFIG,
    validate_output_sql,
    _create_plotly_chart,
    _success_payload,
    _error_payload,
    _list_payload,
    _validate_nrds_output_file_url,
    _detect_output_file_kind,
    _normalize_output_file_url
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BUCKET = os.getenv("BUCKET", "ciroh-community-ngen-datastream")
OUTPUTS_DIR = "outputs"
PREFIX_HYDROFABRIC = "v2.2_hydrofabric"
NGEN_RUN_PREFIX = "ngen-run/outputs/troute"
HYDROFABRIC_INDEX_URL = (
    "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/hydrofabric_index.parquet"
)


def _ensure_full_s3_url(path: str) -> str:
    p = str(path or "").strip()
    if p.startswith(("s3://", "https://")):
        return p
    return f"s3://{p.lstrip('/')}"


def list_available_output_files(data) -> Dict:
    """List outputs for a given model, date, forecast, cycle, and vpu."""
    logger.info(f"Received request to list available output files with data: {data}")
    try:
        q = OutputsFilesQuery.model_validate(data)
    except ValidationError as e:
        return _error_payload(
            "validation_error",
            "Invalid output-file query parameters.",
            details=e.errors(),
        )
    except ValueError as e:
        return _error_payload(
            "validation_error",
            str(e),
        )

    model = q.model
    date_folder = _normalize_date_folder(q.date)
    forecast = q.forecast
    cycle = q.cycle
    vpu = q.vpu

    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date_folder}/{forecast}/{cycle}"
    if forecast == "medium_range":
        s3_url += f"/{q.ensemble}/{vpu}/{NGEN_RUN_PREFIX}"
    else:
        s3_url += f"/{vpu}/{NGEN_RUN_PREFIX}"

    try:
        fs = fsspec.filesystem("s3", anon=True)
        outputs = fs.ls(s3_url, detail=False)
        outputs = sorted(outputs)

        files = []
        for f in outputs:
            name = f.split("/troute/")[-1]
            path = _ensure_full_s3_url(f)
            files.append(
                {
                    "id": name,
                    "label": name,
                    "name": name,
                    "path": path,
                }
            )

        logger.info(f"Found {len(files)} files at {s3_url}")
        return _list_payload("files", files, path=s3_url)

    except FileNotFoundError:
        logger.info(f"No files found at {s3_url}")
        return _list_payload("files", [], path=s3_url)


def get_output_file(model, date, forecast, cycle, vpu, file_name=None, index=None, ensemble=None) -> Dict:
    logger.info(
        f"Received request to get output file with model={model}, date={date}, "
        f"forecast={forecast}, cycle={cycle}, vpu={vpu}, file_name={file_name}, "
        f"index={index}, ensemble={ensemble}"
    )

    if (file_name is None) == (index is None):
        logger.error("Exactly one of file_name or index must be provided.")
        return _error_payload(
            "bad_request",
            "Provide exactly one of file_name or index.",
        )

    date = _normalize_date_folder(date)
    s3_dir = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/{forecast}/{cycle}"
    if forecast == "medium_range":
        ens = ensemble or "1"
        s3_dir += f"/{ens}/{vpu}/{NGEN_RUN_PREFIX}"
    else:
        s3_dir += f"/{vpu}/{NGEN_RUN_PREFIX}"

    try:
        fs = fsspec.filesystem("s3", anon=True)
        files = fs.ls(s3_dir, detail=False)

        files = [f for f in files if f.lower().endswith(".parquet") or f.lower().endswith(".nc")]
        files = sorted(files)

        items = [{"name": f.split("/")[-1], "path": _ensure_full_s3_url(f)} for f in files]

        if file_name is not None:
            sel = next((it for it in items if it["name"] == file_name), None)
            if not sel:
                logger.error(f"file_name '{file_name}' not found in {s3_dir}")
                return _error_payload(
                    "not_found",
                    f"file_name not found: {file_name}",
                    dir=s3_dir,
                    count=len(items),
                )
        else:
            try:
                idx = int(index)
            except Exception:
                logger.error(f"Invalid index value: {index}. Must be an integer.")
                return _error_payload(
                    "bad_request",
                    "index must be an integer",
                    dir=s3_dir,
                    count=len(items),
                )

            if idx < 0 or idx >= len(items):
                logger.error(f"Index out of range: {index}. Must be between 0 and {len(items)-1}.")
                return _error_payload(
                    "bad_request",
                    f"index out of range: {idx}",
                    dir=s3_dir,
                    count=len(items),
                )
            sel = items[idx]

        logger.info(f"Selected file for retrieval: {sel['name']} at {sel['path']}")
        return _success_payload(
            dir=s3_dir,
            count=len(items),
            selected=sel,
        )

    except FileNotFoundError:
        logger.info(f"No files found at {s3_dir}")
        return _success_payload(
            dir=s3_dir,
            count=0,
            selected=None,
        )


def list_available_vpus(model, date, forecast, cycle) -> Dict:
    """List VPUs for a given model, date, forecast, and cycle."""
    logger.info(f"Listing VPUs for model={model}, date={date}, forecast={forecast}, cycle={cycle}")
    date = _normalize_date_folder(date)
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/{forecast}/{cycle}"
    if forecast == "medium_range":
        s3_url += "/1"

    try:
        fs = fsspec.filesystem("s3", anon=True)
        dirs = fs.ls(s3_url, detail=False)

        vpu_ids = sorted(d.split("/")[-1] for d in dirs)
        vpus = [{"id": vpu_id, "label": _label_from_id(vpu_id)} for vpu_id in vpu_ids]

        logger.info(f"Found VPUs at {s3_url}: {[v['label'] for v in vpus]}")
        return _list_payload("vpus", vpus, path=s3_url)
    except FileNotFoundError:
        logger.info(f"No VPUs found at {s3_url}")
        return _list_payload("vpus", [], path=s3_url)


def list_available_cycles(model, date, forecast) -> Dict:
    """List available cycles for a given model, date, and forecast."""
    logger.info(f"Listing cycles for model={model}, date={date}, forecast={forecast}")
    date = _normalize_date_folder(date)
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/{forecast}/"

    try:
        fs = fsspec.filesystem("s3", anon=True)
        dirs = fs.ls(s3_url, detail=False)

        cycle_ids = [d.split("/")[-1] for d in dirs]
        cycles = [{"id": c, "label": c} for c in cycle_ids]
        logger.info(f"Found cycles at {s3_url}: {cycle_ids}")
        return _list_payload("cycles", cycles, path=s3_url)

    except FileNotFoundError:
        logger.info(f"No cycles found at {s3_url}")
        return _list_payload("cycles", [], path=s3_url)


def list_available_dates(model) -> Dict:
    """List available dates for a given model."""
    logger.info(f"Listing dates for model={model}")
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}"

    try:
        fs = fsspec.filesystem("s3", anon=True)
        dirs = fs.ls(s3_url, detail=False)

        date_ids = [d.split("/")[-1].rstrip("/") for d in dirs]  # e.g. ngen.20260218
        dates = []
        for folder in date_ids:
            yyyymmdd = _extract_yyyymmdd_from_date_folder(folder)
            if yyyymmdd:
                label = datetime.strptime(yyyymmdd, "%Y%m%d").date().isoformat()
            else:
                label = folder

            dates.append({
                "id": folder,
                "label": label,
            })

        dates = sorted(dates, key=lambda x: x["label"], reverse=True)

        logger.info(f"Found dates at {s3_url}: {[d['label'] for d in dates]}")
        return _list_payload("dates", dates, path=s3_url)

    except FileNotFoundError:
        logger.info(f"No dates found at {s3_url}")
        return _list_payload("dates", [], path=s3_url)


def list_available_forecasts(model, date) -> Dict:
    """List available forecasts for a given model, date."""
    logger.info(f"Listing forecasts for model={model}, date={date}")
    date = _normalize_date_folder(date)
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/"
    try:
        fs = fsspec.filesystem("s3", anon=True)
        dirs = fs.ls(s3_url, detail=False)

        forecast_ids = [d.split("/")[-1] for d in dirs]
        forecast_labels = [_label_from_id(f) for f in forecast_ids]

        forecasts = [{"id": fid, "label": lbl} for fid, lbl in zip(forecast_ids, forecast_labels)]
        logger.info(f"Found forecasts at {s3_url}: {forecast_labels}")
        return _list_payload("forecasts", forecasts, path=s3_url)
    except FileNotFoundError:
        logger.info(f"No forecasts found at {s3_url}")
        return _list_payload("forecasts", [], path=s3_url)


def list_available_models() -> Dict:
    logger.info(f"Listing available models in bucket={BUCKET} under {OUTPUTS_DIR}")
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}"
    fs = fsspec.filesystem("s3", anon=True)
    try:
        dirs = fs.ls(s3_url, detail=False)
        model_ids = [d.split("/")[-1] for d in dirs]
        logger.info(f"Found models at {s3_url}: {model_ids}")
        models = [{"id": mid, "label": mid} for mid in model_ids]
        return _list_payload("models", models, path=s3_url)
    except FileNotFoundError:
        logger.info(f"No models found at {s3_url}")
        return _list_payload("models", [], path=s3_url)


# def query_netcdf_output_file(s3_url, query) -> Dict:
#     """Run a read-only DuckDB query against a netcdf output file on S3 (view name: `output`)."""
#     logger.info(f"Received request to query NetCDF file at {s3_url} with query: {query}")
#     file_url = s3_url
#     if file_url.startswith("s3://ciroh-community-ngen-datastream"):
#         file_url = file_url.replace(
#             "s3://ciroh-community-ngen-datastream",
#             "https://ciroh-community-ngen-datastream.s3.us-east-1.amazonaws.com",
#         )

#     if not file_url:
#         logger.error("Missing required query param: s3_url")
#         return _error_payload(
#             "bad_request",
#             "Missing required query param: s3_url",
#         )

#     try:
#         query = validate_output_sql(query)
#     except ValueError as e:
#         logger.error(f"Invalid SQL query: {e}")
#         return _error_payload(
#             "validation_error",
#             str(e),
#             file=file_url,
#             query=query,
#         )

#     try:
#         initial_df = _get_troute_df(file_url)
#         logger.info(
#             f"Initial DataFrame loaded with {len(initial_df)} rows and columns: {initial_df.columns.tolist()}"
#         )
#         df = _duckdb_query_netcdf(initial_df, query)

#         if "time" in df.columns:
#             df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

#         logger.info(f"Query returned {len(df)} rows and columns: {df.columns.tolist()}")
#         return _success_payload(
#             file=file_url,
#             query=query,
#             columns=list(df.columns),
#             rows=int(len(df)),
#             data=df.to_dict(orient="records"),
#         )
#     except FileNotFoundError:
#         logger.error(f"File not found: {file_url}")
#         return _error_payload(
#             "not_found",
#             f"File not found: {file_url}",
#             file=file_url,
#             query=query,
#             columns=[],
#             rows=0,
#             data=[],
#         )
#     except Exception as e:
#         logger.error(f"Error querying NetCDF file: {e}")
#         return _error_payload(
#             "execution_error",
#             str(e),
#             file=file_url,
#             query=query,
#         )


# def query_parquet_output_file(s3_url, query) -> Dict:
#     """Run a read-only DuckDB query against a Parquet output file on S3 (view name: `output`)."""

#     raw_url = str(s3_url or "").strip()
#     err = _validate_nrds_output_file_url(BUCKET,raw_url, (".parquet",))
#     if err:
#         return _error_payload(
#             "validation_error",
#             err,
#             file=raw_url,
#             query=query,
#         )
#     file_url = raw_url
#     logger.info(f"Received query request for file: {file_url} with query: {query}")
#     if file_url.startswith("s3://ciroh-community-ngen-datastream"):
#         file_url = file_url.replace(
#             "s3://ciroh-community-ngen-datastream",
#             "https://ciroh-community-ngen-datastream.s3.us-east-1.amazonaws.com",
#         )

#     if not file_url:
#         logger.error("Missing required query param: s3_url")
#         return _error_payload(
#             "bad_request",
#             "Missing required query param: s3_url",
#         )

#     try:
#         query = validate_output_sql(query)
#     except ValueError as e:
#         logger.error(f"Invalid SQL query: {e}")
#         return _error_payload(
#             "validation_error",
#             str(e),
#             file=file_url,
#             query=query,
#         )

#     try:
#         df = _duckdb_query_parquet(file_url, query)

#         if "time" in df.columns:
#             df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

#         logger.info(f"Query returned {len(df)} rows and columns: {df.columns.tolist()}")
#         return _success_payload(
#             file=file_url,
#             query=query,
#             columns=list(df.columns),
#             rows=int(len(df)),
#             data=df.to_dict(orient="records"),
#         )
#     except FileNotFoundError:
#         logger.error(f"File not found: {file_url}")
#         return _error_payload(
#             "not_found",
#             f"File not found: {file_url}",
#             file=file_url,
#             query=query,
#             columns=[],
#             rows=0,
#             data=[],
#         )
#     except Exception as e:
#         logger.error(f"Error querying Parquet file: {e}")
#         return _error_payload(
#             "execution_error",
#             str(e),
#             file=file_url,
#             query=query,
#         )

def query_parquet_output_file(s3_url, query) -> Dict:
    """Backward-compatible parquet wrapper."""
    return query_output_file(s3_url=s3_url, query=query)


def query_netcdf_output_file(s3_url, query) -> Dict:
    """Backward-compatible netcdf wrapper."""
    return query_output_file(s3_url=s3_url, query=query)

def query_output_file(s3_url, query) -> Dict:
    """Run a read-only DuckDB query against one NRDS output file in S3 (parquet or netcdf)."""
    raw_url = str(s3_url or "").strip()
    kind = _detect_output_file_kind(raw_url)

    if kind == "parquet":
        err = _validate_nrds_output_file_url(BUCKET, raw_url, (".parquet",))
    elif kind == "netcdf":
        err = _validate_nrds_output_file_url(BUCKET, raw_url, (".nc", ".nc4"))
    else:
        err = "s3_url must point to one .parquet, .nc, or .nc4 NRDS output file"

    if err:
        return _error_payload(
            "validation_error",
            err,
            file=raw_url,
            query=query,
        )

    file_url = _normalize_output_file_url(raw_url)
    logger.info("Received query request for %s file: %s with query: %s", kind, file_url, query)

    try:
        query = validate_output_sql(query)
    except ValueError as e:
        logger.error("Invalid SQL query: %s", e)
        return _error_payload(
            "validation_error",
            str(e),
            file=file_url,
            query=query,
        )

    try:
        if kind == "parquet":
            df = _duckdb_query_parquet(file_url, query)
        else:
            initial_df = _get_troute_df(file_url)
            logger.info(
                "Initial NetCDF DataFrame loaded with %s rows and columns: %s",
                len(initial_df),
                initial_df.columns.tolist(),
            )
            df = _duckdb_query_netcdf(initial_df, query)

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        logger.info("Query returned %s rows and columns: %s", len(df), df.columns.tolist())
        return _success_payload(
            file=file_url,
            file_type=kind,
            query=query,
            columns=list(df.columns),
            rows=int(len(df)),
            data=df.to_dict(orient="records"),
        )

    except FileNotFoundError:
        logger.error("File not found: %s", file_url)
        return _error_payload(
            "not_found",
            f"File not found: {file_url}",
            file=file_url,
            file_type=kind,
            query=query,
            columns=[],
            rows=0,
            data=[],
        )
    except Exception as e:
        logger.error("Error querying %s file: %s", kind, e)
        return _error_payload(
            "execution_error",
            str(e),
            file=file_url,
            file_type=kind,
            query=query,
        )

def query_output_file_from_output_selector(
    model,
    date,
    forecast,
    cycle,
    vpu,
    query,
    ensemble: Optional[str] = None,
    file_name: Optional[str] = None,
    index: Optional[int] = 0,
) -> Dict:
    """Resolve an output file by selector and run a raw query against the selected parquet or netcdf file."""

    logger.info(
        "Received request to query output file from selector with "
        "model=%s date=%s forecast=%s cycle=%s vpu=%s ensemble=%s file_name=%s index=%s query=%s",
        model,
        date,
        forecast,
        cycle,
        vpu,
        ensemble,
        file_name,
        index,
        query,
    )

    resolved = get_output_file(
        model=model,
        date=date,
        forecast=forecast,
        cycle=cycle,
        vpu=vpu,
        file_name=file_name,
        index=None if file_name is not None else (0 if index is None else index),
        ensemble=ensemble,
    )

    if not isinstance(resolved, dict):
        return _error_payload(
            "execution_error",
            "Unexpected response while resolving output file.",
        )

    if resolved.get("ok") is False:
        return resolved

    selected = resolved.get("selected")
    if not selected:
        return _error_payload(
            "not_found",
            "No output file matched the selector.",
            dir=resolved.get("dir"),
            count=resolved.get("count", 0),
            selected=None,
        )

    selected_path = str((selected or {}).get("path") or "").strip()
    if not selected_path:
        return _error_payload(
            "not_found",
            "Resolved output file does not include a path.",
            dir=resolved.get("dir"),
            count=resolved.get("count", 0),
            selected=selected,
        )

    query_result = query_output_file(
        s3_url=selected_path,
        query=query,
    )

    if isinstance(query_result, dict):
        query_result.setdefault("dir", resolved.get("dir"))
        query_result.setdefault("count", resolved.get("count"))
        query_result.setdefault("selected", selected)

    return query_result

def create_plotly_chart_from_output_file(s3_url, query, title: Optional[str] = None) -> Dict:
    """Run a read-only DuckDB query against one NRDS output file in S3 (parquet or netcdf)
    and return Plotly chart JSON.
    """
    raw_url = str(s3_url or "").strip()
    kind = _detect_output_file_kind(raw_url)

    if kind == "parquet":
        err = _validate_nrds_output_file_url(BUCKET, raw_url, (".parquet",))
    elif kind == "netcdf":
        err = _validate_nrds_output_file_url(BUCKET, raw_url, (".nc", ".nc4"))
    else:
        err = "s3_url must point to one .parquet, .nc, or .nc4 NRDS output file"

    if err:
        return _error_payload(
            "validation_error",
            err,
            file=raw_url,
            query=query,
        )

    file_url = _normalize_output_file_url(raw_url)
    logger.info(
        "Received request to create Plotly chart from %s file: %s with query: %s",
        kind,
        file_url,
        query,
    )

    try:
        query = validate_output_sql(query)
    except ValueError as e:
        logger.error("Invalid SQL query for chart: %s", e)
        return _error_payload(
            "validation_error",
            str(e),
            file=file_url,
            file_type=kind,
            query=query,
        )

    try:
        if kind == "parquet":
            df = _duckdb_query_parquet(file_url, query)
        else:
            initial_df = _get_troute_df(file_url)
            logger.info(
                "Initial NetCDF DataFrame loaded with %s rows and columns: %s",
                len(initial_df),
                initial_df.columns.tolist(),
            )
            df = _duckdb_query_netcdf(initial_df, query)

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        logger.info("Chart query returned %s rows and columns: %s", len(df), df.columns.tolist())
        fig = _create_plotly_chart(df=df, title=title)

        if isinstance(fig, dict) and fig.get("error"):
            return _error_payload(
                "validation_error",
                fig["error"],
                file=file_url,
                file_type=kind,
                query=query,
                columns=list(df.columns),
                rows=int(len(df)),
            )

        return _success_payload(
            file=file_url,
            file_type=kind,
            query=query,
            columns=list(df.columns),
            rows=int(len(df)),
            figure=fig,
        )

    except FileNotFoundError:
        logger.error("File not found: %s", file_url)
        return _error_payload(
            "not_found",
            f"File not found: {file_url}",
            file=file_url,
            file_type=kind,
            query=query,
            columns=[],
            rows=0,
        )
    except Exception as e:
        logger.error("Error creating chart from %s file: %s", kind, e)
        return _error_payload(
            "execution_error",
            str(e),
            file=file_url,
            file_type=kind,
            query=query,
        )

def create_plotly_chart_from_parquet_output_file(s3_url, query, title: Optional[str] = None) -> Dict:
    """Backward-compatible parquet-only chart wrapper."""
    raw_url = str(s3_url or "").strip()
    err = _validate_nrds_output_file_url(BUCKET, raw_url, (".parquet",))
    if err:
        return _error_payload(
            "validation_error",
            err,
            file=raw_url,
            query=query,
        )

    return create_plotly_chart_from_output_file(
        s3_url=raw_url,
        query=query,
        title=title,
    )

def create_plotly_chart_from_output_selector(
    model,
    date,
    forecast,
    cycle,
    vpu,
    query,
    title: Optional[str] = None,
    ensemble: Optional[str] = None,
    file_name: Optional[str] = None,
    index: Optional[int] = 0,
) -> Dict:
    """Resolve an output file by selector and create a Plotly chart from the selected parquet or netcdf file."""
    logger.info(
        "Received request to create Plotly chart from selector with "
        "model=%s date=%s forecast=%s cycle=%s vpu=%s ensemble=%s file_name=%s index=%s title=%s query=%s",
        model,
        date,
        forecast,
        cycle,
        vpu,
        ensemble,
        file_name,
        index,
        title,
        query,
    )

    resolved = get_output_file(
        model=model,
        date=date,
        forecast=forecast,
        cycle=cycle,
        vpu=vpu,
        file_name=file_name,
        index=None if file_name is not None else (0 if index is None else index),
        ensemble=ensemble,
    )

    if not isinstance(resolved, dict):
        return _error_payload(
            "execution_error",
            "Unexpected response while resolving output file.",
        )

    if resolved.get("ok") is False:
        return resolved

    selected = resolved.get("selected")
    if not selected:
        return _error_payload(
            "not_found",
            "No output file matched the selector.",
            dir=resolved.get("dir"),
            count=resolved.get("count", 0),
            selected=None,
        )

    selected_path = str((selected or {}).get("path") or "").strip()
    if not selected_path:
        return _error_payload(
            "not_found",
            "Resolved output file does not include a path.",
            dir=resolved.get("dir"),
            count=resolved.get("count", 0),
            selected=selected,
        )

    chart_result = create_plotly_chart_from_output_file(
        s3_url=selected_path,
        query=query,
        title=title,
    )

    if isinstance(chart_result, dict):
        chart_result.setdefault("dir", resolved.get("dir"))
        chart_result.setdefault("count", resolved.get("count"))
        chart_result.setdefault("selected", selected)

    return chart_result

def query_hydrofabric_parquet_file(hydrofabric_id: str, limit: int = 50) -> Dict:
    """Run a hydrofabric id lookup against the hydrofabric parquet file on S3."""
    logger.info(
        "Received request to query hydrofabric with hydrofabric_id=%s limit=%s",
        hydrofabric_id,
        limit,
    )

    hydrofabric_id = (hydrofabric_id or "").strip()
    if not hydrofabric_id:
        return _error_payload(
            "bad_request",
            "hydrofabric_id is required",
            file=HYDROFABRIC_INDEX_URL,
            hydrofabric_id=hydrofabric_id,
            columns=[],
            rows=0,
            data=[],
        )

    try:
        df = _duckdb_query_hydrofabric_parquet(hydrofabric_id=hydrofabric_id, limit=limit)
        logger.info(
            "Hydrofabric query returned %s rows and columns: %s",
            len(df),
            df.columns.tolist(),
        )
        return _success_payload(
            file=HYDROFABRIC_INDEX_URL,
            hydrofabric_id=hydrofabric_id,
            columns=list(df.columns),
            rows=int(len(df)),
            data=df.to_dict(orient="records"),
        )
    except FileNotFoundError:
        logger.error("File not found: %s", HYDROFABRIC_INDEX_URL)
        return _error_payload(
            "not_found",
            f"File not found: {HYDROFABRIC_INDEX_URL}",
            file=HYDROFABRIC_INDEX_URL,
            hydrofabric_id=hydrofabric_id,
            columns=[],
            rows=0,
            data=[],
        )
    except Exception as e:
        logger.error("Error querying hydrofabric parquet file: %s", e)
        return _error_payload(
            "execution_error",
            str(e),
            file=HYDROFABRIC_INDEX_URL,
            hydrofabric_id=hydrofabric_id,
        )


def build_hydrofabric_feature_map_config(hydrofabric_id: str) -> Dict[str, Any]:
    hydrofabric_id = (hydrofabric_id or "").strip()
    if not hydrofabric_id:
        return _error_payload(
            "bad_request",
            "hydrofabric_id is required",
            type="hydrofabric_feature_map_config",
        )

    try:
        df = _duckdb_lookup_hydrofabric_feature(hydrofabric_id)
        if df.empty:
            return _success_payload(
                type="hydrofabric_feature_map_config",
                query=hydrofabric_id,
                found=False,
                feature=None,
                match=None,
                highlight=None,
                camera=None,
            )

        row = _normalize_record(df.iloc[0].to_dict())
        layer_key = str(row.get("layer") or "").strip().lower()

        layer_cfg = HYDROFABRIC_LAYER_CONFIG.get(layer_key)
        if not layer_cfg:
            return _error_payload(
                "unsupported_layer",
                f"Unsupported hydrofabric layer '{layer_key}'",
                type="hydrofabric_feature_map_config",
                query=hydrofabric_id,
                found=True,
                feature=row,
            )

        center = _get_feature_center(row)
        id_property = layer_cfg["id_property"]
        filter_value = _pick_filter_value(row, id_property, hydrofabric_id)

        return _success_payload(
            type="hydrofabric_feature_map_config",
            query=hydrofabric_id,
            found=True,
            feature=row,
            match={
                "matched_column": row.get("matched_column"),
                "match_type": row.get("match_type"),
            },
            highlight={
                "pmtiles_url": layer_cfg["pmtiles_url"],
                "map_layer_id": layer_cfg["map_layer_id"],
                "id_property": id_property,
                "value": filter_value,
                "layer_key": layer_key,
            },
            camera={
                "mode": "rendered-feature-bounds-with-fallback",
                "center": center,
                "zoom": layer_cfg["default_zoom"],
                "padding": 40,
                "maxZoom": 13,
            },
        )

    except Exception as e:
        logger.exception("Error building hydrofabric map config")
        return _error_payload(
            "execution_error",
            str(e),
            type="hydrofabric_feature_map_config",
            query=hydrofabric_id,
        )