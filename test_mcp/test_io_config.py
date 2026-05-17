"""Tests for nextgen_mcp/_io_config.py - timeout config, helpers, env guards."""
from __future__ import annotations

import importlib

import pytest


def _reload_io_config():
    """Reload _io_config so it re-reads NRDS_HTTP_TIMEOUT_SECONDS at import."""
    from nextgen_mcp import _io_config
    return importlib.reload(_io_config)


# ---------------------------------------------------------------------------
# Env var read + <=0 guard + parse-error fallback
# ---------------------------------------------------------------------------


def test_default_timeout_is_60_seconds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NRDS_HTTP_TIMEOUT_SECONDS", raising=False)
    cfg = _reload_io_config()
    assert cfg.HTTP_TIMEOUT_SECONDS == 60


def test_env_override_is_honored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "15")
    cfg = _reload_io_config()
    assert cfg.HTTP_TIMEOUT_SECONDS == 15


def test_zero_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "0")
    with caplog.at_level("WARNING"):
        cfg = _reload_io_config()
    assert cfg.HTTP_TIMEOUT_SECONDS == 60
    assert any("<=0" in r.message for r in caplog.records)


def test_negative_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "-5")
    with caplog.at_level("WARNING"):
        cfg = _reload_io_config()
    assert cfg.HTTP_TIMEOUT_SECONDS == 60


def test_non_integer_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "not-a-number")
    with caplog.at_level("WARNING"):
        cfg = _reload_io_config()
    assert cfg.HTTP_TIMEOUT_SECONDS == 60
    assert any("not a valid integer" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# boto3 Config shape - connect_timeout, read_timeout, retries disabled
# ---------------------------------------------------------------------------


def test_s3fs_config_kwargs_carries_timeouts_and_no_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    """config_kwargs (not client_kwargs={'config': Config(...)}) is the right
    shape - s3fs already passes a config kwarg internally, so layering ours
    on top causes 'got multiple values for keyword argument config'.
    config_kwargs is passed by s3fs directly to botocore Config(...).
    """
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "20")
    cfg = _reload_io_config()
    kwargs = cfg._s3fs_config_kwargs()

    assert kwargs["connect_timeout"] == 20
    assert kwargs["read_timeout"] == 20
    # Retries disabled so the per-request budget isn't multiplied by 3
    assert kwargs["retries"] == {"max_attempts": 1}


# ---------------------------------------------------------------------------
# s3_filesystem() - skip_instance_cache + client_kwargs propagation
# ---------------------------------------------------------------------------


def test_s3_filesystem_uses_anon_and_skip_instance_cache():
    """The fsspec filesystem is built with anon=True + skip_instance_cache.

    skip_instance_cache is load-bearing: it ensures the timeout-configured
    instance isn't replaced by a cached unconfigured one from earlier code.
    """
    from nextgen_mcp import _io_config

    fs = _io_config.s3_filesystem()
    # fsspec's S3FileSystem exposes anon as `_anon` or `anon`; tolerate either
    assert getattr(fs, "anon", getattr(fs, "_anon", False)) is True


def test_s3_filesystem_returns_distinct_instances_per_call():
    """With skip_instance_cache=True, repeated calls produce fresh instances.

    Without this, a stale cached filesystem could be returned and silently
    drop the per-call timeout config - exactly the bug the auto-fix
    addresses.
    """
    from nextgen_mcp import _io_config

    fs1 = _io_config.s3_filesystem()
    fs2 = _io_config.s3_filesystem()
    assert fs1 is not fs2


# ---------------------------------------------------------------------------
# duckdb_connect_with_httpfs() - http_timeout is in SECONDS, not milliseconds
# ---------------------------------------------------------------------------


def test_duckdb_connect_sets_http_timeout_in_seconds(monkeypatch: pytest.MonkeyPatch):
    """DuckDB 1.x http_timeout is in seconds.

    A prior plan draft used `* 1000` (treating it as milliseconds) - that
    would have produced an 8.3-hour effective timeout. This test pins the
    correct unit.
    """
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "25")
    cfg = _reload_io_config()
    con = cfg.duckdb_connect_with_httpfs()
    try:
        result = con.execute("SELECT current_setting('http_timeout')").fetchone()
        assert result is not None
        # DuckDB returns the setting value as an int (25 seconds).
        # A previous draft used `* 1000` (treating it as ms) - that would
        # have set 25000 here. This pins the correct unit.
        assert int(result[0]) == 25
    finally:
        con.close()


def test_duckdb_connect_loads_httpfs_extension(monkeypatch: pytest.MonkeyPatch):
    """The httpfs extension is loaded after duckdb_connect_with_httpfs."""
    monkeypatch.setenv("NRDS_HTTP_TIMEOUT_SECONDS", "30")
    cfg = _reload_io_config()
    con = cfg.duckdb_connect_with_httpfs()
    try:
        # If httpfs is loaded, SELECT against the extensions list succeeds
        result = con.execute(
            "SELECT loaded FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
        ).fetchone()
        assert result is not None
        assert result[0] is True
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Fault-injection: fixtures correctly redirect IO to raise the configured exc
# ---------------------------------------------------------------------------


def test_mock_fsspec_timeout_fixture_raises(mock_fsspec_timeout):
    """The mock_fsspec_timeout fixture makes s3_filesystem().ls raise TimeoutError."""
    from nextgen_mcp._io_config import s3_filesystem

    fs = s3_filesystem()
    with pytest.raises(TimeoutError, match="simulated read timeout"):
        fs.ls("s3://anything")


def test_mock_fsspec_permission_denied_fixture_raises(mock_fsspec_permission_denied):
    from nextgen_mcp._io_config import s3_filesystem

    fs = s3_filesystem()
    with pytest.raises(PermissionError):
        fs.ls("s3://anything")


def test_mock_fsspec_botocore_client_error_fixture_raises(
    mock_fsspec_botocore_client_error,
):
    from botocore.exceptions import ClientError

    from nextgen_mcp._io_config import s3_filesystem

    fs = s3_filesystem()
    with pytest.raises(ClientError):
        fs.ls("s3://anything")
