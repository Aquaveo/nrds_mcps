# NRDS MCP Server

[Model Context Protocol](https://modelcontextprotocol.io) server exposing NRDS data tools — query S3-backed output files, list available models / dates / forecasts, create charts, and query hydrofabric data.

Built on [FastMCP](https://github.com/jlowin/fastmcp) with SSE transport.

## Quick Start (Docker)

Pull and run the image:

```bash
docker run --rm -d \
  --name nrds-mcps \
  -p 9000:9000 \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -e AWS_DEFAULT_REGION=us-east-1 \
  ghcr.io/aquaveo/nrds-mcps:latest
```

Verify it's running:

```bash
curl -fsS http://localhost:9000/health
# {"status":"ok"}
```

Connect an MCP client to `http://<host>:9000/sse`.

## Image Tags

| Tag | When to use |
|---|---|
| `:latest` | Most recent stable release. Mutates on every release; pinned consumers should use `:VERSION`. |
| `:0.1.0`, `:0.1.1`, ... | Immutable per-release tags. Recommended for production. |

Multi-arch: `linux/amd64`, `linux/arm64`.

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `9000` | Port the SSE server listens on. The container exposes 9000; map elsewhere on the host with `-p HOST:9000`. |
| `MCP_HOST` | `0.0.0.0` | Bind address inside the container. Rarely overridden. |
| `MCP_TRANSPORT` | `sse` | FastMCP transport. Stick with `sse` unless your client requires something else. |
| `NRDS_LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `ALLOWED_ORIGINS` | `*` | CORS allow-list, comma-separated. Set explicitly for production deployments behind a known origin. |
| `AWS_ACCESS_KEY_ID` | — | AWS credentials for S3 access. Inject at runtime; do not bake into the image. |
| `AWS_SECRET_ACCESS_KEY` | — | Companion to the above. |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region for S3 queries. |

For AWS, prefer instance/task IAM roles over static keys when running on AWS infrastructure.

## Healthcheck

The container exposes `GET /health` returning `200 {"status":"ok"}`. The Docker `HEALTHCHECK` directive polls this endpoint every 30 seconds; failures (3 consecutive) flip container health to `unhealthy`. Container orchestrators (Docker Swarm, Kubernetes, ECS) can use this for liveness probes.

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/sse` | GET | MCP SSE transport endpoint. Connect MCP clients here. |
| `/health` | GET | Liveness probe. |

The MCP tool surface (e.g., `list_available_models`, `query_outputs`, `create_chart`) is discovered automatically by MCP clients via SSE; consult the source for the full list.

## Local Development (no Docker)

For iterating on tool definitions or running outside a container, use the included setup script:

```bash
./scripts/setup-mcp.sh
```

This creates `.venv-mcp/`, installs `nextgen_mcp/requirements.txt`, and starts the server on port 9000. Override the port with `MCP_PORT=9001 ./scripts/setup-mcp.sh`.

## Source

- Repository: [github.com/Aquaveo/nrds_mcps](https://github.com/Aquaveo/nrds_mcps)
- Issues: [github.com/Aquaveo/nrds_mcps/issues](https://github.com/Aquaveo/nrds_mcps/issues)
- Changelog: [CHANGELOG.md](./CHANGELOG.md)

## License

MIT — see source repository for details.
