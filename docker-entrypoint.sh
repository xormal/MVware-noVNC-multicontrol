#!/bin/bash
set -e

echo "Starting ESXi Console Portal..."

# Start WebSocket proxy in background
echo "Starting WebSocket proxy on port ${WS_PROXY_PORT:-8765}..."
python -m src.ws_proxy.webmks_proxy &
WS_PID=$!

# Wait a moment for WebSocket proxy to start
sleep 2

# Start Flask API in background
echo "Starting Flask API on port ${FLASK_PORT:-5001}..."
python -m src.api.app &
FLASK_PID=$!

# Function to handle shutdown
shutdown() {
    echo "Shutting down..."
    kill -TERM $WS_PID $FLASK_PID 2>/dev/null || true
    wait $WS_PID $FLASK_PID 2>/dev/null || true
    echo "Shutdown complete"
    exit 0
}

# Trap SIGTERM and SIGINT
trap shutdown SIGTERM SIGINT

echo "Both services started successfully"
echo "WebSocket Proxy PID: $WS_PID"
echo "Flask API PID: $FLASK_PID"

# Wait for both processes (this keeps container running)
# Using 'wait' without -n for better compatibility
wait
