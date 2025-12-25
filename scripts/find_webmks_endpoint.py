#!/usr/bin/env python3
"""
Try different WebMKS endpoint combinations to find the working one
"""

import sys
import os
import ssl
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
load_dotenv()

# Connect to ESXi
esxi_host = os.getenv('ESXI_HOST')
esxi_user = os.getenv('ESXI_USER')
esxi_password = os.getenv('ESXI_PASSWORD')

ssl_context = ssl._create_unverified_context()
si = SmartConnect(
    host=esxi_host,
    user=esxi_user,
    pwd=esxi_password,
    port=443,
    sslContext=ssl_context
)

print("="*80)
print(f"Connected to: {si.content.about.fullName}")
print("="*80)

# Find VM
content = si.RetrieveContent()
container = content.viewManager.CreateContainerView(
    content.rootFolder, [vim.VirtualMachine], True
)

vm = None
for v in container.view:
    if v.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
        vm = v
        break

print(f"\nVM: {vm.name} (moId: {vm._moId})")

# Get ticket
ticket = vm.AcquireTicket(ticketType='webmks')
print(f"Ticket: {ticket.ticket}")
print(f"Official URL from API: {ticket.url}\n")

# Try different combinations
print("Testing different endpoint combinations:")
print("="*80)

combinations = [
    # (port, path_template, description)
    (443, f"/ticket/{ticket.ticket}", "Port 443 with /ticket (as per API)"),
    (443, f"/ui/webconsole.html?vmId={vm._moId}&ticket={ticket.ticket}", "Port 443 with /ui/webconsole.html"),
    (443, f"/ui/vmrc/console.html?vmId={vm._moId}&vmName={vm.name}", "Port 443 with /ui/vmrc/console.html"),
    (443, f"/sdk/ticket/{ticket.ticket}", "Port 443 with /sdk/ticket"),
    (902, f"/ticket/{ticket.ticket}", "Port 902 with /ticket"),
    (902, f"/{ticket.ticket}", "Port 902 with ticket only"),
    (902, f"/console?vmId={vm._moId}", "Port 902 with /console"),
]

for port, path, description in combinations:
    url = f"https://{esxi_host}:{port}{path}"
    print(f"\n{description}")
    print(f"   URL: {url}")

    try:
        response = requests.get(url, verify=False, timeout=3, allow_redirects=False)
        print(f"   ✅ Status: {response.status_code}")

        if response.status_code == 200:
            print(f"   Content-Type: {response.headers.get('Content-Type')}")
            print(f"   Content (first 200 chars): {response.text[:200]}")
        elif response.status_code in (301, 302, 303, 307, 308):
            print(f"   Redirect to: {response.headers.get('Location')}")

    except requests.exceptions.Timeout:
        print(f"   ⏱  Timeout")
    except requests.exceptions.ConnectionError as e:
        print(f"   ❌ Connection error: {str(e)[:100]}")
    except Exception as e:
        print(f"   ❌ Error: {str(e)[:100]}")

print("\n" + "="*80)
print("Checking ESXi web UI for console path...")
print("="*80)

# Try to find console path from ESXi web UI
ui_url = f"https://{esxi_host}/ui/"
print(f"\nAccessing: {ui_url}")

try:
    response = requests.get(ui_url, verify=False, timeout=5)
    if "console" in response.text.lower() or "vmrc" in response.text.lower():
        print("✅ Found console/vmrc references in UI")
        # Extract possible console URLs
        import re
        console_refs = re.findall(r'["\']([^"\']*(?:console|vmrc|webmks)[^"\']*)["\']', response.text, re.I)
        if console_refs:
            print("   Possible console paths:")
            for ref in set(console_refs[:10]):
                print(f"      {ref}")
except Exception as e:
    print(f"Error accessing UI: {e}")

Disconnect(si)
