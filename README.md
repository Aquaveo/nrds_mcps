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

## Test Deployment (Google Cloud Run)

A public test instance runs on Google Cloud Run, free tier:

| | |
|---|---|
| **URL** | https://nrds-mcps-43707369422.us-central1.run.app |
| **Health** | `GET /health` → `{"status":"ok"}` |
| **MCP SSE** | `GET /sse` |
| **Auth** | None (`--allow-unauthenticated`) |
| **GCP project** | `ibis-436806` |
| **Region** | `us-central1` |
| **Image** | `us-central1-docker.pkg.dev/ibis-436806/nrds-mcps/nrds-mcps:0.1.0` (mirrored from `ghcr.io/aquaveo/nrds-mcps:0.1.0` — Cloud Run does not pull from ghcr.io directly) |

### Quick test

```bash
URL=https://nrds-mcps-43707369422.us-central1.run.app

# Healthcheck
curl -fsS "$URL/health"          # → {"status":"ok"}

# MCP SSE handshake
curl -sN --max-time 5 -H "Accept: text/event-stream" "$URL/sse"
# → event: endpoint
#   data: /messages/?session_id=<uuid>

# Full MCP smoke from Python (requires fastmcp installed)
python -c "
import asyncio
from fastmcp import Client
async def smoke():
    async with Client('$URL/sse') as c:
        tools = await c.list_tools()
        print(f'tools: {len(tools)}')
        result = await c.call_tool('list_available_models', {})
        print(result)
asyncio.run(smoke())
"
```

### Redeploy procedure (when a new image tag lands)

The image must be mirrored into Artifact Registry first because Cloud Run does not pull from ghcr.io directly:

```bash
NEW_TAG=0.2.0  # replace with the actual release tag
docker pull "ghcr.io/aquaveo/nrds-mcps:$NEW_TAG"
docker tag "ghcr.io/aquaveo/nrds-mcps:$NEW_TAG" \
  "us-central1-docker.pkg.dev/ibis-436806/nrds-mcps/nrds-mcps:$NEW_TAG"
docker push "us-central1-docker.pkg.dev/ibis-436806/nrds-mcps/nrds-mcps:$NEW_TAG"

gcloud run deploy nrds-mcps \
  --image="us-central1-docker.pkg.dev/ibis-436806/nrds-mcps/nrds-mcps:$NEW_TAG" \
  --region=us-central1
```

Cloud Run does a zero-downtime traffic shift to the new revision.

### Logs

```bash
gcloud run services logs read nrds-mcps --region=us-central1 --limit=50
```

Or visit the [Cloud Run console](https://console.cloud.google.com/run/detail/us-central1/nrds-mcps/logs?project=ibis-436806).

### Service config (current)

- 2 GiB RAM, 1 vCPU, scale-to-zero, max-instances=2 (free-tier safety cap), 60-min request timeout, public unauthenticated ingress.
- No AWS credentials — the NRDS S3 bucket (`s3://ciroh-community-ngen-datastream`) is public; `boto3`/`s3fs` connect anonymously.
- Cold-start latency: ~3–5 s after scale-to-zero.

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
