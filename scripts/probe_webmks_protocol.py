#!/usr/bin/env python3
"""
WebMKS Protocol Detection Script

This script connects to ESXi WebMKS endpoint and analyzes the protocol
to determine if it's pure RFB/VNC or VMware-specific format.

Critical for determining proxy implementation strategy:
- If RFB detected: Simple WS↔WS bridge
- If not RFB: Need protocol translator
"""

import sys
import os
import ssl
import json
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import websocket
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv
import logging

# Enable websocket debug logging
websocket.enableTrace(True)
logging.basicConfig(level=logging.DEBUG)

# Load environment variables
load_dotenv()


class WebMKSProtocolProbe:
    """Analyzes WebMKS protocol to detect RFB compatibility"""

    # RFB protocol signature
    RFB_SIGNATURE = b'RFB '

    def __init__(self):
        self.esxi_host = os.getenv('ESXI_HOST')
        self.esxi_user = os.getenv('ESXI_USER')
        self.esxi_password = os.getenv('ESXI_PASSWORD')
        self.esxi_port = int(os.getenv('ESXI_PORT', 443))
        self.verify_ssl = os.getenv('ESXI_VERIFY_SSL', 'false').lower() == 'true'

        self.si = None
        self.log_file = f"protocol_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log(self, message):
        """Log message to both console and file"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        log_entry = f"[{timestamp}] {message}"
        print(log_entry)
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry + '\n')

    def connect_to_esxi(self):
        """Connect to ESXi host via pyVmomi"""
        self.log(f"Connecting to ESXi host: {self.esxi_host}:{self.esxi_port}")

        # Disable SSL verification if needed
        ssl_context = None
        if not self.verify_ssl:
            ssl_context = ssl._create_unverified_context()
            self.log("⚠️  SSL verification disabled")

        try:
            self.si = SmartConnect(
                host=self.esxi_host,
                user=self.esxi_user,
                pwd=self.esxi_password,
                port=self.esxi_port,
                sslContext=ssl_context
            )
            self.log(f"✅ Connected to ESXi: {self.si.content.about.fullName}")
            return True
        except Exception as e:
            self.log(f"❌ Failed to connect to ESXi: {e}")
            return False

    def get_powered_on_vm(self):
        """Find a powered-on VM to test console access"""
        self.log("Searching for powered-on VMs...")

        content = self.si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )

        for vm in container.view:
            if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
                self.log(f"✅ Found powered-on VM: {vm.name} (moId: {vm._moId})")
                return vm

        self.log("❌ No powered-on VMs found")
        return None

    def acquire_webmks_ticket(self, vm):
        """Acquire WebMKS ticket for VM console access"""
        self.log(f"Acquiring WebMKS ticket for VM: {vm.name}")

        try:
            ticket = vm.AcquireTicket(ticketType='webmks')
            self.log(f"✅ WebMKS ticket acquired:")
            self.log(f"   Host: {ticket.host}")
            self.log(f"   Port: {ticket.port}")
            self.log(f"   Ticket: {ticket.ticket[:20]}... (truncated)")
            self.log(f"   SSL Thumbprint: {ticket.sslThumbprint}")
            # Log all ticket attributes
            self.log(f"   All ticket attributes:")
            for attr in dir(ticket):
                if not attr.startswith('_'):
                    try:
                        val = getattr(ticket, attr)
                        if not callable(val):
                            self.log(f"      {attr}: {val}")
                    except:
                        pass

            return ticket
        except Exception as e:
            self.log(f"❌ Failed to acquire ticket: {e}")
            return None

    def probe_websocket_protocol(self, ticket):
        """Connect to WebMKS WebSocket and analyze protocol"""
        self.log("\n" + "="*80)
        self.log("STARTING PROTOCOL ANALYSIS")
        self.log("="*80)

        # Construct WebSocket URL
        # Ticket contains a ready URL, but hostname may not resolve
        # Replace hostname with ESXi IP address
        if hasattr(ticket, 'url') and ticket.url:
            import re
            # Extract hostname from URL and replace with IP
            ws_url = re.sub(r'://([^:/@]+)', f'://{self.esxi_host}', ticket.url)
            self.log(f"Using ticket.url: {ticket.url}")
            self.log(f"Replaced hostname with IP: {ws_url}")
        else:
            # Fallback to manual construction
            ws_host = ticket.host if ticket.host else self.esxi_host
            ws_port = ticket.port if ticket.port else 443
            ws_url = f"wss://{ws_host}:{ws_port}/ticket/{ticket.ticket}"
            self.log(f"WebSocket URL: {ws_url}")

        # Track received frames
        frames = []

        def on_message(ws, message):
            """Callback for received WebSocket messages"""
            frame_info = {
                'timestamp': datetime.now().isoformat(),
                'type': 'binary' if isinstance(message, bytes) else 'text',
                'size': len(message),
                'data': message if isinstance(message, str) else message[:100]  # Limit binary data
            }
            frames.append(frame_info)

            self.log(f"\n📦 Frame #{len(frames)} received:")
            self.log(f"   Type: {frame_info['type']}")
            self.log(f"   Size: {frame_info['size']} bytes")

            if isinstance(message, bytes):
                # Check for RFB signature
                if message.startswith(self.RFB_SIGNATURE):
                    self.log(f"   🎯 RFB SIGNATURE DETECTED: {message[:12]}")
                    self.log(f"   RFB Version: {message[4:11].decode('ascii', errors='ignore')}")
                else:
                    self.log(f"   Hex dump (first 64 bytes):")
                    hex_dump = ' '.join(f'{b:02x}' for b in message[:64])
                    self.log(f"   {hex_dump}")

                    # Try to decode as ASCII
                    try:
                        ascii_text = message[:100].decode('ascii', errors='ignore')
                        if ascii_text.isprintable():
                            self.log(f"   ASCII: {ascii_text}")
                    except:
                        pass
            else:
                self.log(f"   Content: {message[:200]}")

            # Stop after collecting enough frames
            if len(frames) >= 10:
                self.log("\n📊 Collected 10 frames, closing connection...")
                ws.close()

        def on_error(ws, error):
            self.log(f"❌ WebSocket error: {error}")

        def on_close(ws, close_status_code, close_msg):
            self.log(f"\n🔌 WebSocket closed (code: {close_status_code}, msg: {close_msg})")

        def on_open(ws):
            self.log("✅ WebSocket connection established")
            self.log("📡 Waiting for server messages...")

        try:
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            # Run with timeout
            self.log("\n🔗 Connecting to WebSocket...")
            ws.run_forever(
                sslopt={"cert_reqs": ssl.CERT_NONE},
                ping_interval=10,
                ping_timeout=5
            )

            return frames

        except Exception as e:
            self.log(f"❌ WebSocket connection failed: {e}")
            import traceback
            self.log(traceback.format_exc())
            return frames

    def analyze_results(self, frames):
        """Analyze collected frames and make determination"""
        self.log("\n" + "="*80)
        self.log("ANALYSIS RESULTS")
        self.log("="*80)

        if not frames:
            self.log("❌ No frames captured - unable to determine protocol")
            return

        self.log(f"\n📊 Total frames captured: {len(frames)}")

        # Count frame types
        binary_frames = [f for f in frames if f['type'] == 'binary']
        text_frames = [f for f in frames if f['type'] == 'text']

        self.log(f"   Binary frames: {len(binary_frames)}")
        self.log(f"   Text frames: {len(text_frames)}")

        # Check for RFB signature
        rfb_detected = False
        for frame in binary_frames:
            if isinstance(frame['data'], bytes) and frame['data'].startswith(self.RFB_SIGNATURE):
                rfb_detected = True
                break

        self.log("\n🎯 PROTOCOL DETERMINATION:")
        if rfb_detected:
            self.log("   ✅ RFB/VNC PROTOCOL DETECTED!")
            self.log("   → Strategy: Implement simple WS↔WS bridge")
            self.log("   → noVNC compatibility: HIGH")
            self.log("   → Implementation complexity: LOW")
        else:
            self.log("   ⚠️  RFB/VNC PROTOCOL NOT DETECTED")
            self.log("   → Protocol appears to be VMware-specific (WebMKS)")
            self.log("   → Strategy: Need protocol translator or use WMKS library")
            self.log("   → noVNC compatibility: REQUIRES TRANSLATION")
            self.log("   → Implementation complexity: HIGH")

        # Save detailed analysis
        analysis_file = f"protocol_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(analysis_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'rfb_detected': rfb_detected,
                'frames_captured': len(frames),
                'binary_frames': len(binary_frames),
                'text_frames': len(text_frames),
                'frames': [
                    {
                        'timestamp': f['timestamp'],
                        'type': f['type'],
                        'size': f['size'],
                        'hex_preview': f['data'].hex()[:128] if isinstance(f['data'], bytes) else None,
                        'text_preview': f['data'][:200] if isinstance(f['data'], str) else None
                    }
                    for f in frames
                ]
            }, f, indent=2)

        self.log(f"\n💾 Detailed analysis saved to: {analysis_file}")
        self.log(f"💾 Full log saved to: {self.log_file}")

    def run(self):
        """Main execution flow"""
        self.log("="*80)
        self.log("WebMKS Protocol Detection Tool")
        self.log("="*80)

        try:
            # Step 1: Connect to ESXi
            if not self.connect_to_esxi():
                return False

            # Step 2: Find powered-on VM
            vm = self.get_powered_on_vm()
            if not vm:
                self.log("\n⚠️  Please power on at least one VM and try again")
                return False

            # Step 3: Acquire WebMKS ticket
            ticket = self.acquire_webmks_ticket(vm)
            if not ticket:
                return False

            # Step 4: Probe WebSocket protocol
            frames = self.probe_websocket_protocol(ticket)

            # Step 5: Analyze results
            self.analyze_results(frames)

            return True

        finally:
            # Cleanup
            if self.si:
                try:
                    Disconnect(self.si)
                    self.log("\n✅ Disconnected from ESXi")
                except:
                    pass


def main():
    """Entry point"""
    probe = WebMKSProtocolProbe()
    success = probe.run()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
