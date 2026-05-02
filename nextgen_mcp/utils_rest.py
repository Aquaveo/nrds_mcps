#nextgen_plugins/chatbox/utils_rest.py
from datetime import datetime
from typing import Any, Dict, List, Optional
import json
import ast
import re   
import pandas as pd
import duckdb
import xarray as xr
from shapely import wkb, wkt
from shapely.geometry import shape
import plotly.express as px
from plotly.utils import PlotlyJSONEncoder

HYDROFABRIC_INDEX_URL = (
    "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/hydrofabric_index.parquet"
)
_OUTPUT_SQL_START_RE = re.compile(r"(?is)^\s*(?:WITH\b.*?\bSELECT\b|SELECT\b)")
_OUTPUT_SQL_FROM_OUTPUT_RE = re.compile(r"(?is)\bFROM\s+output\b")
_OUTPUT_SQL_FORBIDDEN_RE = re.compile(
    r"(?is)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|COPY|ATTACH|DETACH|CALL|PRAGMA|VACUUM|TRUNCATE|MERGE|REPLACE)\b"
)

HYDROFABRIC_LAYER_CONFIG = {
    "flowpaths": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/flowpaths.pmtiles",
        "map_layer_id": "flowpaths",
        "id_property": "id",
        "default_zoom": 12,
    },
    "gage": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/gage.pmtiles",
        "map_layer_id": "conus-gauges",
        "id_property": "id",
        "default_zoom": 12,
    },
    "divides": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/divides.pmtiles",
        "map_layer_id": "divides",
        "id_property": "divide_id",
        "default_zoom": 10,
    },
    "hydrolocations": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/hydrolocations.pmtiles",
        "map_layer_id": "nexus-points",
        "id_property": "id",
        "default_zoom": 12,
    },
}

def _normalize_output_file_url(s3_url: str) -> str:
    file_url = str(s3_url or "").strip()
    if file_url.startswith("s3://ciroh-community-ngen-datastream"):
        file_url = file_url.replace(
            "s3://ciroh-community-ngen-datastream",
            "https://ciroh-community-ngen-datastream.s3.us-east-1.amazonaws.com",
        )
    return file_url


def _detect_output_file_kind(file_url: str) -> Optional[str]:
    lower = str(file_url or "").lower()
    if lower.endswith(".parquet"):
        return "parquet"
    if lower.endswith(".nc") or lower.endswith(".nc4"):
        return "netcdf"
    return None

def _validate_nrds_output_file_url(bucket: str, file_url: str, allowed_exts: tuple[str, ...]) -> Optional[str]:
    url = str(file_url or "").strip()
    if not url:
        return "Missing required query param: s3_url"

    lower = url.lower()

    if not lower.endswith(allowed_exts):
        return f"s3_url must point to one file ending in {', '.join(allowed_exts)}"

    allowed_prefixes = (
        f"s3://{bucket.lower()}/",
        f"https://{bucket.lower()}.s3.us-east-1.amazonaws.com/",
    )
    if not any(lower.startswith(prefix) for prefix in allowed_prefixes):
        return f"s3_url must point to bucket {bucket}"

    if "/outputs/" not in lower:
        return "s3_url must point to an NRDS outputs file under /outputs/"

    return None

def _success_payload(**kwargs) -> Dict[str, Any]:
    return {
        "ok": True,
        "error": None,
        **kwargs,
    }

def _error_payload(
    code: str,
    message: str,
    *,
    details: Any = None,
    **kwargs,
) -> Dict[str, Any]:
    error_obj = {
        "code": code,
        "message": message,
    }
    if details is not None:
        error_obj["details"] = details

    return {
        "ok": False,
        "error": error_obj,
        **kwargs,
    }

def _list_payload(key: str, items: list, *, path: str) -> Dict[str, Any]:
    return _success_payload(
        path=path,
        count=len(items),
        **{key: items},
    )

