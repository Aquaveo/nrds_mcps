# NRDS MCP Server

[Model Context Protocol](https://modelcontextprotocol.io) server exposing **data-only** NRDS tools - query S3-backed output files, list available models / dates / forecasts, look up hydrofabric features. Pair with a host that owns rendering (charts, maps, tables); this server returns rows and metadata only and does not produce host-renderable payloads.

Built on [FastMCP](https://github.com/jlowin/fastmcp) with Streamable HTTP transport (default since v0.1.1; legacy SSE supported via `MCP_TRANSPORT=sse`).

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

Connect an MCP client to `http://<host>:9000/mcp` (Streamable HTTP transport, default since v0.1.1; legacy `/sse` returns 404).

## Image Tags

| Tag | When to use |
|---|---|
| `:latest` | Most recent stable release. Mutates on every release; pinned consumers should use `:VERSION`. |
| `:0.1.0`, `:0.1.1`, ... | Immutable per-release tags. Recommended for production. |

Multi-arch: `linux/amd64`, `linux/arm64`.

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `9000` | Port the server listens on. The container exposes 9000; map elsewhere on the host with `-p HOST:9000`. |
| `MCP_HOST` | `0.0.0.0` | Bind address inside the container. Rarely overridden. |
| `MCP_TRANSPORT` | `streamable-http` | FastMCP transport. Default `streamable-http` (path `/mcp`). Set to `sse` (path `/sse`) for legacy clients. |
| `NRDS_LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `ALLOWED_ORIGINS` | `*` | CORS allow-list, comma-separated. Set explicitly for production deployments behind a known origin. |
| `AWS_ACCESS_KEY_ID` | - | AWS credentials for S3 access. Inject at runtime; do not bake into the image. |
| `AWS_SECRET_ACCESS_KEY` | - | Companion to the above. |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region for S3 queries. |

For AWS, prefer instance/task IAM roles over static keys when running on AWS infrastructure.

## Healthcheck

The container exposes `GET /health` returning `200 {"status":"ok"}`. The Docker `HEALTHCHECK` directive polls this endpoint every 30 seconds; failures (3 consecutive) flip container health to `unhealthy`. Container orchestrators (Docker Swarm, Kubernetes, ECS) can use this for liveness probes.

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/mcp` | GET, POST | MCP Streamable HTTP transport endpoint (default since v0.1.1). Connect MCP clients here. |
| `/health` | GET | Liveness probe. |

The MCP tool surface (e.g., `list_available_models`, `query_output_file_from_output_selector`, `lookup_hydrofabric_feature`) is discovered automatically by MCP clients; consult the source for the full list. To render a chart or map from these results, chain into a host-side render tool - this server does not return Plotly figure JSON or map configurations.

## Test Deployment (Google Cloud Run)

A public test instance runs on Google Cloud Run, free tier:

| | |
|---|---|
| **URL** | https://nrds-mcps-43707369422.us-central1.run.app |
| **Health** | `GET /health` → `{"status":"ok"}` |
| **MCP endpoint** | `GET, POST /mcp` (Streamable HTTP, default since v0.1.1). The legacy `/sse` returns 404. |
| **Auth** | None (`--allow-unauthenticated`) |
| **GCP project** | `ibis-436806` |
| **Region** | `us-central1` |
| **Image** | `us-central1-docker.pkg.dev/ibis-436806/nrds-mcps-remote/aquaveo/nrds-mcps:0.1.0` (Artifact Registry **remote repository** transparently proxying `ghcr.io/aquaveo/nrds-mcps`; Cloud Run does not pull from ghcr.io directly, so the AR remote repo is the bridge) |

### Quick test

```bash
URL=https://nrds-mcps-43707369422.us-central1.run.app

# Healthcheck
curl -fsS "$URL/health"          # → {"status":"ok"}

# MCP initialize (Streamable HTTP transport)
curl -isS -X POST "$URL/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
  | head -20
# → 200 OK + mcp-session-id header + JSON-RPC initialize response

# Full MCP smoke from Python (requires fastmcp installed)
python -c "
import asyncio
from fastmcp import Client
async def smoke():
    async with Client('$URL/mcp') as c:
        tools = await c.list_tools()
        print(f'tools: {len(tools)}')
        result = await c.call_tool('list_available_models', {})
        print(result)
asyncio.run(smoke())
"
```

**Migration note (v0.1.0 → v0.1.1):** the legacy `/sse` URL returns 404 starting v0.1.1. Clients with hardcoded `/sse` URLs (e.g., tethysdash's saved MCP server config) must update to `/mcp`. chatbox-core's `pickTransport()` auto-detects from the URL suffix, so consumers only need to change the URL string.

### Redeploy procedure (automated)

Push a `v*` git tag - `release.yml` builds, pushes to ghcr.io, and deploys to Cloud Run automatically:

```bash
git tag -a v0.2.0 -m "..."
git push origin v0.2.0
```

The workflow:
1. Builds + pushes the multi-arch image to `ghcr.io/aquaveo/nrds-mcps:0.2.0` and `:latest`.
2. Polls ghcr.io until the manifest is visible (eventual-consistency guard).
3. Runs `gcloud run deploy` against `us-central1-docker.pkg.dev/ibis-436806/nrds-mcps-remote/aquaveo/nrds-mcps:0.2.0` (the AR remote repo proxies ghcr.io transparently).
4. Cloud Run's startup probe on `/health` gates traffic-shift - a buggy revision is provisioned but never gets traffic.
5. Workflow polls `/health` (120 s window) for forensic confirmation.
6. **Workflow probes MCP `tools/list` via FastMCP client** and asserts at least 10 tools registered. Catches tool-init crashes that leave `/health` green but the MCP tool registry empty or short. Threshold is configured in `release.yml` as `EXPECTED_MIN_TOOLS` - bump in lockstep when adding/removing tools.

If steps 1-5 fail, the previous revision keeps 100% traffic - **failed deploys are non-destructive** at the traffic-shift level. Step 6 (MCP smoke) runs *after* traffic has already flipped, so a failure there means the new revision is broken AND receiving traffic. Recovery is manual rollback: `gcloud run services update-traffic nrds-mcps --region=us-central1 --to-revisions=<previous-revision>=100`.

#### Re-deploy an existing tag

Use the GitHub Actions UI: **Actions → Release → Run workflow**, supply the tag (e.g., `0.1.0`), click Run. The workflow skips the build job and goes straight to deploy + smoke.

#### Manual fallback (CI broken / out-of-band hotfix)

```bash
TAG=0.2.0
gcloud run deploy nrds-mcps \
  --image="us-central1-docker.pkg.dev/ibis-436806/nrds-mcps-remote/aquaveo/nrds-mcps:$TAG" \
  --region=us-central1
```

**When NOT to "just push the tag":** if the new release also requires Cloud Run config changes (env vars, runtime SA, IAM roles, ingress settings), the YAML's hard-coded flags will only deploy what's listed. Coordinate config and code together - either update the workflow YAML in the same PR as the code change, or do a manual `gcloud run services update` after.

#### Service config lives in the workflow YAML

`release.yml`'s deploy step explicitly passes all Cloud Run flags (memory, cpu, instances, env vars, ingress). **Console edits will be silently reset on the next CI deploy.** If you need to adjust config, update `.github/workflows/release.yml`, not the console.

### Logs

```bash
gcloud run services logs read nrds-mcps --region=us-central1 --limit=50
```

Or visit the [Cloud Run console](https://console.cloud.google.com/run/detail/us-central1/nrds-mcps/logs?project=ibis-436806).

### Service config (current)

- 2 GiB RAM, 1 vCPU, scale-to-zero, max-instances=2 (free-tier safety cap), 60-min request timeout, public unauthenticated ingress.
- No AWS credentials - the NRDS S3 bucket (`s3://ciroh-community-ngen-datastream`) is public; `boto3`/`s3fs` connect anonymously.
- Cold-start latency: ~3-5 s after scale-to-zero.

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

MIT - see source repository for details.
