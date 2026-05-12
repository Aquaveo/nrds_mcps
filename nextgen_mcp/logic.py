"""
file logic.py

Description: Implements the core logic for listing and querying NRDS output files on S3, as well as looking up hydrofabric features. 
This is the main "M" in MCP, and is called

"""

import os
import json
import logging
import pandas as pd

from ._io_config import HYDROFABRIC_INDEX_URL, s3_filesystem
import duckdb
from botocore.exceptions import ClientError

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
    HYDROFABRIC_LAYER_CONFIG,
    validate_output_sql,
    _success_payload,
    _error_payload,
    _list_payload,
    _validate_nrds_output_file_url,
    _detect_output_file_kind,
    _normalize_output_file_url,
    _classify_io_error,
    _is_duckdb_programmer_error,
    _classify_llm_sql_error,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BUCKET = os.getenv("BUCKET", "ciroh-community-ngen-datastream")
OUTPUTS_DIR = "outputs"
PREFIX_HYDROFABRIC = "v2.2_hydrofabric"
NGEN_RUN_PREFIX = "ngen-run/outputs/troute"


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
        fs = s3_filesystem()
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
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_url, e)
        return _error_payload(code, msg, fix_hint=fix_hint, path=s3_url)


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

    # Validation-before-IO: fail fast on obviously-bad index BEFORE the S3
    # round-trip. The upper-bound check still happens after fs.ls (requires
    # len(items)) but the lower-bound is cheap and bounded.
    if index is not None:
        try:
            _idx_check = int(index)
        except (TypeError, ValueError):
            return _error_payload(
                "bad_request",
                "index must be an integer",
            )
        if _idx_check < 0:
            return _error_payload(
                "bad_request",
                f"index out of range: {_idx_check}",
            )

    date = _normalize_date_folder(date)
    s3_dir = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/{forecast}/{cycle}"
    if forecast == "medium_range":
        ens = ensemble or "1"
        s3_dir += f"/{ens}/{vpu}/{NGEN_RUN_PREFIX}"
    else:
        s3_dir += f"/{vpu}/{NGEN_RUN_PREFIX}"

    try:
        fs = s3_filesystem()
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
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_dir, e)
        return _error_payload(code, msg, fix_hint=fix_hint, dir=s3_dir)


def list_available_vpus(model, date, forecast, cycle) -> Dict:
    """List VPUs for a given model, date, forecast, and cycle."""
    logger.info(f"Listing VPUs for model={model}, date={date}, forecast={forecast}, cycle={cycle}")
    date = _normalize_date_folder(date)
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/{forecast}/{cycle}"
    if forecast == "medium_range":
        s3_url += "/1"

    try:
        fs = s3_filesystem()
        dirs = fs.ls(s3_url, detail=False)

        vpu_ids = sorted(d.split("/")[-1] for d in dirs)
        vpus = [{"id": vpu_id, "label": _label_from_id(vpu_id)} for vpu_id in vpu_ids]

        logger.info(f"Found VPUs at {s3_url}: {[v['label'] for v in vpus]}")
        return _list_payload("vpus", vpus, path=s3_url)
    except FileNotFoundError:
        logger.info(f"No VPUs found at {s3_url}")
        return _list_payload("vpus", [], path=s3_url)
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_url, e)
        return _error_payload(code, msg, fix_hint=fix_hint, path=s3_url)


def list_available_cycles(model, date, forecast) -> Dict:
    """List available cycles for a given model, date, and forecast."""
    logger.info(f"Listing cycles for model={model}, date={date}, forecast={forecast}")
    date = _normalize_date_folder(date)
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/{forecast}/"

    try:
        fs = s3_filesystem()
        dirs = fs.ls(s3_url, detail=False)

        cycle_ids = [d.split("/")[-1] for d in dirs]
        cycles = [{"id": c, "label": c} for c in cycle_ids]
        logger.info(f"Found cycles at {s3_url}: {cycle_ids}")
        return _list_payload("cycles", cycles, path=s3_url)

    except FileNotFoundError:
        logger.info(f"No cycles found at {s3_url}")
        return _list_payload("cycles", [], path=s3_url)
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_url, e)
        return _error_payload(code, msg, fix_hint=fix_hint, path=s3_url)


def list_available_dates(model) -> Dict:
    """List available dates for a given model."""
    logger.info(f"Listing dates for model={model}")
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}"

    try:
        fs = s3_filesystem()
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
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_url, e)
        return _error_payload(code, msg, fix_hint=fix_hint, path=s3_url)


def list_available_forecasts(model, date) -> Dict:
    """List available forecasts for a given model, date."""
    logger.info(f"Listing forecasts for model={model}, date={date}")
    date = _normalize_date_folder(date)
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/{date}/"
    try:
        fs = s3_filesystem()
        dirs = fs.ls(s3_url, detail=False)

        forecast_ids = [d.split("/")[-1] for d in dirs]
        forecast_labels = [_label_from_id(f) for f in forecast_ids]

        forecasts = [{"id": fid, "label": lbl} for fid, lbl in zip(forecast_ids, forecast_labels)]
        logger.info(f"Found forecasts at {s3_url}: {forecast_labels}")
        return _list_payload("forecasts", forecasts, path=s3_url)
    except FileNotFoundError:
        logger.info(f"No forecasts found at {s3_url}")
        return _list_payload("forecasts", [], path=s3_url)
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_url, e)
        return _error_payload(code, msg, fix_hint=fix_hint, path=s3_url)