def _create_plotly_chart(
    df: pd.DataFrame,
    title: str,
) -> Dict[str, Any]:

    columns = df.columns.tolist()
    if len(columns) < 2:
        return {
            "error": (
                "Chart query must return at least two columns, including "
                "'time' and one metric column such as flow, velocity, depth, or nudge."
            )
        }

    if "time" not in columns:
        return {
            "error": (
                "Chart query must return a 'time' column and one metric column "
                "such as flow, velocity, depth, or nudge."
            )
        }

    try:
        x, y = _auto_pick_axes(columns)
    except ValueError as e:
        return {
            "error": (
                "Chart query must return a 'time' column and at least one additional "
                "metric column such as flow, velocity, depth, or nudge."
            )
        }

    fig = px.line(
        df,
        x=x,
        y=y,
        title=title,
    )

    fig.update_layout(template="plotly_white")
    return json.loads(json.dumps(fig.to_plotly_json(), cls=PlotlyJSONEncoder))

def validate_output_sql(query: str) -> str:
    """
    Validate a DuckDB query for output-file tools.

    Rules:
      - must be a single statement
      - must start with SELECT or WITH ... SELECT
      - must be read-only
      - must read FROM output
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")

    q = query.strip()

    # Allow one trailing semicolon, but reject multi-statement SQL
    q_no_trailing = q[:-1].strip() if q.endswith(";") else q
    if ";" in q_no_trailing:
        raise ValueError("query must be a single SQL statement")

    if not _OUTPUT_SQL_START_RE.match(q):
        raise ValueError("query must start with SELECT or WITH")

    if _OUTPUT_SQL_FORBIDDEN_RE.search(q):
        raise ValueError("query must be read-only")

    if not _OUTPUT_SQL_FROM_OUTPUT_RE.search(q):
        raise ValueError("query must read FROM output")

    return q

def _normalize_record(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (None if pd.isna(v) else v) for k, v in row.items()}

def _get_feature_center(row: Dict[str, Any]) -> Optional[list]:
    lon = row.get("lon")
    lat = row.get("lat")

    if lon is not None and lat is not None:
        return [float(lon), float(lat)]

    lake_x = row.get("lake_x")
    lake_y = row.get("lake_y")
    if lake_x is not None and lake_y is not None:
        return [float(lake_x), float(lake_y)]

    return None

def _pick_filter_value(row: Dict[str, Any], id_property: str, requested_id: str) -> str:
    value = row.get(id_property)
    if value not in (None, ""):
        return str(value)

    if row.get("divide_id") not in (None, ""):
        return str(row["divide_id"])

    if row.get("id") not in (None, ""):
        return str(row["id"])

    return str(requested_id)

def _duckdb_lookup_hydrofabric_feature(hydrofabric_id: str) -> pd.DataFrame:
    con = duckdb.connect(database=":memory:")
    try:
        try:
            con.execute("LOAD httpfs")
        except Exception:
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")

        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW output AS
            SELECT *
            FROM read_parquet('{HYDROFABRIC_INDEX_URL}')
            """
        )

        sql = """
        WITH matches AS (
            SELECT
                *,
                CASE
                    WHEN lower(coalesce(id, '')) = lower(?) THEN 0
                    WHEN lower(coalesce(divide_id, '')) = lower(?) THEN 1
                    WHEN lower(coalesce(id, '')) LIKE '%' || lower(?) || '%' THEN 2
                    WHEN lower(coalesce(divide_id, '')) LIKE '%' || lower(?) || '%' THEN 3
                    ELSE 4
                END AS match_rank,
                CASE
                    WHEN lower(coalesce(id, '')) = lower(?) THEN 'id'
                    WHEN lower(coalesce(divide_id, '')) = lower(?) THEN 'divide_id'
                    WHEN lower(coalesce(id, '')) LIKE '%' || lower(?) || '%' THEN 'id'
                    WHEN lower(coalesce(divide_id, '')) LIKE '%' || lower(?) || '%' THEN 'divide_id'
                    ELSE NULL
                END AS matched_column,
                CASE
                    WHEN lower(coalesce(id, '')) = lower(?) THEN 'exact'
                    WHEN lower(coalesce(divide_id, '')) = lower(?) THEN 'exact'
                    WHEN lower(coalesce(id, '')) LIKE '%' || lower(?) || '%' THEN 'substring'
                    WHEN lower(coalesce(divide_id, '')) LIKE '%' || lower(?) || '%' THEN 'substring'
                    ELSE NULL
                END AS match_type
            FROM output
        )
        SELECT * EXCLUDE (match_rank)
        FROM matches
        WHERE match_rank < 4
        ORDER BY match_rank, id, divide_id
        LIMIT 1
        """

        params = [hydrofabric_id] * 12
        return con.execute(sql, params).df()

    finally:
        try:
            con.close()
        except Exception:
            pass

