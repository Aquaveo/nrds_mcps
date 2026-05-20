"""Tests for ``query_files_by_selector`` — the unified parquet-query MCP tool
introduced in v0.5.0.

Replaces the legacy 4-tool cluster (``query_output_file``,
``query_output_file_from_output_selector``,
``query_output_files_from_output_selector``, ``resolve_output_file``)
with one tool that handles single-file filter (via ``file_name`` or
``index``) AND no-filter ("query all parquet files for the selector") in
one signature.

Test coverage spans:
- Happy paths: no-filter, file_name-set, index-set
- XOR validator: both file_name + index set
- Pydantic edge cases: empty string, whitespace, negative index, oob index
- unsupported_format: envelope (file_name points at .nc/.nc4)
- no_supported_files: envelope (selector resolves to only NetCDF files)
- Mixed-format selector: surfaces ``_excluded_netcdf_count``
- Index parquet-only semantics: NetCDF files do not consume index slots

Following the monkeypatch-the-helpers pattern from
``test_query_output_files.py`` — we never hit live S3 or DuckDB.
"""

from __future__ import annotations

from typing import Any, List

import pandas as pd
import pytest
from pydantic import ValidationError

from nextgen_mcp import logic


SELECTOR = {
    "model": "cfe_nom",
    "date": "2026-05-01",
    "forecast": "short_range",
    "cycle": "00",
    "vpu": "06",
}


class _MockFs:
    """Minimal fsspec filesystem stand-in returning a fixed listing."""

    def __init__(self, listing: List[str]):
        self._listing = listing

    def ls(self, *_args: Any, **_kwargs: Any) -> List[str]:
        return list(self._listing)


def _install_fs(monkeypatch: pytest.MonkeyPatch, listing: List[str]) -> None:
    monkeypatch.setattr(logic, "s3_filesystem", lambda: _MockFs(listing))


def _install_fake_parquets_query(
    monkeypatch: pytest.MonkeyPatch, df_factory
) -> dict:
    """Patch ``_duckdb_query_parquets`` and capture the URLs + query passed."""
    captured: dict = {}

    def _fake(file_urls: List[str], query: str) -> pd.DataFrame:
        captured["urls"] = list(file_urls)
        captured["query"] = query
        return df_factory(file_urls, query)

    from nextgen_mcp import utils_rest
    monkeypatch.setattr(utils_rest, "_duckdb_query_parquets", _fake)
    return captured


_S3_DIR = (
    "ciroh-community-ngen-datastream/outputs/cfe_nom/v2.2_hydrofabric/"
    "20260501/short_range/00/06/ngen-run/outputs/troute"
)


def _full(name: str) -> str:
    return f"{_S3_DIR}/{name}"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_no_filter_returns_all_parquet_files(monkeypatch):
    listing = [_full(n) for n in ("a.parquet", "b.parquet", "c.parquet")]
    _install_fs(monkeypatch, listing)
    captured = _install_fake_parquets_query(
        monkeypatch,
        lambda urls, q: pd.DataFrame(
            [{"filename": f"file_{i}.parquet", "feature_id": i} for i in range(len(urls))]
        ),
    )

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
    )

    assert result.get("ok") is True
    assert len(captured["urls"]) == 3
    assert result["file_count"] == 3
    assert result["rows"] == 3
    # No exclusion field when no NetCDF was filtered out
    assert "_excluded_netcdf_count" not in result


def test_file_name_filter_returns_single_file(monkeypatch):
    listing = [_full(n) for n in ("a.parquet", "b.parquet", "c.parquet")]
    _install_fs(monkeypatch, listing)
    captured = _install_fake_parquets_query(
        monkeypatch,
        lambda urls, q: pd.DataFrame([{"filename": "b.parquet", "feature_id": 42}]),
    )

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="b.parquet",
    )

    assert result.get("ok") is True
    assert len(captured["urls"]) == 1
    assert captured["urls"][0].endswith("b.parquet")
    assert result["file_count"] == 1


def test_index_filter_returns_single_file_at_index(monkeypatch):
    listing = [_full(n) for n in ("a.parquet", "b.parquet", "c.parquet")]
    _install_fs(monkeypatch, listing)
    captured = _install_fake_parquets_query(
        monkeypatch,
        lambda urls, q: pd.DataFrame([{"filename": "b.parquet", "feature_id": 99}]),
    )

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        index=1,
    )

    assert result.get("ok") is True
    assert len(captured["urls"]) == 1
    assert captured["urls"][0].endswith("b.parquet")


# ---------------------------------------------------------------------------
# XOR + Pydantic edge cases
# ---------------------------------------------------------------------------


def test_both_file_name_and_index_rejected(monkeypatch):
    listing = [_full("a.parquet")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="a.parquet",
        index=0,
    )

    assert result.get("ok") is False
    assert "invalid_args" in result["error"]["code"]


def test_empty_string_file_name_rejected(monkeypatch):
    listing = [_full("a.parquet")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="",
    )

    assert result.get("ok") is False
    assert "invalid_args" in result["error"]["code"]


