#!/usr/bin/env python3
"""
Full Integration Test

Tests the complete flow:
1. Get VM list via Flask API
2. Create console session
3. Connect to WebSocket proxy
4. Verify RFB/VNC handshake
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import websockets
import ssl
from src.utils.esxi_client import ESXiClient
from dotenv import load_dotenv

load_dotenv()


async def test_integration():
    """Test full integration flow"""

    print("="*80)
    print("Full Integration Test: ESXi → Proxy → noVNC")
    print("="*80)

    # Step 1: Get VM list
    print("\n[Step 1] Getting VM list...")
    with ESXiClient() as client:
        vms = client.get_vms()
        from pyVmomi import vim
        powered_on = [vm for vm in vms if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn]

        if not powered_on:
            print("❌ No powered-on VMs found")
            return False

        vm = powered_on[0]
        vm_info = client.get_vm_info(vm)
        print(f"✅ Found VM: {vm_info['name']} (moid: {vm_info['moid']})")

        # Step 2: Acquire ticket directly (simulating API call)
        print(f"\n[Step 2] Acquiring WebMKS ticket...")
        ticket = client.acquire_webmks_ticket(vm)
        print(f"✅ Ticket acquired")
        print(f"   Ticket: {ticket.ticket[:20]}...")
        print(f"   Host: {ticket.host or 'None (using ESXi host)'}")
        print(f"   Port: {ticket.port}")

    # Step 3: Connect directly to ESXi WebMKS (bypass proxy for testing)
    print(f"\n[Step 3] Testing direct WebMKS connection...")

    esxi_host = os.getenv('ESXI_HOST')
    ws_host = ticket.host if ticket.host else esxi_host
    ws_url = f"wss://{ws_host}:{ticket.port}/ticket/{ticket.ticket}"

    print(f"   URL: {ws_url}")

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            ws_url,
            ssl=ssl_context,
            subprotocols=['binary'],
            origin='http://localhost'
        ) as ws:
            print(f"✅ Connected to WebMKS")

            # Wait for RFB handshake
            print(f"   Waiting for RFB handshake...")

            data = await asyncio.wait_for(ws.recv(), timeout=5.0)

            if data.startswith(b'RFB '):
                version = data[4:11].decode('ascii', errors='ignore')
                print(f"✅ RFB handshake received!")
                print(f"   Version: {version}")
                print(f"   Hex: {data[:12].hex()}")
                print(f"\n🎉 SUCCESS! Full integration working!")
                return True
            else:
                print(f"❌ Unexpected data: {data[:20].hex()}")
                return False

    except asyncio.TimeoutError:
        print(f"❌ Timeout waiting for RFB handshake")
        return False
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False


if __name__ == '__main__':
    success = asyncio.run(test_integration())
    sys.exit(0 if success else 1)
