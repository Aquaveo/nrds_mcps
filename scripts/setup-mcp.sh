#!/usr/bin/env bash
# setup-mcp.sh
#
# Creates a Python virtual environment, installs dependencies,
# and starts the NRDS MCP server.
#
# Usage:
#   ./scripts/setup-mcp.sh          # setup + run
#   ./scripts/setup-mcp.sh --setup  # setup only (no run)
#   ./scripts/setup-mcp.sh --run    # run only (skip setup)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv-mcp"
REQUIREMENTS="$PROJECT_DIR/nextgen_mcp/requirements.txt"

setup() {
    echo "==> Setting up MCP server environment"

    if [ ! -d "$VENV_DIR" ]; then
        echo "    Creating virtual environment at $VENV_DIR"
        python3 -m venv "$VENV_DIR"
    else
        echo "    Virtual environment already exists at $VENV_DIR"
    fi

    echo "    Installing dependencies..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$REQUIREMENTS"
    echo "    Done."
}

run() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "Error: Virtual environment not found. Run setup first:"
        echo "  ./scripts/setup-mcp.sh --setup"
        exit 1
    fi

    echo "==> Starting NRDS MCP Server on http://0.0.0.0:9000/sse"
    cd "$PROJECT_DIR"
    exec "$VENV_DIR/bin/python" -m nextgen_mcp.mcp_server
}

case "${1:-}" in
    --setup)
        setup
        ;;
    --run)
        run
        ;;
    *)
        setup
        run
        ;;
esac
