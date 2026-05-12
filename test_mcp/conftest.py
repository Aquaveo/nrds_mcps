"""Shared pytest fixtures for nrds_mcps tests.

Fault-injection fixtures monkeypatch the IO helpers in
``nextgen_mcp._io_config`` so tests can deterministically simulate
slow/failing S3 without hitting live infrastructure.

The fixtures patch the helper functions (``s3_filesystem``,
``duckdb_connect_with_httpfs``, ``open_fsspec_file``) at their
import sites in ``rest.py`` and ``utils_rest.py`` — not the underlying
``fsspec`` / ``duckdb`` / ``xarray`` modules — because the production
code imports the helpers, not the underlying modules. Patching at the
helper layer guarantees every IO site is covered.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from nextgen_mcp import logic
import pytest


class _MockFsspecFilesystem:
    """Stand-in for an fsspec S3 filesystem that raises on every operation.

    Configured via ``raise_with`` — an exception instance to raise on
    each method call. Mirrors the public surface used by ``rest.py``
    (``ls`` is the primary call; ``exists``, ``open`` etc. are stubbed
    out for completeness).
    """

    def __init__(self, raise_with: Exception):
        self._raise = raise_with

    def ls(self, *args: Any, **kwargs: Any):
        raise self._raise

    def exists(self, *args: Any, **kwargs: Any):
        raise self._raise

    def open(self, *args: Any, **kwargs: Any):
        raise self._raise


def _install_fsspec_fault(monkeypatch: pytest.MonkeyPatch, exc: Exception):
    """Patch s3_filesystem() in both rest.py and _io_config.py to raise exc."""
    from nextgen_mcp import _io_config

    def _factory():
        return _MockFsspecFilesystem(exc)

    monkeypatch.setattr(_io_config, "s3_filesystem", _factory)
    monkeypatch.setattr(logic, "s3_filesystem", _factory)


@pytest.fixture
def mock_fsspec_timeout(monkeypatch: pytest.MonkeyPatch):
    """fs.ls raises TimeoutError — simulates a hung S3 read that hit the budget."""
    _install_fsspec_fault(monkeypatch, TimeoutError("simulated read timeout"))


@pytest.fixture
def mock_fsspec_permission_denied(monkeypatch: pytest.MonkeyPatch):
    """fs.ls raises PermissionError — simulates AccessDenied on anonymous S3."""
    _install_fsspec_fault(monkeypatch, PermissionError("simulated access denied"))


@pytest.fixture
def mock_fsspec_connection_error(monkeypatch: pytest.MonkeyPatch):
    """fs.ls raises ConnectionError — simulates network-layer failure."""
    _install_fsspec_fault(
        monkeypatch, ConnectionError("simulated connection error")
    )


@pytest.fixture
def mock_fsspec_not_found(monkeypatch: pytest.MonkeyPatch):
    """fs.ls raises FileNotFoundError — simulates a missing S3 prefix."""
    _install_fsspec_fault(monkeypatch, FileNotFoundError("simulated not found"))


@pytest.fixture
def mock_fsspec_empty_ls(monkeypatch: pytest.MonkeyPatch):
    """fs.ls returns []. Useful when a test needs the IO call to succeed
    (so the surrounding code runs) but shouldn't hit live S3.
    """
    from nextgen_mcp import _io_config

    class _OkFS:
        def ls(self, *args, **kwargs):
            return []

        def exists(self, *args, **kwargs):
            return False

    def _factory():
        return _OkFS()

    monkeypatch.setattr(_io_config, "s3_filesystem", _factory)
    monkeypatch.setattr(logic, "s3_filesystem", _factory)


@pytest.fixture
def mock_fsspec_botocore_client_error(monkeypatch: pytest.MonkeyPatch):
    """fs.ls raises botocore.exceptions.ClientError — simulates an AWS API error."""
    from botocore.exceptions import ClientError

    err = ClientError(
        error_response={
            "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
            "ResponseMetadata": {"HTTPStatusCode": 503},
        },
        operation_name="ListObjects",
    )
    _install_fsspec_fault(monkeypatch, err)


@pytest.fixture
def mock_duckdb_connect_io_error(monkeypatch: pytest.MonkeyPatch):
    """duckdb_connect_with_httpfs's first execute() raises duckdb.IOException.

    Simulates an S3/httpfs failure inside a DuckDB query.
    """
    import duckdb
    from nextgen_mcp import _io_config, utils_rest

    def _factory(database: str = ":memory:"):
        con = MagicMock()
        con.execute.side_effect = duckdb.IOException("simulated httpfs failure")
        con.sql.side_effect = duckdb.IOException("simulated httpfs failure")
        return con

    monkeypatch.setattr(_io_config, "duckdb_connect_with_httpfs", _factory)
    monkeypatch.setattr(utils_rest, "duckdb_connect_with_httpfs", _factory)


@pytest.fixture
def mock_duckdb_binder_error(monkeypatch: pytest.MonkeyPatch):
    """DuckDB query raises BinderException — programmer error class.

    These MUST be re-raised, not caught as execution_error, per the plan.
    """
    import duckdb
    from nextgen_mcp import _io_config, utils_rest

    def _factory(database: str = ":memory:"):
        con = MagicMock()
        con.execute.side_effect = duckdb.BinderException(
            "simulated programmer error: unknown column"
        )
        con.sql.side_effect = duckdb.BinderException(
            "simulated programmer error: unknown column"
        )
        return con

    monkeypatch.setattr(_io_config, "duckdb_connect_with_httpfs", _factory)
    monkeypatch.setattr(utils_rest, "duckdb_connect_with_httpfs", _factory)
