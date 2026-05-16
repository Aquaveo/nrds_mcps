# nextgen_mcp

Source for the NRDS MCP server. For deployment, Docker, env-var, healthcheck,
and Cloud Run documentation see the [top-level README](../README.md).

## Local development (no Docker)

From the repository root:

```bash
./scripts/setup-mcp.sh
```

Creates `.venv-mcp/`, installs `nextgen_mcp/requirements.txt`, and starts the
server on `http://0.0.0.0:9000/mcp` (Streamable HTTP transport, default since
v0.1.1). Override the port with `MCP_PORT=9001 ./scripts/setup-mcp.sh`.

### Setup only / run only

```bash
./scripts/setup-mcp.sh --setup   # install deps, do not start
./scripts/setup-mcp.sh --run     # skip install, start server
```

### Manual

```bash
python3 -m venv .venv-mcp
source .venv-mcp/bin/activate
pip install -r nextgen_mcp/requirements.txt
python -m nextgen_mcp.mcp_server
```

## Connecting an MCP client

With the server running on `http://localhost:9000`, point any MCP client at
`http://localhost:9000/mcp` (Streamable HTTP). Examples:

**Claude Code CLI:**

```bash
claude mcp add nrds --transport http http://localhost:9000/mcp
claude mcp list      # verify
claude mcp remove nrds
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "nrds": {
      "type": "http",
      "url": "http://localhost:9000/mcp"
    }
  }
}
```

Legacy `/sse` is supported by setting `MCP_TRANSPORT=sse` before starting the
server; clients then connect to `http://localhost:9000/sse`.

## Environment variables

The server-relevant variables are documented in the
[top-level README](../README.md#configuration-env-vars). The S3 bucket is
public (`s3://ciroh-community-ngen-datastream`), so AWS credentials are
optional in most local-dev scenarios — `boto3`/`s3fs` connect anonymously
when no credentials are present.

## Tools

The MCP tool surface is discovered automatically by clients via `tools/list`.
The current 10 tools cover:

- Discovery: `list_available_models`, `list_available_dates`,
  `list_available_forecasts`, `list_available_cycles`, `list_available_vpus`,
  `list_available_output_files`
- Resolution: `resolve_output_file`
- Query: `query_output_file`, `query_output_file_from_output_selector`
- Hydrofabric: `lookup_hydrofabric_feature`

All tools return data only — no Plotly figure JSON or map config blobs.
Charts and maps are the host's responsibility.

## Project structure

```
nextgen_mcp/
  __init__.py
  mcp_server.py       # Server entry point
  _mcp.py             # FastMCP instance, middleware registration, /health route
  _io_config.py       # S3 filesystem + URL constants
  tools.py            # @mcp.tool definitions
  prompts.py          # @mcp.prompt slash-command templates
  logic.py            # Core data logic (S3 listing, DuckDB queries)
  utils.py            # Tool-body helpers — date parsing, validation guards,
                      # id/label normalization, payload reshaping
  utils_rest.py       # DuckDB / parquet / netCDF helpers, error classifiers
  validation.py       # Pydantic models, Literal types, normalize_* helpers
  middleware/
    _input_validation_middleware.py
    _observability_middleware.py
  requirements.txt
  requirements.lock
  README.md           # this file
```
