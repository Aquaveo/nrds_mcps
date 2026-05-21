"""Tests for the ``_duckdb_query_parquets`` helper and shared payload
summarizer.

The four behavior tests that previously lived here (against the deleted
``query_output_files_from_output_selector``) were superseded in v0.5.0 by:

  - ``test_query_files_by_selector.py``: end-to-end coverage of the new
    unified tool including unsupported_format / no_supported_files /
    excluded_netcdf_count envelopes
  - ``test_exception_handling.py``: migrated BinderException recovery test
    targeting ``query_files_by_selector`` + ``_duckdb_query_parquets``

What remains here are the helper-level live-DuckDB tests against
``_duckdb_query_parquets`` (filename basename, substr-on-filename) and the
``_summarize_tool_result`` helper.
"""

from __future__ import annotations

from typing import Any, List

import duckdb
import pandas as pd
import pytest

from nextgen_mcp import logic


SELECTOR = {
    "model": "cfe_nom",
    "date": "2026-05-01",
    "forecast": "short_range",
    "cycle": "00",
    "vpu": "06",
}


class _MockFs:
    """Minimal fsspec filesystem stand-in returning a fixed file list from ls."""

    def __init__(self, listing: List[str]):
        self._listing = listing

    def ls(self, *_args: Any, **_kwargs: Any) -> List[str]:
        return list(self._listing)


def _install_fs(monkeypatch: pytest.MonkeyPatch, listing: List[str]) -> None:
    """Patch ``s3_filesystem`` at the call site in logic.py."""
    monkeypatch.setattr(logic, "s3_filesystem", lambda: _MockFs(listing))


def test_duckdb_query_parquets_filename_is_basename(tmp_path) -> None:
    """Integration: filename column exposed to SQL is basename-only.

    Live DuckDB call against two local parquet fixtures. Verifies:
    - filename column contains just the basename (no s3:// prefix, no path)
    - source_path column contains the full URL passed to read_parquet
    - parquet columns are still selectable alongside the provenance columns

    This is the test that would have caught the bug seen in production where
    `filename` was the full S3 URL and the LLM's `substr(filename, 15, 12)`
    returned 'munity-ngen-' instead of the date portion of the basename.
    """
    from nextgen_mcp.utils_rest import _duckdb_query_parquets

    fixture_a = tmp_path / "troute_output_202605180100.parquet"
    fixture_b = tmp_path / "troute_output_202605190100.parquet"
    pd.DataFrame({"feature_id": [1, 2], "velocity": [1.5, 2.5]}).to_parquet(fixture_a)
    pd.DataFrame({"feature_id": [3, 4], "velocity": [3.5, 4.5]}).to_parquet(fixture_b)

    # Local file URLs — DuckDB's read_parquet handles file:// paths the same
    # way it handles s3://, and the basename regex is path-style-agnostic.
    urls = [str(fixture_a), str(fixture_b)]

    df = _duckdb_query_parquets(
        urls,
        "SELECT filename, source_path, feature_id, velocity FROM output ORDER BY feature_id",
    )

    # filename: basename only, no slashes
    assert list(df["filename"]) == [
        "troute_output_202605180100.parquet",
        "troute_output_202605180100.parquet",
        "troute_output_202605190100.parquet",
        "troute_output_202605190100.parquet",
    ]
    assert all("/" not in name for name in df["filename"])

    # source_path: full path, with slashes
    assert all(str(fixture_a) == p or str(fixture_b) == p for p in df["source_path"])

    # Parquet columns survive the projection
    assert list(df["feature_id"]) == [1, 2, 3, 4]
    assert list(df["velocity"]) == [1.5, 2.5, 3.5, 4.5]


def test_summarize_tool_result_includes_aggregates_and_sample_row() -> None:
    """Log summary must show enough to spot-check the result without dumping
    the full ``data`` array.

    Contract: aggregates (file_count, rows, columns *by name*) + ONE sample
    row, all under ~500 chars. Reprs of full data arrays produce ~19 KB
    single-line log entries per call; too-sparse stats lines hide whether
    the data looked right. This shape is the middle ground.
    """
    from nextgen_mcp.utils import _summarize_tool_result

    success = {
        "ok": True,
        "file_count": 10,
        "rows": 240,
        "columns": ["time", "flow"],
        "data": [
            {"time": "2026-05-18T02:00:00.000000Z", "flow": 18.58},
            {"time": "2026-05-18T03:00:00.000000Z", "flow": 27.91},
        ],
    }
    summary = _summarize_tool_result(success)
    # Aggregates.
    assert "ok=True" in summary
    assert "file_count=10" in summary
    assert "rows=240" in summary
    # Column NAMES, not just count — operator can verify projection.
    assert "columns=[time,flow]" in summary
    # ONE sample row for spot-checking.
    assert "sample_row=" in summary
    assert "2026-05-18T02:00:00.000000Z" in summary
    assert "18.58" in summary
    # But NOT 240 rows.
    assert "27.91" not in summary
    # Single line, bounded length — log readability gate.
    assert "\n" not in summary
    assert len(summary) < 500

    # Wide columns get truncated with a count tail.
    wide = {
        "ok": True,
        "file_count": 1,
        "rows": 1,
        "columns": [f"c{i}" for i in range(12)],
        "data": [{f"c{i}": i for i in range(12)}],
    }
    wide_summary = _summarize_tool_result(wide)
    assert "...+4" in wide_summary, (
        f"wide column list should show a +N truncation tail; got {wide_summary!r}"
    )


def test_summarize_tool_result_error_envelope_surfaces_code_and_message() -> None:
    """Error envelopes log code, message, and fix_hint preview — enough
    to triage without re-running the call."""
    from nextgen_mcp.utils import _summarize_tool_result

    error_envelope = {
        "ok": False,
        "error": {
            "code": "not_found",
            "message": "No output files matched the selector.",
        },
        "fix_hint": "Use list_available_output_files to confirm files exist first.",
    }
    err_summary = _summarize_tool_result(error_envelope)
    assert "ok=False" in err_summary
    assert "code=not_found" in err_summary
    assert "message=No output files matched" in err_summary
    assert "fix_hint_preview=" in err_summary


def test_duckdb_query_parquets_substr_on_filename_yields_useful_date(tmp_path) -> None:
    """Integration: substr on the basename gives the LLM-extractable date.

    Pins the specific user-facing scenario: the LLM tries to extract a date
    from the filename, e.g. `substr(filename, 15, 12)` against
    `troute_output_202605180100.parquet`. With basename-only filename, this
    yields `202605180100` (the timestamp), not garbage.
    """
    from nextgen_mcp.utils_rest import _duckdb_query_parquets

    fixture = tmp_path / "troute_output_202605180100.parquet"
    pd.DataFrame({"feature_id": [1], "velocity": [1.5]}).to_parquet(fixture)

    df = _duckdb_query_parquets(
        [str(fixture)],
        "SELECT substr(filename, 15, 12) AS event_time FROM output",
    )

    # 'troute_output_202605180100.parquet'[14:26] (1-indexed in SQL) = '202605180100'
    assert df["event_time"].iloc[0] == "202605180100"
