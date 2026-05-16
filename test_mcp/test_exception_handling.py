"""Tests for broadened exception handling on S3/DuckDB IO paths (Unit 4).

Verifies each IO-bearing tool returns a structured error envelope with the
right code + sanitized message + fix_hint when its underlying IO layer
raises one of:
  - TimeoutError -> code=timeout
  - PermissionError -> code=permission_denied
  - ConnectionError -> code=upstream_error
  - botocore.ClientError -> code=upstream_error / permission_denied / not_found
    depending on AWS error code
  - duckdb.IOException -> code=upstream_error
  - duckdb.BinderException -> RE-RAISED, not classified

And that the exception message is sanitized (no AWS account IDs, no full
S3 URLs, no presigned URL tokens leak into the LLM-facing envelope).
"""
from __future__ import annotations

from nextgen_mcp import logic, logic
from nextgen_mcp.utils_rest import (
    _classify_io_error,
    _is_duckdb_programmer_error,
    _IO_ERROR_CATALOG,
)


# ---------------------------------------------------------------------------
# _classify_io_error - code + sanitized message + fix_hint per exception class
# ---------------------------------------------------------------------------


def test_classify_timeout_error():
    code, msg, fix_hint = _classify_io_error(TimeoutError("connect timeout"))
    assert code == "timeout"
    assert "timed out" in msg.lower()
    assert "retry the same call once" in fix_hint.lower()


def test_classify_permission_error():
    code, msg, _ = _classify_io_error(PermissionError("denied"))
    assert code == "permission_denied"
    assert "denied" in msg.lower() or "access" in msg.lower()


def test_classify_file_not_found_error():
    code, _, fix_hint = _classify_io_error(FileNotFoundError("nope"))
    assert code == "not_found"
    assert "do not retry" in fix_hint.lower()


def test_classify_connection_error():
    code, _, _ = _classify_io_error(ConnectionError("broken pipe"))
    assert code == "upstream_error"


def test_classify_botocore_access_denied():
    from botocore.exceptions import ClientError

    err = ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "..."}},
        operation_name="ListObjects",
    )
    code, _, _ = _classify_io_error(err)
    assert code == "permission_denied"


def test_classify_botocore_no_such_key():
    from botocore.exceptions import ClientError

    err = ClientError(
        error_response={"Error": {"Code": "NoSuchKey", "Message": "..."}},
        operation_name="GetObject",
    )
    code, _, _ = _classify_io_error(err)
    assert code == "not_found"


def test_classify_botocore_throttling():
    from botocore.exceptions import ClientError

    err = ClientError(
        error_response={"Error": {"Code": "Throttling", "Message": "Slow down"}},
        operation_name="ListObjects",
    )
    code, _, _ = _classify_io_error(err)
    assert code == "upstream_error"


def test_classify_duckdb_io_exception():
    import duckdb

    code, _, _ = _classify_io_error(duckdb.IOException("httpfs read failed"))
    assert code == "upstream_error"


def test_classify_generic_oserror():
    """OSError parent covers filesystem classes not specifically pinned."""
    code, _, _ = _classify_io_error(OSError("some os error"))
    assert code == "upstream_error"


def test_classify_unknown_exception_falls_back_to_execution_error():
    code, msg, fix_hint = _classify_io_error(RuntimeError("?"))
    assert code == "execution_error"
    assert "do not retry" in fix_hint.lower()


# ---------------------------------------------------------------------------
# Sanitization - envelope messages NEVER include str(exc)
# ---------------------------------------------------------------------------


def test_classify_does_not_leak_exception_str():
    """The sanitized message must not contain the raw exception text.

    Raw boto/duckdb error messages can embed AWS account IDs, bucket names,
    full S3 URLs with presigned tokens - none of that belongs in the
    LLM-facing envelope.
    """
    from botocore.exceptions import ClientError

    err = ClientError(
        error_response={
            "Error": {
                "Code": "AccessDenied",
                "Message": "arn:aws:s3:::SECRET-BUCKET-123456789012 forbidden",
            },
        },
        operation_name="ListObjects",
    )
    _, msg, _ = _classify_io_error(err)
    assert "SECRET-BUCKET-123456789012" not in msg
    assert "arn:aws" not in msg


# ---------------------------------------------------------------------------
# Programmer-error re-raise guard
# ---------------------------------------------------------------------------


def test_is_duckdb_programmer_error_true_for_binder():
    import duckdb

    assert _is_duckdb_programmer_error(duckdb.BinderException("?"))


def test_is_duckdb_programmer_error_true_for_parser():
    import duckdb

    assert _is_duckdb_programmer_error(duckdb.ParserException("?"))


def test_is_duckdb_programmer_error_true_for_catalog():
    import duckdb

    assert _is_duckdb_programmer_error(duckdb.CatalogException("?"))


def test_is_duckdb_programmer_error_false_for_io():
    import duckdb

    assert not _is_duckdb_programmer_error(duckdb.IOException("?"))


def test_is_duckdb_programmer_error_false_for_generic():
    assert not _is_duckdb_programmer_error(OSError("?"))