def list_available_models() -> Dict:
    logger.info(f"Listing available models in bucket={BUCKET} under {OUTPUTS_DIR}")
    s3_url = f"s3://{BUCKET}/{OUTPUTS_DIR}"
    fs = s3_filesystem()
    try:
        dirs = fs.ls(s3_url, detail=False)
        model_ids = [d.split("/")[-1] for d in dirs]
        logger.info(f"Found models at {s3_url}: {model_ids}")
        models = [{"id": mid, "label": mid} for mid in model_ids]
        return _list_payload("models", models, path=s3_url)
    except FileNotFoundError:
        logger.info(f"No models found at {s3_url}")
        return _list_payload("models", [], path=s3_url)
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s on %s: %s", type(e).__name__, s3_url, e)
        return _error_payload(code, msg, fix_hint=fix_hint, path=s3_url)


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

    except (duckdb.BinderException, duckdb.ParserException, duckdb.CatalogException) as e:
        # LLM-supplied SQL programmer error. Unlike the hardcoded-SQL
        # paths (which re-raise via _is_duckdb_programmer_error), these
        # errors are RECOVERABLE if the LLM gets a structured envelope
        # with the available column list as fix_hint — same shape as the
        # input-validation middleware's invalid_args response. Observed
        # 2026-05-10: qwen stalled on a BinderException when this re-raised
        # as a protocol error; with the structured envelope below the
        # LLM has the actual column candidates and can retry in one turn.
        code, msg, fix_hint, available_columns = _classify_llm_sql_error(
            e, file_url, query
        )
        logger.warning(
            "LLM SQL error %s on %s file %s: %s",
            type(e).__name__,
            kind,
            file_url,
            e,
        )
        return _error_payload(
            code,
            msg,
            fix_hint=fix_hint,
            file=file_url,
            file_type=kind,
            query=query,
            available_columns=available_columns,
        )
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error(
            "IO error %s querying %s file %s: %s",
            type(e).__name__,
            kind,
            file_url,
            e,
        )
        # not_found preserves the empty-result shape that callers expect
        if code == "not_found":
            return _error_payload(
                code,
                msg,
                fix_hint=fix_hint,
                file=file_url,
                file_type=kind,
                query=query,
                columns=[],
                rows=0,
                data=[],
            )
        return _error_payload(
            code,
            msg,
            fix_hint=fix_hint,
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
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error(
            "IO error %s on hydrofabric parquet %s: %s",
            type(e).__name__,
            HYDROFABRIC_INDEX_URL,
            e,
        )
        if code == "not_found":
            return _error_payload(
                code,
                msg,
                fix_hint=fix_hint,
                file=HYDROFABRIC_INDEX_URL,
                hydrofabric_id=hydrofabric_id,
                columns=[],
                rows=0,
                data=[],
            )
        return _error_payload(
            code,
            msg,
            fix_hint=fix_hint,
            file=HYDROFABRIC_INDEX_URL,
            hydrofabric_id=hydrofabric_id,
        )


def _bbox_from_row(row: Dict[str, Any]) -> Optional[List[float]]:
    """Compute a [minLon, minLat, maxLon, maxLat] bbox from a hydrofabric row.

    Tries explicit min/max columns first, then falls back to a degenerate
    bbox (a single point) when only a center coordinate is available.
    Returns None if no usable spatial information is on the row.
    """
    keys = ("minx", "miny", "maxx", "maxy")
    if all(row.get(k) is not None for k in keys):
        try:
            return [
                float(row["minx"]),
                float(row["miny"]),
                float(row["maxx"]),
                float(row["maxy"]),
            ]
        except (TypeError, ValueError):
            pass

    center = _get_feature_center(row)
    if center is not None:
        lon, lat = center
        return [float(lon), float(lat), float(lon), float(lat)]

    return None


def lookup_hydrofabric_feature(hydrofabric_id: str) -> Dict[str, Any]:
    """Look up one hydrofabric feature by id and return data only.

    Returns a flat shape:
      - rows: list of matching rows from the hydrofabric index
      - pmtiles_layer: the PMTiles layer name associated with the feature, or None
      - bbox: [minLon, minLat, maxLon, maxLat] bounding the feature, or None

    On empty match: rows=[], pmtiles_layer=None, bbox=None.
    The host is responsible for assembling any map view from this data.
    """
    hydrofabric_id = (hydrofabric_id or "").strip()
    if not hydrofabric_id:
        return _error_payload(
            "bad_request",
            "hydrofabric_id is required",
            rows=[],
            pmtiles_layer=None,
            bbox=None,
        )

    try:
        df = _duckdb_lookup_hydrofabric_feature(hydrofabric_id)
        if df.empty:
            return _success_payload(
                rows=[],
                pmtiles_layer=None,
                bbox=None,
            )

        rows = [_normalize_record(r) for r in df.to_dict(orient="records")]
        first = rows[0]
        layer_key = str(first.get("layer") or "").strip().lower()
        layer_cfg = HYDROFABRIC_LAYER_CONFIG.get(layer_key)
        pmtiles_layer = layer_cfg["map_layer_id"] if layer_cfg else None
        bbox = _bbox_from_row(first)

        return _success_payload(
            rows=rows,
            pmtiles_layer=pmtiles_layer,
            bbox=bbox,
        )

    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error(
            "IO error %s looking up hydrofabric feature %s: %s",
            type(e).__name__,
            hydrofabric_id,
            e,
        )
        return _error_payload(
            code,
            msg,
            fix_hint=fix_hint,
            rows=[],
            pmtiles_layer=None,
            bbox=None,
        )