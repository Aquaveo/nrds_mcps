# NRDS MCP Server

Model Context Protocol (MCP) server that exposes NRDS data tools — query output files, list available models/dates/forecasts, create charts, and query hydrofabric data.

## Quick Start

From the `nextgen_plugins/` directory:

```bash
./scripts/setup-mcp.sh
```

This creates a virtual environment at `.venv-mcp/`, installs dependencies, and starts the server on `http://0.0.0.0:9000/sse`.

### Setup only (no run)

```bash
./scripts/setup-mcp.sh --setup
```

### Run only (skip setup)

```bash
./scripts/setup-mcp.sh --run
```

### Manual setup

```bash
python3 -m venv .venv-mcp
source .venv-mcp/bin/activate
pip install -r nextgen_mcp/requirements.txt
python -m nextgen_mcp.mcp_server
```

## Connecting to Claude Desktop

Add the following to your Claude Desktop MCP configuration file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

### Option 1: SSE transport (server runs separately)

Start the MCP server first, then configure Claude Desktop to connect via SSE:

```json
{
  "mcpServers": {
    "nrds": {
      "transport": {
        "type": "sse",
        "url": "http://localhost:9000/sse"
      }
    }
  }
}
```

### Option 2: stdio transport (Claude Desktop manages the process)

Claude Desktop launches and manages the MCP server process directly:

```json
{
  "mcpServers": {
    "nrds": {
      "command": "/path/to/nextgen_plugins/.venv-mcp/bin/python",
      "args": ["-m", "nextgen_mcp.mcp_server"],
      "cwd": "/path/to/nextgen_plugins",
      "env": {
        "NRDS_API_HOST": "http://localhost:8000/apps/nrds/api"
      }
    }
  }
}
```

Replace `/path/to/nextgen_plugins` with the actual path (e.g., `/home/aquagio/tethysdev/firoh/plugins/nextgen_plugins`).

**Note:** For stdio transport, the server needs to detect the transport mode. The current server defaults to SSE on port 9000. To use stdio, you would need to either modify `mcp_server.py` to accept a `--transport` argument or set a `MCP_TRANSPORT` environment variable.

## Connecting to Claude Code (CLI)

### Option 1: Via the CLI command (recommended)

With the MCP server running, add it in one command:

```bash
claude mcp add nrds --transport sse http://localhost:9000/sse
```

This registers the server in your project config. Restart Claude Code to pick up the new tools.

To verify it was added:

```bash
claude mcp list
```

To remove it later:

```bash
claude mcp remove nrds
```

### Option 2: Manual config file

Add to `.mcp.json` in the project root or `~/.claude.json`:

```json
{
  "mcpServers": {
    "nrds": {
      "type": "sse",
      "url": "http://localhost:9000/sse"
    }
  }
}
```

### Usage

1. Start the MCP server: `./scripts/setup-mcp.sh`
2. In another terminal, start Claude Code: `claude`
3. The NRDS tools are now available — ask questions like:
   - "What models are available for NRDS?"
   - "List available dates for cfe_nom"
   - "Query the first output file for feature_id 1019290"

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NRDS_API_HOST` | `http://localhost:8000/apps/nrds/api` | NRDS REST API base URL |
| `NRDS_LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `BUCKET` | `ciroh-community-ngen-datastream` | S3 bucket for output files |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (if using chat features) |

## Available Tools

The MCP server exposes the following tools:

| Tool | Description |
|------|-------------|
| `list_available_models` | List NWM model configurations |
| `list_available_dates` | List available forecast dates for a model |
| `list_available_forecasts` | List forecast types (short_range, medium_range, etc.) |
| `list_available_cycles` | List available cycles for a date |
| `list_available_vpus` | List available VPUs for a model/date/forecast |
| `list_available_output_files` | List output files in S3 for given parameters |
| `get_output_file` | Get a specific output file URL |
| `query_output_file` | Run SQL queries against parquet/netcdf output files |
| `query_output_file_from_output_selector` | Query using model/date/forecast/cycle/vpu selectors |
| `create_plotly_chart_from_output_file` | Generate Plotly charts from output file queries |
| `create_plotly_chart_from_output_selector` | Generate charts using output selectors |
| `query_hydrofabric_parquet_file` | Query hydrofabric parquet data |
| `build_hydrofabric_feature_map_config` | Build map configurations for hydrofabric features |

## Docker Alternative

If you prefer Docker, use the devcontainer setup:

```bash
cd .devcontainer
docker compose -f docker-compose.dev.yml up mcp
```

This runs the MCP server on `http://localhost:9000/sse` with all dependencies pre-installed.

## Project Structure

```
nextgen_mcp/
  __init__.py
  mcp_server.py      # MCP server entry point (FastMCP + tool definitions)
  utils.py            # Helper functions, REST API bridge
  validations.py      # Type literals (FORECASTS, MODELS)
  validators.py       # Pydantic validators (normalize_vpu, OutputsFilesQuery)
  rest.py             # REST API wrappers (S3, DuckDB, output file queries)
  utils_rest.py       # Low-level utilities (DuckDB queries, Plotly chart generation)
  requirements.txt    # Python dependencies
  README.md           # This file
scripts/
  setup-mcp.sh        # One-command setup + run script
```
