"""
ESXi Client Utility

Provides connection and common operations for VMware ESXi hosts.
"""

import ssl
import os
import urllib3
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from dotenv import load_dotenv

# Disable SSL warnings for self-signed ESXi certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()


class ESXiClient:
    """Client for VMware ESXi API operations"""

    def __init__(self, host=None, user=None, password=None, port=None, verify_ssl=None):
        """
        Initialize ESXi client with credentials.

        Args:
            host: ESXi hostname/IP (defaults to ESXI_HOST env var)
            user: Username (defaults to ESXI_USER env var)
            password: Password (defaults to ESXI_PASSWORD env var)
            port: Port (defaults to ESXI_PORT env var or 443)
            verify_ssl: Verify SSL cert (defaults to ESXI_VERIFY_SSL env var or False)
        """
        self.host = host or os.getenv('ESXI_HOST')
        self.user = user or os.getenv('ESXI_USER')
        self.password = password or os.getenv('ESXI_PASSWORD')
        self.port = int(port or os.getenv('ESXI_PORT', 443))
        self.verify_ssl = verify_ssl if verify_ssl is not None else \
                         os.getenv('ESXI_VERIFY_SSL', 'false').lower() == 'true'

        self.si = None
        self._ssl_context = None

    def connect(self):
        """Establish connection to ESXi host"""
        if not self.verify_ssl:
            self._ssl_context = ssl._create_unverified_context()

        self.si = SmartConnect(
            host=self.host,
            user=self.user,
            pwd=self.password,
            port=self.port,
            sslContext=self._ssl_context
        )
        return self.si

    def disconnect(self):
        """Close connection to ESXi host"""
        if self.si:
            Disconnect(self.si)
            self.si = None

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()

    def get_vms(self):
        """
        Get list of all VMs on the host.

        Returns:
            List of vim.VirtualMachine objects
        """
        if not self.si:
            raise RuntimeError("Not connected to ESXi. Call connect() first.")

        content = self.si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder,
            [vim.VirtualMachine],
            True
        )
        return list(container.view)

    def get_vm_by_moid(self, moid):
        """
        Get VM by managed object ID.

        Args:
            moid: Managed object ID (e.g., 'vm-123')

        Returns:
            vim.VirtualMachine or None
        """
        vms = self.get_vms()
        for vm in vms:
            if vm._moId == moid:
                return vm
        return None

    def get_vm_info(self, vm):
        """
        Extract basic info from VM object.

        Args:
            vm: vim.VirtualMachine object

        Returns:
            Dictionary with VM information
        """
        return {
            'moid': vm._moId,
            'name': vm.name,
            'power_state': str(vm.runtime.powerState),
            'guest_os': vm.config.guestFullName if vm.config else None,
            'guest_ip': vm.guest.ipAddress if vm.guest else None,
            'num_cpu': vm.config.hardware.numCPU if vm.config else None,
            'memory_mb': vm.config.hardware.memoryMB if vm.config else None,
        }

    def acquire_webmks_ticket(self, vm):
        """
        Acquire WebMKS ticket for VM console access.

        Args:
            vm: vim.VirtualMachine object

        Returns:
            Ticket object with host, port, ticket, sslThumbprint
        """
        return vm.AcquireTicket(ticketType='webmks')

    def create_screenshot(self, vm):
        """
        Create screenshot of VM console.

        Args:
            vm: vim.VirtualMachine object

        Returns:
            bytes: PNG image data
        """
        import requests
        from pyVim import task

        # Check if VM is powered on
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
            return None

        try:
            # Create screenshot task
            screenshot_task = vm.CreateScreenshot_Task()

            # Wait for task to complete (with timeout)
            task.WaitForTask(screenshot_task, si=self.si)

            # Get the result from the task info
            # screenshot_task.info.result contains the datastore path
            # Format: [datastore_name] path/to/screenshot.png
            screenshot_path = screenshot_task.info.result

            # Parse datastore path
            # Example: "[datastore1] 01-10/vm08/screenshot-12345.png"
            if not screenshot_path:
                return None

            # Build download URL
            # For standalone ESXi, dcPath is typically "ha-datacenter"
            dc_path = "ha-datacenter"

            # Remove brackets from datastore name and get path
            # [datastore1] path -> path with ?dsName=datastore1
            import re
            match = re.match(r'\[([^\]]+)\]\s*(.+)', screenshot_path)
            if not match:
                return None

            ds_name = match.group(1)
            file_path = match.group(2)

            # Build download URL
            download_url = f"https://{self.host}:{self.port}/folder/{file_path}"
            download_url += f"?dcPath={dc_path}&dsName={ds_name}"

            # Download the screenshot
            response = requests.get(
                download_url,
                auth=(self.user, self.password),
                verify=self.verify_ssl,
                timeout=10
            )

            if response.status_code == 200:
                return response.content
            else:
                return None

        except Exception as e:
            import traceback
            print(f"Error creating screenshot: {e}")
            print(traceback.format_exc())
            return None