def _load_geometry(value):
    if value is None:
        return None

    if isinstance(value, (bytes, bytearray, memoryview)):
        return wkb.loads(bytes(value))

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("{"):
            return shape(json.loads(text))
        return wkt.loads(text)

    return value

def _lookup_flowpath_view(feature_id: str) -> dict:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")

    row = con.execute(
        """
        SELECT *
        FROM read_parquet(?)
        WHERE id = ?
        LIMIT 1
        """,
        [HYDROFABRIC_INDEX_URL, feature_id],
    ).fetchone()

    if not row:
        return {"bbox": None, "center": None, "zoom": None}

    geom = _load_geometry(row[1])
    if geom is None:
        return {"bbox": None, "center": None, "zoom": None}

    minx, miny, maxx, maxy = geom.bounds
    center = [(minx + maxx) / 2.0, (miny + maxy) / 2.0]

    return {
        "bbox": [[minx, miny], [maxx, maxy]],
        "center": center,
        "zoom": 12,
    }

def _strip_markdown_code_fence(text: str) -> str:
    s = (text or "").strip()
    if not s.startswith("```"):
        return s

    lines = s.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()

def _extract_first_json_object(text: str) -> Dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None

def _parse_query_result_payload(query_result: Dict[str, Any] | str) -> Dict[str, Any]:
    if isinstance(query_result, dict):
        return query_result
    if isinstance(query_result, str):
        s = _strip_markdown_code_fence(query_result)
        if not s:
            raise ValueError("query_result string is empty.")

        try:
            payload = json.loads(s)
            if isinstance(payload, dict):
                return payload
        except Exception:
            payload = None

        payload = _extract_first_json_object(s)
        if isinstance(payload, dict):
            return payload

        try:
            payload = ast.literal_eval(s)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    raise ValueError("query_result must be a JSON object or JSON object string.")

def _auto_pick_axes(columns: List[str]) -> tuple[str, str]:
    if not columns:
        raise ValueError("No columns available to infer x/y axes.")

    picked_x = next((col for col in columns if "time" in col.lower()), None)
    picked_y = next((col for col in columns if col != picked_x and col != "feature_id"), None)
    if not picked_y:
        raise ValueError("Could not infer a y-axis column different from x-axis and 'feature_id'.")
    if not picked_x:
        raise ValueError("Could not infer an x-axis column.")
    return (picked_x, picked_y)

def _get_troute_df(s3_nc_url: str) -> pd.DataFrame:
    """Load the t-route crosswalk DataFrame."""

    nc_xarray = xr.open_dataset(
        s3_nc_url,
        engine="h5netcdf"
    )
    nc_df = nc_xarray.to_dataframe()
    nc_df = nc_df.reset_index()

    return nc_df

