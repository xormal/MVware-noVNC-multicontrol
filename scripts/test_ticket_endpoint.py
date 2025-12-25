#!/usr/bin/env python3
"""Test what the ticket endpoint returns"""

import sys
import os
import ssl
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv

# Disable SSL warnings
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

print(f"Connected to: {si.content.about.fullName}")

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

print(f"VM: {vm.name}")

# Get ticket
ticket = vm.AcquireTicket(ticketType='webmks')
print(f"\nTicket URL: {ticket.url}")
print(f"Ticket: {ticket.ticket}")

# Try HTTP GET to the ticket endpoint
import re
http_url = re.sub(r'://([^:/@]+)', f'://{esxi_host}', ticket.url)
http_url = http_url.replace('wss://', 'https://')

print(f"\nTrying HTTP GET to: {http_url}")

try:
    response = requests.get(http_url, verify=False, timeout=5)
    print(f"Status: {response.status_code}")
    print(f"Headers: {dict(response.headers)}")
    print(f"Content: {response.text[:500]}")
except Exception as e:
    print(f"Error: {e}")

Disconnect(si)
