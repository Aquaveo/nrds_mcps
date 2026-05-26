"""Centralized IO configuration for nrds_mcps - timeouts and helper factories.

All outbound network IO (S3 via fsspec, DuckDB httpfs) goes through helpers
in this module so a single ``NRDS_HTTP_TIMEOUT_SECONDS`` env var controls
every IO layer's per-request budget.

Conventions:
- Default 60s, intentionally permissive ("don't break working flows").
- ``<=0`` env values fall back to default with a warning (defends against
  IaC pipelines inheriting misconfigured parent envs).
- fsspec.filesystem is called with ``skip_instance_cache=True`` so an
  earlier unconfigured instantiation can't be served back and silently
  drop the timeout config.
- botocore retries are disabled (``max_attempts=1``) so the per-request
  budget isn't multiplied by the default 3 retry attempts.
- DuckDB http_timeout is in SECONDS (not milliseconds - DuckDB 1.x docs).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import duckdb
import fsspec

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_DUCKDB_MEMORY_LIMIT_MB = 512

# Hydrofabric index parquet - the canonical lookup table for flowpath /
# divide / etc. feature metadata. Single source of truth here so the
# rest-shim and the utils_rest helpers stay in sync.
HYDROFABRIC_INDEX_URL = (
    "https://communityhydrofabric.s3.us-east-1.amazonaws.com/map/hydrofabric_index.parquet"
)


def _read_timeout_env() -> int:
    """Read NRDS_HTTP_TIMEOUT_SECONDS with a <=0 guard and parse-error fallback."""
    raw = os.getenv("NRDS_HTTP_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "NRDS_HTTP_TIMEOUT_SECONDS=%r is not a valid integer; using default %ds",
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            "NRDS_HTTP_TIMEOUT_SECONDS=%d is <=0; using default %ds",
            value,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    return value


HTTP_TIMEOUT_SECONDS: int = _read_timeout_env()


def _read_memory_limit_env() -> int:
    raw = os.getenv("NRDS_DUCKDB_MEMORY_LIMIT_MB", str(_DEFAULT_DUCKDB_MEMORY_LIMIT_MB))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "NRDS_DUCKDB_MEMORY_LIMIT_MB=%r is not a valid integer; using default %dMB",
            raw,
            _DEFAULT_DUCKDB_MEMORY_LIMIT_MB,
        )
        return _DEFAULT_DUCKDB_MEMORY_LIMIT_MB
    if value <= 0:
        logger.warning(
            "NRDS_DUCKDB_MEMORY_LIMIT_MB=%d is <=0; using default %dMB",
            value,
            _DEFAULT_DUCKDB_MEMORY_LIMIT_MB,
        )
        return _DEFAULT_DUCKDB_MEMORY_LIMIT_MB
    return value


DUCKDB_MEMORY_LIMIT_MB: int = _read_memory_limit_env()


def _s3fs_config_kwargs() -> dict:
    """Build config_kwargs for s3fs / fsspec("s3", ...) - passed directly
    to botocore.client.Config(...) by s3fs.

    Cannot use ``client_kwargs={"config": Config(...)}`` because s3fs
    already passes a `config=` kwarg internally to
    `session.create_client(...)`; supplying our own collides with
    `TypeError: got multiple values for keyword argument 'config'`.

    ``config_kwargs`` is the s3fs-supported alternative.

    Disabling retries (``max_attempts=1``) ensures the per-request budget
    isn't multiplied by the default 3 retry attempts - a 60s timeout means
    60s, not potentially 180s.
    """
    return {
        "connect_timeout": HTTP_TIMEOUT_SECONDS,
        "read_timeout": HTTP_TIMEOUT_SECONDS,
        "retries": {"max_attempts": 1},
    }


def s3_filesystem() -> Any:
    """Return a timeout-configured anonymous S3 fsspec filesystem.

    ``skip_instance_cache=True`` is load-bearing: fsspec caches filesystem
    instances by args-hash; if any prior code path called
    ``fsspec.filesystem("s3", anon=True)`` without config_kwargs, that
    instance would be returned for subsequent timeout-configured calls,
    silently dropping the config. Per-call instantiation guarantees the
    timeout reaches the underlying transport.
    """
    return fsspec.filesystem(
        "s3",
        anon=True,
        config_kwargs=_s3fs_config_kwargs(),
        skip_instance_cache=True,
    )


def duckdb_connect_with_httpfs(database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection with httpfs loaded and http_timeout set.

    ``SET http_timeout = N`` is in SECONDS (verified against DuckDB 1.x
    settings docs - `'HTTP timeout read/write/connection/retry (in seconds)'`).
    A previous plan draft had ``* 1000`` (treating it as milliseconds);
    that was wrong and would have configured an 8.3-hour effective timeout.
    """
    con = duckdb.connect(database=database)
    try:
        con.execute("LOAD httpfs")
    except Exception:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
    con.execute(f"SET http_timeout = {HTTP_TIMEOUT_SECONDS}")
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT_MB}MB'")
    con.execute("SET threads = 2")
    return con


