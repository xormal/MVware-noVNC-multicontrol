#!/usr/bin/env python3
"""
Async WebMKS Protocol Probe using aiohttp

Minimal delay between ticket acquisition and WebSocket connection.
"""

import sys
import os
import ssl
import asyncio
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv

load_dotenv()


async def probe_webmks():
    """Probe WebMKS protocol with minimal ticket-to-connection delay"""

    # ESXi connection settings
    esxi_host = os.getenv('ESXI_HOST')
    esxi_user = os.getenv('ESXI_USER')
    esxi_password = os.getenv('ESXI_PASSWORD')
    esxi_port = int(os.getenv('ESXI_PORT', 443))

    print("="*80)
    print("Async WebMKS Protocol Probe")
    print("="*80)
    print(f"\n[{datetime.now()}] Connecting to ESXi: {esxi_host}:{esxi_port}")

    # Connect to ESXi
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
        return

    print(f"✅ Found VM: {vm.name}")

    # Acquire ticket
    print(f"\n[{datetime.now()}] Acquiring WebMKS ticket...")
    ticket = vm.AcquireTicket(ticketType='webmks')

    print(f"✅ Ticket acquired")
    print(f"   URL: {ticket.url}")
    print(f"   Thumbprint: {ticket.sslThumbprint}")

    # Replace hostname with IP
    import re
    ws_url = re.sub(r'://([^:/@]+)', f'://{esxi_host}', ticket.url)
    print(f"   Adjusted URL: {ws_url}")

    # Disconnect from API (we have the ticket)
    Disconnect(si)

    # Immediately connect to WebSocket
    print(f"\n[{datetime.now()}] Connecting to WebSocket...")

    # Create SSL context that doesn't verify certificates
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                ws_url,
                ssl=ssl_ctx,
                heartbeat=10
            ) as ws:
                print(f"✅ WebSocket connected!")
                print(f"   Type: {ws.type}")
                print(f"\n📡 Waiting for messages...")

                frame_count = 0
                async for msg in ws:
                    frame_count += 1
                    print(f"\n📦 Frame #{frame_count}")
                    print(f"   Type: {msg.type}")

                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data
                        print(f"   Size: {len(data)} bytes")
                        print(f"   Hex (first 64 bytes): {data[:64].hex()}")

                        # Check for RFB signature
                        if data.startswith(b'RFB '):
                            print(f"   🎯 RFB SIGNATURE DETECTED!")
                            print(f"   RFB Version: {data[4:11].decode('ascii', errors='ignore')}")
                        else:
                            # Try ASCII decode
                            try:
                                ascii_text = data[:100].decode('ascii', errors='ignore')
                                if ascii_text.isprintable():
                                    print(f"   ASCII: {ascii_text}")
                            except:
                                pass

                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        print(f"   Text: {msg.data[:200]}")

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        print(f"   ❌ Error: {ws.exception()}")
                        break

                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        print(f"   Connection closing")
                        break

                    # Stop after 10 frames
                    if frame_count >= 10:
                        print(f"\n✅ Collected {frame_count} frames, disconnecting...")
                        await ws.close()
                        break

                print(f"\n📊 Total frames received: {frame_count}")

    except aiohttp.ClientConnectorError as e:
        print(f"❌ Connection error: {e}")
    except aiohttp.WSServerHandshakeError as e:
        print(f"❌ WebSocket handshake error: {e}")
        print(f"   Status: {e.status}")
        print(f"   Message: {e.message}")
        if hasattr(e, 'headers'):
            print(f"   Headers: {e.headers}")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(probe_webmks())
