"""
ESXi Server Manager

Manages multiple ESXi server configurations.
"""

import json
import os
import threading
from pathlib import Path
from typing import List, Dict, Optional
import uuid


class ServerManager:
    """Manages ESXi server configurations"""

    def __init__(self, config_file=None):
        """
        Initialize server manager.

        Args:
            config_file: Path to servers.json file
        """
        if config_file is None:
            # Default to project root
            project_root = Path(__file__).parent.parent.parent
            config_file = project_root / 'config' / 'servers.json'

        self.config_file = Path(config_file)
        self.config_file.parent.mkdir(parents=True, exist_ok=True)

        self.lock = threading.Lock()
        self._servers = {}
        self._load_servers()

    def _load_servers(self):
        """Load servers from config file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    self._servers = data.get('servers', {})
            except Exception as e:
                print(f"Error loading servers config: {e}")
                self._servers = {}
        else:
            # Create default config with current ESXi from .env
            from dotenv import load_dotenv
            load_dotenv()

            default_server = {
                'id': str(uuid.uuid4()),
                'name': 'Default ESXi',
                'host': os.getenv('ESXI_HOST', ''),
                'port': int(os.getenv('ESXI_PORT', 443)),
                'user': os.getenv('ESXI_USER', 'root'),
                'password': os.getenv('ESXI_PASSWORD', ''),
                'verify_ssl': os.getenv('ESXI_VERIFY_SSL', 'false').lower() == 'true',
                'enabled': True
            }

            if default_server['host']:
                self._servers[default_server['id']] = default_server
                self._save_servers()

    def _save_servers(self):
        """Save servers to config file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump({'servers': self._servers}, f, indent=2)
        except Exception as e:
            print(f"Error saving servers config: {e}")

    def get_all_servers(self) -> List[Dict]:
        """Get all servers"""
        with self.lock:
            return list(self._servers.values())

    def get_enabled_servers(self) -> List[Dict]:
        """Get only enabled servers"""
        with self.lock:
            return [s for s in self._servers.values() if s.get('enabled', True)]

    def get_server(self, server_id: str) -> Optional[Dict]:
        """Get server by ID"""
        with self.lock:
            return self._servers.get(server_id)

    def add_server(self, name: str, host: str, user: str, password: str,
                   port: int = 443, verify_ssl: bool = False) -> Dict:
        """
        Add new server.

        Args:
            name: Server display name
            host: ESXi hostname/IP
            user: Username
            password: Password
            port: Port (default: 443)
            verify_ssl: Verify SSL certificate

        Returns:
            Created server dict
        """
        server = {
            'id': str(uuid.uuid4()),
            'name': name,
            'host': host,
            'port': port,
            'user': user,
            'password': password,
            'verify_ssl': verify_ssl,
            'enabled': True
        }

        with self.lock:
            self._servers[server['id']] = server
            self._save_servers()

        return server

    def update_server(self, server_id: str, **kwargs) -> Optional[Dict]:
        """
        Update server configuration.

        Args:
            server_id: Server ID
            **kwargs: Fields to update

        Returns:
            Updated server dict or None if not found
        """
        with self.lock:
            if server_id not in self._servers:
                return None

            server = self._servers[server_id]

            # Update allowed fields
            allowed_fields = {'name', 'host', 'port', 'user', 'password', 'verify_ssl', 'enabled'}
            for key, value in kwargs.items():
                if key in allowed_fields:
                    server[key] = value

            self._save_servers()
            return server

    def delete_server(self, server_id: str) -> bool:
        """
        Delete server.

        Args:
            server_id: Server ID

        Returns:
            True if deleted, False if not found
        """
        with self.lock:
            if server_id in self._servers:
                del self._servers[server_id]
                self._save_servers()
                return True
            return False

    def test_connection(self, server_id: str) -> Dict:
        """
        Test connection to server.

        Args:
            server_id: Server ID

        Returns:
            {'success': bool, 'message': str, 'vm_count': int}
        """
        server = self.get_server(server_id)
        if not server:
            return {'success': False, 'message': 'Server not found'}

        try:
            from src.utils.esxi_client import ESXiClient

            client = ESXiClient(
                host=server['host'],
                user=server['user'],
                password=server['password'],
                port=server['port'],
                verify_ssl=server['verify_ssl']
            )

            client.connect()
            vms = client.get_vms()
            vm_count = len(vms)
            client.disconnect()

            return {
                'success': True,
                'message': f'Connected successfully - {vm_count} VMs found',
                'vm_count': vm_count
            }

        except Exception as e:
            return {
                'success': False,
                'message': f'Connection failed: {str(e)}'
            }


# Global instance
_global_manager = None


def get_server_manager():
    """Get global server manager instance"""
    global _global_manager
    if _global_manager is None:
        _global_manager = ServerManager()
    return _global_manager
