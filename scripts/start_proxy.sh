#!/bin/bash
# Start WebMKS WebSocket Proxy Server

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

cd "$PROJECT_ROOT"

echo "================================"
echo "Starting WebMKS Proxy Server"
echo "================================"
echo ""

# Activate virtual environment
source venv/bin/activate

# Load environment variables
export $(grep -v '^#' .env | xargs)

echo "Configuration:"
echo "  ESXi Host: $ESXI_HOST"
echo "  Proxy listening on: ${WS_PROXY_HOST:-0.0.0.0}:${WS_PROXY_PORT:-8765}"
echo ""

# Start proxy server
python3 -m src.ws_proxy.webmks_proxy

deactivate
