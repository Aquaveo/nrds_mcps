
# Installs pinned Python dependencies into a venv. Build tooling stays here.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build deps for C-extension wheels not on PyPI (safety net; most wheels exist).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY nextgen_mcp/requirements.lock ./
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install -r requirements.lock


# Copies the venv from the builder. No build tooling. Non-root user.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9000 \
    MCP_TRANSPORT=streamable-http

# wget for HEALTHCHECK; ca-certificates for HTTPS to S3 etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system mcp \
    && useradd --system --gid mcp --home /app --shell /usr/sbin/nologin mcp

WORKDIR /app
COPY --from=builder /venv /venv
COPY --chown=mcp:mcp nextgen_mcp/ ./nextgen_mcp/

USER mcp

# Cloud Run's container filesystem is read-only except /tmp. DuckDB writes
# its extension cache to ~/.duckdb/extensions/ on first use of httpfs (or
# any extension), so the user's HOME must be writable. /app - the WORKDIR -
# is owned by root and not writable by the mcp user; pointing HOME at /tmp
# (Cloud Run's tmpfs) lets DuckDB create /tmp/.duckdb/extensions/ on demand.
# Extensions get re-downloaded on cold start, which is bounded (~5 MB for
# httpfs); pre-baking is a future optimization.
ENV HOME=/tmp

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget --quiet --spider --tries=1 \
        "http://127.0.0.1:${MCP_PORT}/health" || exit 1

ENTRYPOINT ["python", "-m", "nextgen_mcp.mcp_server"]