# ---------------------------------------------------------------------------
# Catalog - all 5 codes have a sanitized message + fix_hint
# ---------------------------------------------------------------------------


def test_all_codes_have_sanitized_message_and_fix_hint():
    expected_codes = {
        "timeout",
        "permission_denied",
        "upstream_error",
        "not_found",
        "execution_error",
    }
    assert set(_IO_ERROR_CATALOG.keys()) == expected_codes
    for code, (msg, fix_hint) in _IO_ERROR_CATALOG.items():
        assert isinstance(msg, str) and msg.strip(), f"{code} has empty message"
        assert isinstance(fix_hint, str) and fix_hint.strip(), (
            f"{code} has empty fix_hint"
        )


# ---------------------------------------------------------------------------
# Integration - IO-bearing tools return structured envelope on each fault class
# ---------------------------------------------------------------------------


def test_list_available_models_returns_envelope_on_permission_denied(
    mock_fsspec_permission_denied,
):
    result = logic.list_available_models()
    assert result.get("ok") is False
    assert result["error"]["code"] == "permission_denied"
    assert "fix_hint" in result
    assert "access denied" in result["error"]["message"].lower()


def test_list_available_models_returns_envelope_on_timeout(mock_fsspec_timeout):
    result = logic.list_available_models()
    assert result.get("ok") is False
    assert result["error"]["code"] == "timeout"
    assert "retry the same call once" in result["fix_hint"].lower()


def test_list_available_models_returns_envelope_on_connection_error(
    mock_fsspec_connection_error,
):
    result = logic.list_available_models()
    assert result.get("ok") is False
    assert result["error"]["code"] == "upstream_error"


def test_list_available_models_returns_envelope_on_botocore_client_error(
    mock_fsspec_botocore_client_error,
):
    result = logic.list_available_models()
    assert result.get("ok") is False
    # Throttling maps to upstream_error
    assert result["error"]["code"] == "upstream_error"


def test_list_available_models_returns_empty_on_not_found(mock_fsspec_not_found):
    """FileNotFoundError preserves the existing benign-empty payload behavior."""
    result = logic.list_available_models()
    # Benign no-results path - _list_payload returns ok-shape with empty list
    assert "models" in result
    assert result["models"] == []


def test_query_hydrofabric_returns_envelope_on_duckdb_io_error(
    mock_duckdb_connect_io_error,
):
    """DuckDB IOException -> upstream_error envelope."""
    result = logic.lookup_hydrofabric_feature("wb-1019290")
    assert result.get("ok") is False
    assert result["error"]["code"] == "upstream_error"
    assert "fix_hint" in result


def test_query_hydrofabric_reraises_duckdb_binder_error(mock_duckdb_binder_error):
    """DuckDB BinderException (programmer error) MUST propagate as exception,
    not be normalized to execution_error.

    Catching it would let the LLM retry the same broken query forever.
    """
    import duckdb
    import pytest as _pytest

    with _pytest.raises(duckdb.BinderException):
        logic.lookup_hydrofabric_feature("wb-1019290")


# ---------------------------------------------------------------------------
# Validation-before-IO - get_output_file's idx < 0 check fires without S3
# ---------------------------------------------------------------------------


def test_get_output_file_negative_index_fails_before_s3_call(monkeypatch):
    """A negative index returns bad_request WITHOUT invoking fs.ls."""
    ls_call_count = {"n": 0}

    class _CountingFS:
        def ls(self, *args, **kwargs):
            ls_call_count["n"] += 1
            return []

    from nextgen_mcp import _io_config

    monkeypatch.setattr(_io_config, "s3_filesystem", lambda: _CountingFS())
    monkeypatch.setattr(logic, "s3_filesystem", lambda: _CountingFS())

    result = logic.get_output_file(
        model="cfe_nom",
        date="2026-05-01",
        forecast="medium_range",
        cycle="00",
        vpu="06",
        index=-1,
    )
    assert result.get("ok") is False
    assert result["error"]["code"] == "bad_request"
    assert ls_call_count["n"] == 0, (
        "negative index should fail BEFORE the S3 ls call"
    )


# ---------------------------------------------------------------------------
# LLM-supplied SQL error handling - invalid_query envelope with column list
# ---------------------------------------------------------------------------


def test_extract_duckdb_candidates_pulls_columns_from_message():
    """Pulls 'output.feature_id', 'output.velocity' from a BinderException
    message and normalizes them to bare column names.
    """
    from nextgen_mcp.utils_rest import _extract_duckdb_candidates

    msg = (
        'Binder Error: Referenced column "variable" not found in FROM clause!\n'
        'Candidate bindings: "output.feature_id", "output.velocity"'
    )
    cols = _extract_duckdb_candidates(msg)
    assert cols == ["feature_id", "velocity"]


def test_extract_duckdb_candidates_handles_bare_names():
    """Column candidates without table qualifier are kept as-is."""
    from nextgen_mcp.utils_rest import _extract_duckdb_candidates

    msg = 'Binder Error: ...\nCandidate bindings: "time", "flow"'
    cols = _extract_duckdb_candidates(msg)
    assert cols == ["time", "flow"]


