#nextgen_plugins/chatbox/utils_rest.py
from datetime import datetime
from typing import Any, Dict, List, Optional
import json
import re
import pandas as pd
import duckdb
import xarray as xr

from ._io_config import HYDROFABRIC_INDEX_URL, duckdb_connect_with_httpfs, open_fsspec_file


# Per-code sanitized message + fix_hint. NEVER use str(exc) directly - 
# The LLM-facing envelope sees the sanitized
# message only; the full str(exc) is logged at WARNING+ for operators.
_IO_ERROR_CATALOG = {
    "timeout": (
        "Upstream request timed out.",
        "Upstream timed out. Retry the same call once; if it fails again, "
        "surface the error to the user and try a different selector "
        "(e.g., a different date or forecast cycle).",
    ),
    "permission_denied": (
        "Access denied to the upstream data store.",
        "Access denied to the upstream data store. Do NOT retry - this is "
        "a deployment configuration issue. Surface to the user as a "
        "server-side problem.",
    ),
    "upstream_error": (
        "Upstream data store error.",
        "Upstream data store error. Do NOT retry the same call - try a "
        "different selector combination (model/date/forecast/vpu) before "
        "reporting failure to the user.",
    ),
    "not_found": (
        "Requested resource was not found.",
        "Requested resource was not found. Do NOT retry the same call - "
        "the selector combination does not match any available data. Try "
        "a different selector or check what's available via the "
        "corresponding list_* tool.",
    ),
    "execution_error": (
        "Internal execution error.",
        "Internal execution error. Do NOT retry - surface to the user. If "
        "reproducible, this is a server-side bug.",
    ),
}

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
    },
    "gage": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/gage.pmtiles",
        "map_layer_id": "conus-gauges",
        "id_property": "id",
    },
    "divides": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/divides.pmtiles",
        "map_layer_id": "divides",
        "id_property": "divide_id",
    },
    "hydrolocations": {
        "pmtiles_url": "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/kepler/hydrolocations.pmtiles",
        "map_layer_id": "nexus-points",
        "id_property": "id",
    },
}

# Regex to pull DuckDB's "Candidate bindings:" column suggestions out of
# a BinderException message. Format observed:
#     Binder Error: Referenced column "X" not found in FROM clause!
#     Candidate bindings: "output.feature_id", "output.velocity"
# The strip after the dot is to normalize "output.velocity" -> "velocity".
_DUCKDB_CANDIDATES_RE = re.compile(
    r'Candidate bindings:\s*(.+?)(?:\n|$)', re.IGNORECASE
)


def _classify_io_error(exc: BaseException) -> tuple[str, str, str]:
    """Return (error_code, sanitized_message, fix_hint) for any caught IO exception.

    Maps the raised exception class to a stable error code and per-code
    sanitized text. The raw ``str(exc)`` is intentionally NOT propagated
    into the LLM-facing envelope - see _IO_ERROR_CATALOG for the rationale.

    Programmer-error DuckDB classes (BinderException, ParserException,
    CatalogException) MUST be re-raised before reaching this helper -
    classifying them as execution_error would mask wrong-column / malformed
    -SQL / missing-table bugs and let an LLM retry forever. Callers should
    check ``isinstance(exc, (duckdb.BinderException, duckdb.ParserException,
    duckdb.CatalogException))`` and re-raise before calling this function.
    """
    from botocore.exceptions import ClientError

    code: str
    if isinstance(exc, TimeoutError):
        code = "timeout"
    elif isinstance(exc, PermissionError):
        code = "permission_denied"
    elif isinstance(exc, FileNotFoundError):
        code = "not_found"
    elif isinstance(exc, ClientError):
        # Inspect the AWS error code for AccessDenied / NoSuchKey
        aws_code = exc.response.get("Error", {}).get("Code", "")
        if aws_code in ("AccessDenied", "403"):
            code = "permission_denied"
        elif aws_code in ("NoSuchKey", "NoSuchBucket", "404"):
            code = "not_found"
        else:
            code = "upstream_error"
    elif isinstance(exc, duckdb.IOException):
        code = "upstream_error"
    elif isinstance(exc, ConnectionError):
        code = "upstream_error"
    elif isinstance(exc, OSError):
        # OSError parent covers many filesystem/network classes not pinned above.
        # Default to upstream_error for these; if a more specific case
        # emerges in production, branch here.
        code = "upstream_error"
    else:
        code = "execution_error"

    message, fix_hint = _IO_ERROR_CATALOG[code]
    return code, message, fix_hint

def _is_duckdb_programmer_error(exc: BaseException) -> bool:
    """True if exc is a DuckDB SQL programmer-error class that must NOT be
    caught when the SQL was hardcoded by us.

    Wrong-column / malformed-SQL / missing-table errors in OUR hardcoded
    SQL should crash visibly so they're discoverable in observability logs,
    not normalized to a polite envelope. CRITICAL: this guard applies ONLY
    to hardcoded-SQL call sites (e.g. _duckdb_lookup_hydrofabric_feature).

    For LLM-supplied-SQL call sites (query_files_by_selector's `query`
    arg), use ``_classify_llm_sql_error`` instead - the LLM CAN recover
    from these if given a structured envelope with the column list as
    fix_hint, the same pattern InputValidationEnvelopeMiddleware uses
    for kwarg errors.
    """
    return isinstance(
        exc,
        (
            duckdb.BinderException,
            duckdb.ParserException,
            duckdb.CatalogException,
        ),
    )

