"""Lockstep contract tests for tool descriptions.

Pattern reference:
``docs/solutions/best-practices/lockstep-rule-description-string-drift-2026-05-11.md``

Positive assertions: the description must contain load-bearing constraints
(parquet-only, error class names, provenance column names).

Negative assertions: the description must NOT contain concrete example values.
Per ``feedback_no_examples_in_tool_descriptions.md``, LLMs copy concrete
examples verbatim - any ``s3://`` URL, example filename, or inline SQL in
the description leaks into tool calls.
"""

from __future__ import annotations

import re

from nextgen_mcp._tool_descriptions import QUERY_FILES_BY_SELECTOR_DESCRIPTION


def test_query_files_by_selector_description_positive_invariants() -> None:
    desc = QUERY_FILES_BY_SELECTOR_DESCRIPTION
    lower = desc.lower()

    # Names the parquet-only constraint
    assert "parquet only" in lower

    # Names the error class the LLM may receive on NetCDF-target calls
    assert "unsupported_format" in desc

    # Names the provenance columns the LLM can reference in SQL
    assert "filename" in desc
    assert "source_path" in desc

    # Names the file_name / index filter args so the LLM knows the filter shape
    assert "file_name" in desc
    assert "index" in desc

    # Names _excluded_netcdf_count so the LLM knows to look for it
    assert "_excluded_netcdf_count" in desc


def test_query_files_by_selector_description_negative_invariants() -> None:
    """Description must not embed concrete example values that LLMs would copy."""
    desc = QUERY_FILES_BY_SELECTOR_DESCRIPTION

    # No concrete URLs - LLMs would copy these into tool calls
    assert "s3://" not in desc
    assert "https://" not in desc

    # No example filenames - "*.parquet" or "troute_output_..." style
    assert "troute_output" not in desc
    assert "*.parquet" not in desc

    # The phrase "parquet only" is the constraint statement and uses lowercase
    # "parquet" deliberately. Bare ".parquet" filename suffixes (with the dot)
    # would be example values; assert none appear.
    assert ".parquet" not in desc

    # No inline SQL examples - just structural mentions ("SELECT or WITH...SELECT")
    # are acceptable, but "SELECT * FROM ..." or specific column names are not
    assert "SELECT * FROM" not in desc
    assert "WHERE feature_id" not in desc

    # No specific selector example values (model names, vpu IDs, etc.)
    # The description should describe the shape, not name specific instances.
    assert "cfe_nom" not in desc
    assert "short_range" not in desc
    assert not re.search(r"\bVPU_\d+", desc)
