"""Module-level tool description constants.

Tool description prose is extracted here so ``test_mcp/test_tool_descriptions.py``
can lock the content against drift via positive + negative substring assertions.

Description content must obey:
  - Positive: name the load-bearing constraints (parquet-only, the error
    classes the LLM may receive, provenance columns).
  - Negative: no concrete example values. LLMs copy concrete examples
    verbatim into tool calls. No ``s3://`` URLs, no example filenames,
    no inline SQL.
"""

QUERY_FILES_BY_SELECTOR_DESCRIPTION = (
    "Query NRDS parquet outputs by selector. Queries one OR many files "
    "in a single call: pass `file_name` or `index` to filter to one file; "
    "omit both to query the full selector as a unioned dataset. "
    "Parquet only - NetCDF files return an `unsupported_format:` envelope. "
    "Result rows always carry `filename` and `source_path` provenance "
    "columns so SQL can group or filter by source. "
    "Mixed-format selectors (parquet plus NetCDF) silently filter to "
    "parquet and surface the exclusion count as `_excluded_netcdf_count` "
    "on the result envelope when non-zero. "
    "The SQL must be a single read-only SELECT or WITH...SELECT against "
    "table `output`. For data extraction, prefer WHERE filtering over "
    "LIMIT - LIMIT silently drops rows and breaks ordered time series. "
    "Use aggregates (COUNT, SUM, AVG, MAX, MIN) for summary statistics."
)
