#!/usr/bin/env python3
"""
Test WebSocket console connection flow
"""

import asyncio
import websockets
import requests
import sys

API_BASE = "http://localhost:5001/api/v1"
VM_MOID = "10"  # Test VM

async def test_websocket_connection():
    """Test the full console connection flow"""

    # Step 1: Create console session via API
    print(f"1. Creating console session for VM {VM_MOID}...")
    response = requests.post(f"{API_BASE}/vms/{VM_MOID}/console", timeout=5)

    if response.status_code != 200:
        print(f"❌ Failed to create session: {response.status_code}")
        print(response.text)
        return False

    session = response.json()
    print(f"✓ Session created: {session['session_id']}")
    print(f"  WS URL: {session['ws_url']}")

    # Step 2: Connect to WebSocket proxy
    ws_url = session['ws_url']
    print(f"\n2. Connecting to WebSocket: {ws_url}")

    try:
        async with websockets.connect(ws_url, subprotocols=['binary']) as ws:
            print(f"✓ WebSocket connected")

            # Step 3: Wait for RFB handshake
            print(f"\n3. Waiting for RFB protocol handshake...")

            # Should receive: "RFB 003.008\n"
            rfb_version = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"✓ Received RFB version: {rfb_version[:20]}")

            if rfb_version.startswith(b'RFB'):
                print("✓ ✓ ✓ WebSocket connection SUCCESSFUL!")
                return True
            else:
                print(f"❌ Unexpected response: {rfb_version[:50]}")
                return False

    except asyncio.TimeoutError:
        print(f"❌ Timeout waiting for RFB handshake")
        return False
    except websockets.exceptions.ConnectionClosed as e:
        print(f"❌ Connection closed: {e}")
        return False
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    print("="*60)
    print("WebSocket Console Connection Test")
    print("="*60)

    success = asyncio.run(test_websocket_connection())

    sys.exit(0 if success else 1)