def _duckdb_query_hydrofabric_parquet(hydrofabric_id: str, limit: int = 50) -> pd.DataFrame:
    """Lookup hydrofabric rows by id/divide_id using exact and substring matching."""

    con = duckdb.connect(database=":memory:")
    try:
        try:
            con.execute("LOAD httpfs")
        except Exception:
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")

        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW output AS
            SELECT *
            FROM read_parquet('{HYDROFABRIC_INDEX_URL}')
            """
        )

        sql = """
        WITH matches AS (
            SELECT
                *,
                CASE
                    WHEN lower(coalesce(id, '')) = lower(?) THEN 0
                    WHEN lower(coalesce(divide_id, '')) = lower(?) THEN 1
                    WHEN lower(coalesce(id, '')) LIKE '%' || lower(?) || '%' THEN 2
                    WHEN lower(coalesce(divide_id, '')) LIKE '%' || lower(?) || '%' THEN 3
                    ELSE 4
                END AS match_rank,
                CASE
                    WHEN lower(coalesce(id, '')) = lower(?) THEN 'id'
                    WHEN lower(coalesce(divide_id, '')) = lower(?) THEN 'divide_id'
                    WHEN lower(coalesce(id, '')) LIKE '%' || lower(?) || '%' THEN 'id'
                    WHEN lower(coalesce(divide_id, '')) LIKE '%' || lower(?) || '%' THEN 'divide_id'
                    ELSE NULL
                END AS matched_column,
                CASE
                    WHEN lower(coalesce(id, '')) = lower(?) THEN 'exact'
                    WHEN lower(coalesce(divide_id, '')) = lower(?) THEN 'exact'
                    WHEN lower(coalesce(id, '')) LIKE '%' || lower(?) || '%' THEN 'substring'
                    WHEN lower(coalesce(divide_id, '')) LIKE '%' || lower(?) || '%' THEN 'substring'
                    ELSE NULL
                END AS match_type
            FROM output
        )
        SELECT * EXCLUDE (match_rank)
        FROM matches
        WHERE match_rank < 4
        ORDER BY match_rank, id, divide_id
        LIMIT ?
        """

        params = [hydrofabric_id] * 12 + [limit]
        return con.execute(sql, params).df()
    finally:
        try:
            con.close()
        except Exception:
            pass

def _duckdb_query_parquet(file_url: str, query: str) -> pd.DataFrame:
    """Execute an arbitrary DuckDB query against a parquet file exposed as temp view `output`."""
    safe_file_url = file_url.replace("'", "''")

    con = duckdb.connect(database=":memory:")
    try:
        try:
            con.execute("LOAD httpfs")
        except Exception:
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")

        con.execute(f"CREATE OR REPLACE TEMP VIEW output AS SELECT * FROM read_parquet('{safe_file_url}')")
        return con.sql(query).df()
    finally:
        try:
            con.close()
        except Exception:
            pass

def _duckdb_query_netcdf(df: pd.DataFrame , query: str) -> pd.DataFrame:
    """Execute an arbitrary DuckDB query against a netcdf file exposed as temp view `output`."""
    
    con = duckdb.connect(database=":memory:")
    con.register('tmp_table_nc', df)
    try:
        con.execute(f"CREATE OR REPLACE TEMP VIEW output AS SELECT * FROM tmp_table_nc")
        return con.sql(query).df()
    finally:
        try:
            con.close()
        except Exception:
            pass

def _normalize_date_yyyymmdd(date_str: str | None) -> str | None:
    """Normalize a date string to YYYYMMDD.

    Accepts:
      - YYYYMMDD
      - YYYY-MM-DD
      - YYYY/MM/DD
    """
    if not date_str:
        return None

    s = str(date_str).strip()
    if len(s) == 8 and s.isdigit():
        return s

    s = s.replace("/", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        return None

def _normalize_date_folder(date_str: str | None, *, default_prefix: str = "ngen") -> str | None:
    """Normalize a date folder name for the S3 layout.

    The datastream commonly uses folders like: ngen.YYYYMMDD

    Accepts:
      - ngen.YYYYMMDD
      - ngen.YYYY-MM-DD
      - ngen.YYYY/MM/DD
      - YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD (prefix added)
    """
    if not date_str:
        return None

    s = str(date_str).strip()
    if "." in s:
        prefix, tail = s.split(".", 1)
        yyyymmdd = _normalize_date_yyyymmdd(tail)
        return f"{prefix}.{yyyymmdd}" if yyyymmdd else None

    yyyymmdd = _normalize_date_yyyymmdd(s)
    return f"{default_prefix}.{yyyymmdd}" if yyyymmdd else None

def _extract_yyyymmdd_from_date_folder(folder: str) -> str | None:
    """Extract YYYYMMDD from a folder like 'ngen.20260127'."""
    if not folder:
        return None
    base = folder.strip().rstrip("/")
    if "." in base:
        _, tail = base.split(".", 1)
        return _normalize_date_yyyymmdd(tail)
    return _normalize_date_yyyymmdd(base)

def _label_from_id(value: str) -> str:
    """Default label: replace underscores with spaces."""
    return value.replace("_", " ")

