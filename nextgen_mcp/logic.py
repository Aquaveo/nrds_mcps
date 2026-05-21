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
from .validation import OutputsFilesQuery
from pydantic import ValidationError
from .utils import _require
from .utils_rest import (
    _extract_yyyymmdd_from_date_folder,
    _label_from_id,
    _normalize_date_folder,
    _duckdb_lookup_hydrofabric_feature,
    _normalize_record,
    _get_feature_center,
    HYDROFABRIC_LAYER_CONFIG,
    validate_output_sql,
    _success_payload,
    _error_payload,
    _list_payload,
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

        files = [f for f in files if f.lower().endswith(".parquet")]
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



_NETCDF_EXTS = (".nc", ".nc4")


def _resolve_parquet_files_for_query(
    model,
    date,
    forecast,
    cycle,
    vpu,
    ensemble: Optional[str] = None,
    file_name: Optional[str] = None,
    index: Optional[int] = None,
) -> Dict[str, Any]:
    """Resolve a list of parquet S3 URLs from selector args.

    Three branches:
      - (file_name=None, index=None): list all parquet files for the selector.
      - (file_name set,  index=None): filter to one file by exact name.
      - (file_name=None, index set):  filter to one file by 0-based index
        into the parquet-only, sorted list. NetCDF files do NOT consume
        index slots — `index=N` always refers to the N-th parquet file.

    The (file_name, index) BOTH-set case is rejected by the caller's
    Pydantic model_validator before reaching here. We don't re-check.

    Returns one of:
      {"ok": True, "urls": [...], "s3_dir": ..., "excluded_netcdf_count": int}
      or an error envelope (ok=False) ready to return to the caller.
    """
    date_folder = _normalize_date_folder(date)
    s3_dir = (
        f"s3://{BUCKET}/{OUTPUTS_DIR}/{model}/{PREFIX_HYDROFABRIC}/"
        f"{date_folder}/{forecast}/{cycle}"
    )
    if forecast == "medium_range":
        ens = ensemble or "1"
        s3_dir += f"/{ens}/{vpu}/{NGEN_RUN_PREFIX}"
    else:
        s3_dir += f"/{vpu}/{NGEN_RUN_PREFIX}"

    # file_name path: check extension locally BEFORE any S3 I/O.
    # Cheap short-circuit for the common LLM mistake of pointing at .nc/.nc4.
    if file_name is not None:
        lower = file_name.lower()
        if lower.endswith(_NETCDF_EXTS):
            return _error_payload(
                "unsupported_format",
                "NetCDF (.nc/.nc4) files are not supported by this server.",
                fix_hint=(
                    "This server queries parquet files only. NetCDF outputs "
                    "are available in S3 - download with netCDF-aware tooling "
                    "(xarray, h5netcdf) and query locally."
                ),
                format_detected="netcdf",
                file_name=file_name,
            )

    # List S3 for both no-filter and index/file_name paths.
    try:
        fs = s3_filesystem()
        listing = fs.ls(s3_dir, detail=False)
    except FileNotFoundError:
        return _error_payload(
            "not_found",
            "No output files matched the selector.",
            dir=s3_dir,
            count=0,
        )
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error("IO error %s listing %s: %s", type(e).__name__, s3_dir, e)
        return _error_payload(code, msg, fix_hint=fix_hint, dir=s3_dir)

    listing_sorted = sorted(listing)
    items_all = [
        {"name": f.split("/")[-1], "path": _ensure_full_s3_url(f)}
        for f in listing_sorted
    ]
    parquet_items = [
        it for it in items_all if it["name"].lower().endswith(".parquet")
    ]
    netcdf_items = [
        it for it in items_all
        if it["name"].lower().endswith(_NETCDF_EXTS)
    ]

    # no_supported_files: selector resolved to N files, none parquet.
    if not parquet_items:
        return _error_payload(
            "no_supported_files",
            (
                f"Selector resolved to {len(items_all)} files, none parquet. "
                "This server queries parquet only."
            ),
            fix_hint=(
                "Check whether parquet outputs exist for this selector. If "
                "only NetCDF is available, download from S3 with netCDF-aware "
                "tooling."
            ),
            dir=s3_dir,
            files_found=len(items_all),
            netcdf_files=len(netcdf_items),
            parquet_files=0,
        )

    # file_name branch: exact lookup against the parquet-filtered list.
    # (We already short-circuited .nc/.nc4 above; getting here means the
    # caller wants a parquet file. If the name doesn't match anything, it's
    # genuinely not found.)
    if file_name is not None:
        match = next(
            (it for it in parquet_items if it["name"] == file_name),
            None,
        )
        if match is None:
            return _error_payload(
                "not_found",
                f"file_name not found in selector: {file_name}",
                fix_hint=(
                    "Call list_available_output_files with the same selector "
                    "to see valid file names."
                ),
                dir=s3_dir,
                file_name=file_name,
                files_found=len(items_all),
                parquet_files=len(parquet_items),
            )
        return {
            "ok": True,
            "urls": [match["path"]],
            "s3_dir": s3_dir,
            "excluded_netcdf_count": len(netcdf_items),
        }

    # index branch: parquet-only semantics.
    if index is not None:
        if index >= len(parquet_items):
            return _error_payload(
                "invalid_args",
                f"index {index} out of range; selector has {len(parquet_items)} parquet files.",
                fix_hint=(
                    "Use an index in [0, parquet_files - 1] or call "
                    "list_available_output_files to see the file list."
                ),
                dir=s3_dir,
                index=index,
                files_found=len(parquet_items),
                parquet_files=len(parquet_items),
            )
        return {
            "ok": True,
            "urls": [parquet_items[index]["path"]],
            "s3_dir": s3_dir,
            "excluded_netcdf_count": len(netcdf_items),
        }

    # No-filter branch: query all parquet files.
    return {
        "ok": True,
        "urls": [it["path"] for it in parquet_items],
        "s3_dir": s3_dir,
        "excluded_netcdf_count": len(netcdf_items),
    }


def query_files_by_selector(
    model,
    date,
    forecast,
    cycle,
    vpu,
    query,
    ensemble: Optional[str] = None,
    file_name: Optional[str] = None,
    index: Optional[int] = None,
) -> Dict[str, Any]:
    """Unified query tool for NRDS parquet outputs by selector.

    Single-file filter (via file_name or index) and no-filter (all parquet
    files for the selector) share one resolution path; both end up calling
    _duckdb_query_parquets with a list of 1+ URLs and a unified result-
    envelope shape.

    file_name XOR index: this logic-layer function normalizes/strips
    file_name and enforces the XOR so the contract holds even when invoked
    outside the MCP tool wrapper.
    """
    from .utils_rest import _duckdb_query_parquets

    # Normalize file_name: strip whitespace; treat empty-after-strip as None
    # so we behave the same as "no file_name set" rather than a vacuous match.
    if isinstance(file_name, str):
        file_name = file_name.strip()
        if not file_name:
            return _error_payload(
                "invalid_args",
                "file_name must be a non-empty string or omitted.",
                fix_hint=(
                    "Omit file_name to query all parquet files for the "
                    "selector, or provide an exact filename."
                ),
            )

    # Negative-index defense-in-depth (Pydantic ge=0 also catches this).
    if index is not None and index < 0:
        return _error_payload(
            "invalid_args",
            f"index must be >= 0; got {index}.",
            fix_hint="Use a non-negative index, or omit index to query all files.",
        )

    # XOR — both file_name and index set is invalid.
    if file_name is not None and index is not None:
        return _error_payload(
            "invalid_args",
            "Provide file_name OR index, not both.",
            fix_hint=(
                "Pick one filter mode: file_name for an exact match, or "
                "index for the N-th parquet file (0-based)."
            ),
        )

    err = _require(model=model, forecast=forecast, vpu=vpu)
    if err:
        # _require returns a non-standard {"error": "<message>"} shape; wrap
        # into the standard _error_payload envelope so callers get a
        # consistent invalid_args: response.
        return _error_payload(
            "invalid_args",
            err["error"],
            fix_hint=(
                "Call list_available_models / list_available_forecasts / "
                "list_available_vpus to discover valid selector values."
            ),
        )

    logger.info(
        "Received request to query_files_by_selector with "
        "model=%s date=%s forecast=%s cycle=%s vpu=%s ensemble=%s "
        "file_name=%s index=%s query=%s",
        model, date, forecast, cycle, vpu, ensemble, file_name, index, query,
    )

    resolved = _resolve_parquet_files_for_query(
        model=model,
        date=date,
        forecast=forecast,
        cycle=cycle,
        vpu=vpu,
        ensemble=ensemble,
        file_name=file_name,
        index=index,
    )

    if not resolved.get("ok"):
        return resolved

    file_urls = resolved["urls"]
    s3_dir = resolved["s3_dir"]
    excluded_netcdf_count = resolved["excluded_netcdf_count"]

    try:
        query = validate_output_sql(query)
    except ValueError as e:
        return _error_payload(
            "validation_error",
            str(e),
            dir=s3_dir,
            file_count=len(file_urls),
            query=query,
        )

    try:
        df = _duckdb_query_parquets(file_urls, query)

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )

        payload = _success_payload(
            dir=s3_dir,
            file_count=len(file_urls),
            file_type="parquet",
            query=query,
            columns=list(df.columns),
            rows=int(len(df)),
            data=df.to_dict(orient="records"),
        )

        # Surface the exclusion count when non-zero so the user/LLM has a
        # signal that NetCDF files were dropped. Omit when zero to keep the
        # common-case envelope lean.
        if excluded_netcdf_count > 0:
            payload["_excluded_netcdf_count"] = excluded_netcdf_count

        return payload

    except (duckdb.BinderException, duckdb.ParserException, duckdb.CatalogException) as e:
        code, msg, fix_hint, available_columns = _classify_llm_sql_error(
            e, file_urls[0], query
        )
        logger.warning(
            "LLM SQL error %s across %s parquet files in %s: %s",
            type(e).__name__, len(file_urls), s3_dir, e,
        )
        return _error_payload(
            code, msg,
            fix_hint=fix_hint,
            dir=s3_dir,
            file_count=len(file_urls),
            file_type="parquet",
            query=query,
            available_columns=available_columns,
        )
    except (OSError, ClientError, duckdb.Error) as e:
        if _is_duckdb_programmer_error(e):
            raise
        code, msg, fix_hint = _classify_io_error(e)
        logger.error(
            "IO error %s querying %s parquet files in %s: %s",
            type(e).__name__, len(file_urls), s3_dir, e,
        )
        return _error_payload(
            code, msg,
            fix_hint=fix_hint,
            dir=s3_dir,
            file_count=len(file_urls),
            file_type="parquet",
            query=query,
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


def lookup_hydrofabric_feature(
    hydrofabric_id: str, limit: int = 1
) -> Dict[str, Any]:
    """Look up hydrofabric features by id and return data only.

    Returns a flat shape:
      - rows: list of matching rows from the hydrofabric index (up to ``limit``)
      - pmtiles_layer: the PMTiles layer name associated with the top match, or None
      - bbox: [minLon, minLat, maxLon, maxLat] bounding the top match, or None

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
        df = _duckdb_lookup_hydrofabric_feature(hydrofabric_id, limit=limit)
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
    

def get_hydrofabric_pmtiles_layers() -> Dict[str, Any]:
    """Return the list of hydrofabric layers and their associated PMTiles layer names."""
    try:
        layers = []
        for layer_key, cfg in HYDROFABRIC_LAYER_CONFIG.items():
            layers.append({
                "id": layer_key,
                "map_layer_id": cfg.get("map_layer_id"),
                "url": cfg.get("pmtiles_url"),
                "id_property": cfg.get("id_property"),
            })
        return _success_payload(layers=layers)
    except Exception as e:
        logger.error("Error getting hydrofabric layers: %s", e)
        return _error_payload(
            "execution_error",
            "Unexpected error getting hydrofabric layers.",
            details=str(e),
        )