def test_whitespace_file_name_rejected(monkeypatch):
    listing = [_full("a.parquet")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="   ",
    )

    assert result.get("ok") is False
    assert "invalid_args" in result["error"]["code"]


def test_negative_index_rejected(monkeypatch):
    listing = [_full("a.parquet")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        index=-1,
    )

    assert result.get("ok") is False
    assert "invalid_args" in result["error"]["code"]


def test_out_of_range_index_returns_invalid_args(monkeypatch):
    listing = [_full(n) for n in ("a.parquet", "b.parquet")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        index=999999,
    )

    assert result.get("ok") is False
    assert "invalid_args" in result["error"]["code"]
    # Surface the actual count so the LLM can retry with a valid index
    assert result.get("files_found") == 2


def test_file_name_not_found_returns_not_found(monkeypatch):
    listing = [_full(n) for n in ("a.parquet", "b.parquet")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="missing.parquet",
    )

    assert result.get("ok") is False
    assert "not_found" in result["error"]["code"]


# ---------------------------------------------------------------------------
# unsupported_format: envelope
# ---------------------------------------------------------------------------


def test_file_name_pointing_at_netcdf_returns_unsupported_format(monkeypatch):
    """No S3 I/O required - cheap local extension check on file_name."""
    # We intentionally don't install fs because the check must short-circuit
    # before fs.ls is called. If implementation tries to list, the next
    # call would raise AttributeError on the missing patch.

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="something.nc",
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "unsupported_format"
    assert result.get("format_detected") == "netcdf"
    assert result.get("file_name") == "something.nc"
    assert "fix_hint" in result


def test_file_name_pointing_at_nc4_returns_unsupported_format(monkeypatch):
    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        file_name="something.nc4",
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "unsupported_format"
    assert result.get("format_detected") == "netcdf"


# ---------------------------------------------------------------------------
# no_supported_files: envelope
# ---------------------------------------------------------------------------


def test_selector_only_netcdf_returns_no_supported_files(monkeypatch):
    listing = [_full(n) for n in ("a.nc", "b.nc", "c.nc4")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "no_supported_files"
    assert result["files_found"] == 3
    assert result["netcdf_files"] == 3
    assert result["parquet_files"] == 0


# ---------------------------------------------------------------------------
# Mixed-format selector
# ---------------------------------------------------------------------------


def test_mixed_format_selector_surfaces_excluded_netcdf_count(monkeypatch):
    listing = [
        _full("a.parquet"),
        _full("b.nc"),
        _full("c.parquet"),
        _full("d.parquet"),
        _full("e.nc4"),
    ]
    _install_fs(monkeypatch, listing)
    _install_fake_parquets_query(
        monkeypatch,
        lambda urls, q: pd.DataFrame(
            [{"filename": "x.parquet", "feature_id": i} for i in range(len(urls))]
        ),
    )

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
    )

    assert result.get("ok") is True
    assert result["file_count"] == 3  # only parquet
    assert result["_excluded_netcdf_count"] == 2


# ---------------------------------------------------------------------------
# Index parquet-only semantics — NetCDF files do not consume index slots
# ---------------------------------------------------------------------------


def test_index_skips_netcdf_files_in_mixed_listing(monkeypatch):
    """When mixed [a.parquet, b.nc, c.parquet], index=1 → c.parquet, NOT b.nc."""
    listing = [_full(n) for n in ("a.parquet", "b.nc", "c.parquet")]
    _install_fs(monkeypatch, listing)
    captured = _install_fake_parquets_query(
        monkeypatch,
        lambda urls, q: pd.DataFrame([{"filename": "c.parquet", "feature_id": 1}]),
    )

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        index=1,
    )

    assert result.get("ok") is True
    assert len(captured["urls"]) == 1
    # CRITICAL: index=1 resolves to the SECOND PARQUET file (c.parquet),
    # not the second file in the raw sorted listing (b.nc).
    assert captured["urls"][0].endswith("c.parquet")


def test_index_zero_against_all_netcdf_returns_no_supported_files(monkeypatch):
    """index=0 against an all-NetCDF directory: no parquet exists at any
    index, so no_supported_files: (not unsupported_format:) — the request
    shape was valid; the selector yielded no queryable files."""
    listing = [_full(n) for n in ("a.nc", "b.nc")]
    _install_fs(monkeypatch, listing)

    result = logic.query_files_by_selector(
        **SELECTOR,
        query="SELECT * FROM output",
        index=0,
    )

    assert result.get("ok") is False
    assert result["error"]["code"] == "no_supported_files"


# ---------------------------------------------------------------------------
# Missing required selector args
# ---------------------------------------------------------------------------


def test_missing_model_returns_invalid_args(monkeypatch):
    selector_no_model = dict(SELECTOR)
    selector_no_model["model"] = None

    result = logic.query_files_by_selector(
        **selector_no_model,
        query="SELECT * FROM output",
    )

    assert result.get("ok") is False
    # _require pattern: existing error class
    assert "invalid_args" in result["error"]["code"] or "validation" in result["error"]["code"]
