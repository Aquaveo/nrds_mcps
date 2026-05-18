"""Tests for ``query_output_files_from_output_selector`` — the multi-file
parquet-query MCP tool.

The function resolves an S3 directory from (model, date, forecast, cycle,
vpu, ensemble), lists the files there, filters to parquet, and runs a
single DuckDB query across all of them with a ``filename`` provenance
column. These tests pin the four code paths that matter:

  - happy path (3 parquet files, query returns rows)
  - directory contains only ``.nc`` files (not_found with count)
  - directory is empty (not_found with count=0)
  - DuckDB raises ``BinderException`` (LLM-recoverable invalid_query envelope)

Following the same monkeypatch-the-helpers pattern as
``test_exception_handling.py`` — we never hit live S3 or DuckDB.
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


def test_query_output_files_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three parquet files resolved; DuckDB returns rows with filename column."""
    listing = [
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/file_a.parquet",
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/file_b.parquet",
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/file_c.parquet",
    ]
    _install_fs(monkeypatch, listing)

    captured_urls: dict = {}

    def _fake_query(file_urls: List[str], query: str) -> pd.DataFrame:
        captured_urls["urls"] = file_urls
        captured_urls["query"] = query
        # Simulate one row per file with filename provenance + a real column.
        return pd.DataFrame(
            [
                {"filename": "s3://bucket/file_a.parquet", "feature_id": 1},
                {"filename": "s3://bucket/file_b.parquet", "feature_id": 2},
                {"filename": "s3://bucket/file_c.parquet", "feature_id": 3},
            ]
        )

    # Patch at the import site inside logic.query_output_files_from_output_selector
    # (the function does ``from .utils_rest import _duckdb_query_parquets``
    # at call time, so patching the source module is the reliable seam).
    from nextgen_mcp import utils_rest
    monkeypatch.setattr(utils_rest, "_duckdb_query_parquets", _fake_query)

    result = logic.query_output_files_from_output_selector(
        **SELECTOR,
        query="SELECT filename, feature_id FROM output",
    )

    assert result.get("ok") is True, result
    assert result["file_count"] == 3
    assert len(result["files"]) == 3
    # File names are extracted from the S3 path basename.
    assert {f["name"] for f in result["files"]} == {
        "file_a.parquet",
        "file_b.parquet",
        "file_c.parquet",
    }
    assert result["file_type"] == "parquet"
    assert "filename" in result["columns"]
    assert result["rows"] == 3
    assert len(result["data"]) == 3
    # Verify the helper actually received the 3 parquet URLs.
    assert len(captured_urls["urls"]) == 3
    assert all(u.endswith(".parquet") for u in captured_urls["urls"])


def test_query_output_files_not_found_only_netcdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selector resolves to a dir containing only .nc files → not_found.

    The envelope's ``count`` reflects the unfiltered total so the LLM can
    tell the directory had files, just not parquet — which is the signal
    to redirect to the singular ``query_output_file_from_output_selector``.
    """
    listing = [
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/out_a.nc",
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/out_b.nc",
    ]
    _install_fs(monkeypatch, listing)

    result = logic.query_output_files_from_output_selector(
        **SELECTOR,
        query="SELECT * FROM output",
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "not_found"
    assert result["file_count"] == 0
    assert result["files"] == []
    # count reflects unfiltered total — directory had 2 .nc files.
    assert result["count"] == 2


def test_query_output_files_not_found_empty_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty selector directory → not_found with count=0."""
    _install_fs(monkeypatch, [])

    result = logic.query_output_files_from_output_selector(
        **SELECTOR,
        query="SELECT * FROM output",
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "not_found"
    assert result["file_count"] == 0
    assert result["count"] == 0


def test_query_output_files_binder_exception_returns_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """DuckDB BinderException → recoverable invalid_query envelope with fix_hint.

    Same recovery contract as the singular ``query_output_file`` — the LLM
    gets ``available_columns`` parsed from DuckDB's "Candidate bindings"
    message and ``fix_hint`` text telling it to retry once with a real
    column. Pins parity with the singular tool's envelope shape.
    """
    listing = [
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/file_a.parquet",
        "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/20260501/short_range/00/06/ngen-run/outputs/troute/file_b.parquet",
    ]
    _install_fs(monkeypatch, listing)

    def _raise_binder(file_urls: List[str], query: str) -> pd.DataFrame:
        raise duckdb.BinderException(
            'Referenced column "variable" not found in FROM clause!\n'
            'Candidate bindings: "output.feature_id", "output.velocity"'
        )

    from nextgen_mcp import utils_rest
    monkeypatch.setattr(utils_rest, "_duckdb_query_parquets", _raise_binder)

    result = logic.query_output_files_from_output_selector(
        **SELECTOR,
        query="SELECT * FROM output WHERE variable = 'velocity'",
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "invalid_query"
    assert result["available_columns"] == ["feature_id", "velocity"]
    assert "feature_id" in result["fix_hint"]
    # File listing still surfaced in the envelope so the LLM has context.
    assert result["file_count"] == 2
    assert len(result["files"]) == 2
