#!/bin/bash
# Start Flask API Server

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

cd "$PROJECT_ROOT"

echo "================================"
echo "Starting Flask API Server"
echo "================================"
echo ""

# Activate virtual environment
source venv/bin/activate

# Load environment variables
export $(grep -v '^#' .env | xargs)

echo "Configuration:"
echo "  ESXi Host: $ESXI_HOST"
echo "  API listening on: 0.0.0.0:${FLASK_PORT:-5000}"
echo "  Debug mode: ${FLASK_DEBUG:-true}"
echo ""

# Start Flask app
python3 -m src.api.app

deactivate
