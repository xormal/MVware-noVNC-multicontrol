#!/bin/bash
# Start all services (API + Proxy) in separate terminals

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "================================"
echo "ESXi WebMKS Console Portal"
echo "Starting all services..."
echo "================================"
echo ""

# Check if running on macOS or Linux
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS - use Terminal.app
    osascript <<EOF
tell application "Terminal"
    do script "cd '$SCRIPT_DIR/..' && bash scripts/start_proxy.sh"
    do script "cd '$SCRIPT_DIR/..' && bash scripts/start_api.sh"
end tell
EOF
    echo "✓ Services started in separate Terminal windows"
    echo ""
    echo "Access the portal at: http://localhost:5000"
    echo ""
    echo "To stop services: Close the Terminal windows or press Ctrl+C in each"
else
    # Linux - use gnome-terminal or xterm
    if command -v gnome-terminal &> /dev/null; then
        gnome-terminal -- bash -c "cd '$SCRIPT_DIR/..' && bash scripts/start_proxy.sh; exec bash"
        gnome-terminal -- bash -c "cd '$SCRIPT_DIR/..' && bash scripts/start_api.sh; exec bash"
    elif command -v xterm &> /dev/null; then
        xterm -e "cd '$SCRIPT_DIR/..' && bash scripts/start_proxy.sh" &
        xterm -e "cd '$SCRIPT_DIR/..' && bash scripts/start_api.sh" &
    else
        echo "⚠️  Could not find terminal emulator"
        echo "Please start services manually:"
        echo "  Terminal 1: bash scripts/start_proxy.sh"
        echo "  Terminal 2: bash scripts/start_api.sh"
        exit 1
    fi
    echo "✓ Services started in separate terminal windows"
    echo ""
    echo "Access the portal at: http://localhost:5000"
fi
