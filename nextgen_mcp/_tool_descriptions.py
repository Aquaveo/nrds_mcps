"""Module-level tool description constants.

Tool description prose is extracted here so ``test_mcp/test_tool_descriptions.py``
can lock the content against drift via positive + negative substring assertions.

Description content must obey:
  - Positive: name the load-bearing constraints (parquet-only, provenance
    columns, filter arg shape).
  - Negative: no concrete example values. LLMs copy concrete examples
    verbatim into tool calls. No ``s3://`` URLs, no example filenames,
    no inline SQL. Also no negative-form mentions of formats we don't
    support — weak quantized models token-match on the format name
    regardless of polarity, so naming "NetCDF" even as an unsupported
    case leaks the token into the model's working set.
"""

QUERY_FILES_BY_SELECTOR_DESCRIPTION = (
    "Query NRDS parquet outputs by selector. Parquet only. "
    "Queries one OR many files in a single call: pass `file_name` or "
    "`index` to filter to one file; omit both to query the full selector "
    "as a unioned dataset. "
    "Result rows always carry `filename` and `source_path` provenance "
    "columns so SQL can group or filter by source. "
    "The SQL must be a single read-only SELECT or WITH...SELECT against "
    "table `output`. For data extraction, prefer WHERE filtering over "
    "LIMIT - LIMIT silently drops rows and breaks ordered time series. "
    "Use aggregates (COUNT, SUM, AVG, MAX, MIN) for summary statistics."
)

LIST_AVAILABLE_OUTPUT_FILES_DESCRIPTION = (
    "List available output files for a given model, date, forecast, cycle, "
    "and VPU (accepts id or label, including subregion VPUs). Optional "
    "ensemble member for applicable forecast. Parquet only."
)