def _extract_duckdb_candidates(exc_message: str) -> list[str]:
    """Pull the candidate column names out of a DuckDB BinderException message.

    Returns ``[]`` if no candidate-bindings clause is found (e.g., the error
    is ParserException, or the message format changed in a future DuckDB
    version). Callers should handle the empty case as "we don't know the
    columns; fix_hint can't list them."
    """
    match = _DUCKDB_CANDIDATES_RE.search(exc_message)
    if not match:
        return []
    raw = match.group(1)
    # raw looks like: '"output.feature_id", "output.velocity"'
    parts = re.findall(r'"([^"]+)"', raw)
    columns: list[str] = []
    for part in parts:
        # Strip leading table-qualifier: "output.velocity" -> "velocity"
        # Keep the original if there's no dot.
        bare = part.rsplit(".", 1)[-1] if "." in part else part
        if bare and bare not in columns:
            columns.append(bare)
    return columns

def _classify_llm_sql_error(
    exc: BaseException, file_url: str, query: str
) -> tuple[str, str, str, list[str]]:
    """Return (error_code, sanitized_message, fix_hint, available_columns)
    for a DuckDB programmer-error class fired against LLM-supplied SQL.

    The LLM-facing envelope from this classifier is the SQL analogue of
    InputValidationEnvelopeMiddleware's `invalid_args` envelope: it gives
    the LLM a structured way to recover in one retry instead of stalling
    in a thinking loop (as observed with qwen on 2026-05-10).

    Callers must use this AT LLM-supplied-SQL call sites only (currently
    ``query_output_file`` in rest.py). For hardcoded-SQL paths, use the
    existing ``_is_duckdb_programmer_error`` re-raise guard instead.
    """
    exc_msg = str(exc)
    if isinstance(exc, duckdb.BinderException):
        columns = _extract_duckdb_candidates(exc_msg)
        if columns:
            column_list = ", ".join(columns)
            fix_hint = (
                f"The query references a column that does not exist in the "
                f"output file. The actual columns are: {column_list}. "
                f"Rewrite the query using one of these column names and "
                f"retry once."
            )
        else:
            fix_hint = (
                "The query references a column that does not exist in the "
                "output file. Rewrite the query using only the columns "
                "documented for this output kind, or check what's available "
                "by running a probe query like SELECT * FROM output LIMIT 1, "
                "and retry once."
            )
        return (
            "invalid_query",
            "Query references a column that does not exist in the output file.",
            fix_hint,
            columns,
        )
    if isinstance(exc, duckdb.ParserException):
        return (
            "invalid_query",
            "Query is not valid SQL syntax.",
            (
                "The query is not valid DuckDB SQL. Common causes: missing "
                "comma, unbalanced parenthesis, wrong keyword order. Rewrite "
                "as a single SELECT statement against the `output` table and "
                "retry once."
            ),
            [],
        )
    if isinstance(exc, duckdb.CatalogException):
        return (
            "invalid_query",
            "Query references a missing table.",
            (
                "The query references a table that does not exist. The only "
                "queryable table in this context is `output`. Rewrite the "
                "FROM clause to `FROM output` and retry once."
            ),
            [],
        )
    # Other duckdb.Error subclasses or non-duckdb classes: fall back to
    # the generic IO classifier so callers always get a typed result.
    code, msg, fix_hint = _classify_io_error(exc)
    return code, msg, fix_hint, []

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

def _duckdb_lookup_hydrofabric_feature(
    hydrofabric_id: str, limit: int = 1
) -> pd.DataFrame:
    """Lookup hydrofabric rows by id/divide_id using exact and substring matching."""
    con = duckdb_connect_with_httpfs()
    try:
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

def _get_troute_df(s3_nc_url: str) -> pd.DataFrame:
    """Load the t-route crosswalk DataFrame.

    Uses ``open_fsspec_file`` so the timeout-configured fsspec client
    reaches the underlying h5netcdf transport. ``xarray.open_dataset``
    cannot be called directly on a URL with a custom fsspec config - the
    OpenFile context manager handles that.
    """

    with open_fsspec_file(s3_nc_url) as f:
        nc_xarray = xr.open_dataset(f, engine="h5netcdf")
        nc_df = nc_xarray.to_dataframe()
        nc_df = nc_df.reset_index()

    return nc_df

def _duckdb_query_parquets(file_urls: List[str], query: str) -> pd.DataFrame:
    """Execute an arbitrary DuckDB query across multiple parquet files exposed as one temp view `output`.

    The view exposes two provenance columns alongside the parquet data:

    - ``filename`` — **basename only** (e.g. ``troute_output_202605180100.parquet``).
      Computed as ``regexp_replace(filename, '^.*/', '')`` so LLM SQL can
      ``substr``/``regexp_extract`` time portions out of the filename without
      tripping over the S3 URL prefix.
    - ``source_path`` — full S3 URL of the source file. Use for unambiguous
      row → URL provenance when basename alone is ambiguous.

    ``union_by_name=true`` tolerates same-name columns appearing in different
    orders across files; type-incompatible same-named columns still surface
    as a DuckDB error (a real schema bug worth raising).
    """
    safe_file_urls = [u.replace("'", "''") for u in file_urls]
    quoted = ", ".join(f"'{u}'" for u in safe_file_urls)

    con = duckdb_connect_with_httpfs()
    try:
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW output AS "
            f"SELECT "
            f"  regexp_replace(filename, '^.*/', '') AS filename, "
            f"  filename AS source_path, "
            f"  * EXCLUDE (filename) "
            f"FROM read_parquet([{quoted}], union_by_name=true, filename=true)"
        )
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
