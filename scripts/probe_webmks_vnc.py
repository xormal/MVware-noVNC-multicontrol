#!/usr/bin/env python3
"""
WebMKS Protocol Probe - Based on markpeek/webmks

This implementation follows the Go reference to properly connect
to VMware WebMKS using VNC over WebSocket with subprotocol "binary".
"""

import sys
import os
import ssl
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

import websocket
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv

load_dotenv()


def probe_webmks_vnc():
    """Probe WebMKS using VNC protocol over WebSocket"""

    # ESXi connection settings
    esxi_host = os.getenv('ESXI_HOST')
    esxi_user = os.getenv('ESXI_USER')
    esxi_password = os.getenv('ESXI_PASSWORD')
    esxi_port = int(os.getenv('ESXI_PORT', 443))

    print("="*80)
    print("WebMKS VNC-over-WebSocket Probe")
    print("Based on markpeek/webmks reference implementation")
    print("="*80)

    # Connect to ESXi
    print(f"\n[{datetime.now()}] Connecting to ESXi: {esxi_host}:{esxi_port}")
    ssl_context = ssl._create_unverified_context()
    si = SmartConnect(
        host=esxi_host,
        user=esxi_user,
        pwd=esxi_password,
        port=esxi_port,
        sslContext=ssl_context
    )

    print(f"✅ Connected: {si.content.about.fullName}")

    # Find powered-on VM
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )

    vm = None
    for v in container.view:
        if v.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            vm = v
            break

    if not vm:
        print("❌ No powered-on VMs found")
        Disconnect(si)
        return False

    print(f"✅ Found VM: {vm.name}")

    # Acquire WebMKS ticket
    print(f"\n[{datetime.now()}] Acquiring WebMKS ticket...")
    ticket = vm.AcquireTicket(ticketType='webmks')

    print(f"✅ Ticket acquired")
    print(f"   Ticket URL: {ticket.url}")
    print(f"   Thumbprint: {ticket.sslThumbprint}")

    # Construct WebSocket URL
    # Use ticket.host if available, otherwise use ESXi host
    ws_host = ticket.host if ticket.host else esxi_host
    ws_port = ticket.port if ticket.port else 443
    ws_url = f"wss://{ws_host}:{ws_port}/ticket/{ticket.ticket}"

    print(f"\n[{datetime.now()}] WebSocket URL: {ws_url}")
    print(f"   Protocol: binary (CRITICAL!)")
    print(f"   Origin: http://localhost")

    # Disconnect from API (we have the ticket)
    Disconnect(si)

    # Track received data
    frames_received = []
    rfb_detected = False

    def on_open(ws):
        print(f"\n✅ WebSocket connection established!")
        print(f"📡 Waiting for VNC/RFB handshake...")

    def on_message(ws, message):
        nonlocal rfb_detected

        frame_num = len(frames_received) + 1
        frames_received.append({
            'timestamp': datetime.now().isoformat(),
            'size': len(message),
            'data': message[:100] if isinstance(message, bytes) else message
        })

        print(f"\n📦 Frame #{frame_num} received")
        print(f"   Type: {'binary' if isinstance(message, bytes) else 'text'}")
        print(f"   Size: {len(message)} bytes")

        if isinstance(message, bytes):
            # Display hex dump
            hex_dump = ' '.join(f'{b:02x}' for b in message[:64])
            print(f"   Hex (first 64 bytes): {hex_dump}")

            # Check for RFB signature
            if message.startswith(b'RFB '):
                rfb_detected = True
                print(f"\n   🎯🎯🎯 RFB SIGNATURE DETECTED! 🎯🎯🎯")
                version = message[4:11].decode('ascii', errors='ignore')
                print(f"   RFB Version: {version}")
                print(f"\n   ✅ CONFIRMED: WebMKS uses VNC/RFB protocol!")
                print(f"   ✅ Strategy: Simple WS↔WS bridge for noVNC")

                # Close after detecting RFB
                print(f"\n📊 Protocol detection successful!")
                ws.close()
            else:
                # Try to decode as ASCII
                try:
                    ascii_text = message[:100].decode('ascii', errors='ignore')
                    if ascii_text and ascii_text.isprintable():
                        print(f"   ASCII: {ascii_text}")
                except:
                    pass
        else:
            print(f"   Text content: {message[:200]}")

        # Stop after 10 frames
        if len(frames_received) >= 10:
            print(f"\n📊 Collected 10 frames, closing...")
            ws.close()

    def on_error(ws, error):
        print(f"\n❌ WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        print(f"\n🔌 WebSocket closed")
        print(f"   Status code: {close_status_code}")
        print(f"   Message: {close_msg}")

    # Connect to WebSocket with CRITICAL parameters from Go reference
    print(f"\n🔗 Connecting to WebSocket...")
    print(f"   CRITICAL: Using subprotocol='binary' (from Go reference)")

    try:
        ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            # CRITICAL: subprotocols parameter!
            subprotocols=['binary']  # ← This was missing in our previous attempts!
        )

        # Set origin header (from Go reference)
        headers = {
            'Origin': 'http://localhost'
        }

        ws.run_forever(
            sslopt={"cert_reqs": ssl.CERT_NONE},
            origin='http://localhost',
            ping_interval=10,
            ping_timeout=5
        )

    except Exception as e:
        print(f"\n❌ Exception: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Final analysis
    print(f"\n" + "="*80)
    print("FINAL RESULTS")
    print("="*80)
    print(f"Total frames received: {len(frames_received)}")
    print(f"RFB/VNC detected: {'✅ YES' if rfb_detected else '❌ NO'}")

    if rfb_detected:
        print(f"\n🎉 SUCCESS!")
        print(f"   WebMKS = VNC/RFB over WebSocket")
        print(f"   Implementation strategy: WS↔WS bridge for noVNC")
        print(f"   Complexity: LOW")
    else:
        print(f"\n⚠️  RFB signature not detected in {len(frames_received)} frames")
        if len(frames_received) > 0:
            print(f"   First frame preview:")
            first_frame = frames_received[0]['data']
            if isinstance(first_frame, bytes):
                print(f"   Hex: {first_frame[:32].hex()}")

    return rfb_detected


if __name__ == '__main__':
    success = probe_webmks_vnc()
    sys.exit(0 if success else 1)
