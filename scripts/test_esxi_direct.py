#!/usr/bin/env python3
"""Test direct ESXi connection"""

import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.esxi_client import ESXiClient

def test_connection():
    print("Testing direct ESXi connection...")
    print(f"Host: {os.getenv('ESXI_HOST')}")

    try:
        print("\n1. Creating ESXi client...")
        client = ESXiClient()

        print("2. Connecting to ESXi...")
        client.connect()
        print("   ✓ Connected successfully!")

        print("3. Getting VM list...")
        vms = client.get_vms()
        print(f"   ✓ Found {len(vms)} VMs")

        if vms:
            print("\n4. Sample VMs:")
            for vm in vms[:3]:
                info = client.get_vm_info(vm)
                print(f"   - {info['name']}: {info['power_state']}")

        print("\n5. Disconnecting...")
        client.disconnect()
        print("   ✓ Disconnected")

        print("\n✓ ✓ ✓ ESXi connection works perfectly!")
        return True

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = test_connection()
    sys.exit(0 if success else 1)