def test_extract_duckdb_candidates_returns_empty_on_no_match():
    """If the message has no Candidate bindings clause, returns []."""
    from nextgen_mcp.utils_rest import _extract_duckdb_candidates

    assert _extract_duckdb_candidates("some other error") == []


def test_classify_llm_sql_error_binder_returns_invalid_query_with_columns():
    """BinderException -> code=invalid_query with available_columns extracted."""
    import duckdb

    from nextgen_mcp.utils_rest import _classify_llm_sql_error

    exc = duckdb.BinderException(
        'Referenced column "variable" not found in FROM clause!\n'
        'Candidate bindings: "output.feature_id", "output.velocity"'
    )
    code, msg, fix_hint, cols = _classify_llm_sql_error(
        exc, "s3://x/y.parquet", "SELECT * WHERE variable = 'velocity'"
    )
    assert code == "invalid_query"
    assert "does not exist" in msg
    assert "feature_id" in fix_hint and "velocity" in fix_hint
    assert "retry once" in fix_hint.lower()
    assert cols == ["feature_id", "velocity"]


def test_classify_llm_sql_error_parser_returns_invalid_query():
    """ParserException -> code=invalid_query with sql-syntax fix_hint."""
    import duckdb

    from nextgen_mcp.utils_rest import _classify_llm_sql_error

    exc = duckdb.ParserException("syntax error at end of input")
    code, msg, fix_hint, cols = _classify_llm_sql_error(
        exc, "s3://x/y.parquet", "SELEC * FROM"
    )
    assert code == "invalid_query"
    assert "syntax" in msg.lower() or "valid" in msg.lower()
    assert "single SELECT" in fix_hint
    assert cols == []


def test_classify_llm_sql_error_catalog_returns_invalid_query():
    """CatalogException -> code=invalid_query with table-not-found fix_hint."""
    import duckdb

    from nextgen_mcp.utils_rest import _classify_llm_sql_error

    exc = duckdb.CatalogException("Table 'bogus' does not exist")
    code, _, fix_hint, _ = _classify_llm_sql_error(
        exc, "s3://x/y.parquet", "SELECT * FROM bogus"
    )
    assert code == "invalid_query"
    assert "FROM output" in fix_hint


def test_classify_llm_sql_error_falls_back_for_non_programmer_errors():
    """A generic OSError on the LLM-SQL path falls back to _classify_io_error
    behavior so callers still get a typed result.
    """
    from nextgen_mcp.utils_rest import _classify_llm_sql_error

    code, msg, fix_hint, cols = _classify_llm_sql_error(
        TimeoutError("upstream"), "s3://x/y.parquet", "SELECT 1"
    )
    assert code == "timeout"
    assert cols == []


def test_query_output_file_returns_envelope_on_binder_exception(monkeypatch):
    """Integration: when DuckDB raises BinderException against an LLM-supplied
    query, the tool returns a structured envelope with available_columns +
    fix_hint instead of letting the exception bubble.

    This is the recovery path qwen needed in the 2026-05-10 bug - the LLM
    gets the actual column list and can rewrite its query in one retry.
    """
    import duckdb

    from nextgen_mcp import utils_rest

    def _raise_binder(file_url, query):
        raise duckdb.BinderException(
            'Referenced column "variable" not found in FROM clause!\n'
            'Candidate bindings: "output.feature_id", "output.velocity"'
        )

    monkeypatch.setattr(utils_rest, "_duckdb_query_parquet", _raise_binder)
    monkeypatch.setattr(logic, "_duckdb_query_parquet", _raise_binder)

    result = logic.query_output_file(
        s3_url="s3://ciroh-community-ngen-datastream/outputs/x/y.parquet",
        query="SELECT * FROM output WHERE variable = 'velocity'",
    )
    assert result.get("ok") is False
    assert result["error"]["code"] == "invalid_query"
    assert result["available_columns"] == ["feature_id", "velocity"]
    assert "feature_id" in result["fix_hint"]
    assert "retry once" in result["fix_hint"].lower()


def test_get_output_file_oversize_index_still_requires_s3(monkeypatch):
    """An oversize index (e.g. 999) still needs fs.ls to compare against
    len(items) - that upper-bound check stays after IO (documented in plan).
    """
    ls_call_count = {"n": 0}

    class _CountingFS:
        def ls(self, *args, **kwargs):
            ls_call_count["n"] += 1
            return []

    from nextgen_mcp import _io_config

    monkeypatch.setattr(_io_config, "s3_filesystem", lambda: _CountingFS())
    monkeypatch.setattr(logic, "s3_filesystem", lambda: _CountingFS())

    logic.get_output_file(
        model="cfe_nom",
        date="2026-05-01",
        forecast="medium_range",
        cycle="00",
        vpu="06",
        index=999,
    )
    assert ls_call_count["n"] == 1, (
        "oversize index check requires len(items) and thus a fs.ls call"
    )
