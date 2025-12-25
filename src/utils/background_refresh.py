"""
Background VM Data Refresh Service

Periodically fetches VM data from all ESXi servers and updates cache.
This ensures clients always get fast responses from fresh cache.
"""

import threading
import time
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class BackgroundRefreshService:
    """
    Background service that periodically refreshes VM data from ESXi servers.

    Runs in a separate thread and updates the global cache automatically.
    """

    def __init__(self, refresh_interval: int = 30):
        """
        Args:
            refresh_interval: Seconds between refresh cycles (default: 30)
        """
        self.refresh_interval = refresh_interval
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        self._cache = {}  # Format: {server_id: {'vms': [...], 'stats': {...}, 'timestamp': float, 'error': str or None}}

    def start(self):
        """Start the background refresh service"""
        with self._lock:
            if self.running:
                logger.warning("Background refresh service already running")
                return

            self.running = True
            self.thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self.thread.start()
            logger.info(f"Background refresh service started (interval: {self.refresh_interval}s)")

    def stop(self):
        """Stop the background refresh service"""
        with self._lock:
            if not self.running:
                return

            self.running = False
            if self.thread:
                self.thread.join(timeout=10)
            logger.info("Background refresh service stopped")

    def _refresh_loop(self):
        """Main refresh loop that runs in background thread"""
        while self.running:
            try:
                self._refresh_all_servers()
            except Exception as e:
                logger.error(f"Error in background refresh: {e}")

            # Sleep in small chunks to allow fast shutdown
            for _ in range(self.refresh_interval):
                if not self.running:
                    break
                time.sleep(1)

    def _refresh_all_servers(self):
        """Refresh VM data for all enabled servers"""
        from src.utils.server_manager import get_server_manager

        manager = get_server_manager()
        servers = manager.get_enabled_servers()

        for server in servers:
            if not server.get('enabled', True):
                continue

            try:
                server_id = server['id']
                server_name = server['name']
                logger.debug(f"Background refresh: fetching VMs and stats from {server_name}")

                start_time = time.time()

                # Fetch VMs and stats separately to handle partial failures
                vm_list = []
                stats = {}
                vm_error_msg = None
                stats_error_msg = None

                try:
                    vm_list = self._fetch_vms_for_server(server)
                except Exception as vm_error:
                    vm_error_msg = str(vm_error)
                    logger.error(f"Failed to fetch VMs from {server_name}: {vm_error}")
                    # Preserve old VMs if available
                    if server_id in self._cache:
                        vm_list = self._cache[server_id].get('vms', [])
                        logger.info(f"Using cached VMs for {server_name} ({len(vm_list)} VMs)")

                try:
                    stats = self._fetch_stats_for_server(server)
                except Exception as stats_error:
                    stats_error_msg = str(stats_error)
                    logger.error(f"Failed to fetch stats from {server_name}: {stats_error}")
                    # Preserve old stats if available
                    if server_id in self._cache:
                        stats = self._cache[server_id].get('stats', {})
                        logger.info(f"Using cached stats for {server_name}")

                elapsed = time.time() - start_time

                # Combine error messages if both failed
                error_msg = None
                if vm_error_msg and stats_error_msg:
                    error_msg = f"VMs: {vm_error_msg}; Stats: {stats_error_msg}"
                elif vm_error_msg:
                    error_msg = vm_error_msg
                elif stats_error_msg:
                    error_msg = stats_error_msg

                # Update cache (even with partial data)
                self._cache[server_id] = {
                    'vms': vm_list,
                    'stats': stats,
                    'timestamp': time.time(),
                    'error': error_msg
                }

                logger.info(f"Background refresh: cached {len(vm_list)} VMs and stats from {server_name} ({elapsed:.1f}s)")

            except Exception as e:
                logger.error(f"Background refresh error for {server.get('name', 'unknown')}: {e}")

    def _fetch_vms_for_server(self, server: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch VM list from a single ESXi server"""
        from src.utils.esxi_client import ESXiClient

        client = ESXiClient(
            host=server['host'],
            user=server['user'],
            password=server['password'],
            port=server['port'],
            verify_ssl=server['verify_ssl']
        )

        try:
            client.connect()
            vms = client.get_vms()

            vm_list = []
            for vm in vms:
                try:
                    vm_list.append({
                        'name': vm.name,
                        'moid': vm._moId,
                        'power_state': vm.runtime.powerState,
                        'guest_os': vm.config.guestFullName if vm.config else 'Unknown',
                        'memory_mb': vm.config.hardware.memoryMB if vm.config else 0,
                        'num_cpu': vm.config.hardware.numCPU if vm.config else 0,
                        'guest_ip': vm.guest.ipAddress if vm.guest and vm.guest.ipAddress else None
                    })
                except Exception as e:
                    logger.warning(f"Error getting VM info in background refresh: {e}")
                    continue

            return vm_list

        finally:
            client.disconnect()

    def _fetch_stats_for_server(self, server: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch resource statistics from a single ESXi server"""
        from src.utils.esxi_client import ESXiClient
        from pyVmomi import vim

        client = ESXiClient(
            host=server['host'],
            user=server['user'],
            password=server['password'],
            port=server['port'],
            verify_ssl=server['verify_ssl']
        )

        try:
            client.connect()
            content = client.si.RetrieveContent()
            host_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.HostSystem], True
            )
            hosts = host_view.view
            host_view.Destroy()

            if not hosts:
                return {}

            host = hosts[0]

            # CPU stats
            cpu_usage = host.summary.quickStats.overallCpuUsage or 0
            cpu_total = host.summary.hardware.cpuMhz * host.summary.hardware.numCpuCores
            cpu_percent = (cpu_usage / cpu_total * 100) if cpu_total > 0 else 0

            cpu_stats = {
                'usage_percent': round(cpu_percent, 1),
                'num_cores': host.summary.hardware.numCpuCores,
                'usage_mhz': cpu_usage,
                'total_mhz': cpu_total
            }

            # Memory stats
            mem_usage = host.summary.quickStats.overallMemoryUsage * 1024 * 1024
            mem_total = host.summary.hardware.memorySize

            memory_stats = {
                'used': mem_usage,
                'total': mem_total,
                'percent': round((mem_usage / mem_total * 100), 1) if mem_total > 0 else 0
            }

            # Datastore stats
            datastores = []
            for ds in host.datastore:
                if ds.summary.accessible:
                    datastores.append({
                        'name': ds.summary.name,
                        'capacity': ds.summary.capacity,
                        'free': ds.summary.freeSpace,
                        'used': ds.summary.capacity - ds.summary.freeSpace,
                        'type': ds.summary.type
                    })

            return {
                'cpu': cpu_stats,
                'memory': memory_stats,
                'datastores': datastores
            }

        finally:
            client.disconnect()

    def get_cached_vms(self, server_id: str) -> Dict[str, Any]:
        """
        Get cached VM data for a server.

        Returns:
            {'vms': [...], 'cached': True, 'cache_age': seconds, 'error': str or None} or None if no cache
        """
        if server_id not in self._cache:
            return None

        cache_data = self._cache[server_id]
        age = time.time() - cache_data['timestamp']

        return {
            'vms': cache_data['vms'],
            'cached': True,
            'cache_age': round(age, 1),
            'error': cache_data.get('error')
        }

    def get_cached_stats(self, server_id: str) -> Dict[str, Any]:
        """
        Get cached stats data for a server.

        Returns:
            {'cpu': {...}, 'memory': {...}, 'datastores': [...], 'cache_age': seconds, 'error': str or None} or None if no cache
        """
        if server_id not in self._cache:
            return None

        cache_data = self._cache[server_id]
        age = time.time() - cache_data['timestamp']

        stats = cache_data.get('stats', {})
        if not stats:
            # Return error info even if no stats
            error = cache_data.get('error')
            if error:
                return {
                    'error': error,
                    'cache_age': round(age, 1)
                }
            return None

        result = dict(stats)
        result['cache_age'] = round(age, 1)
        result['error'] = cache_data.get('error')
        return result

    def invalidate_cache(self, server_id: str = None):
        """
        Invalidate cache for a specific server or all servers.

        Args:
            server_id: Server to invalidate, or None for all servers
        """
        if server_id:
            self._cache.pop(server_id, None)
            logger.info(f"Invalidated cache for server {server_id}")
        else:
            self._cache.clear()
            logger.info("Invalidated all cache")

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the background refresh service"""
        return {
            'running': self.running,
            'refresh_interval': self.refresh_interval,
            'cached_servers': len(self._cache),
            'cache_info': {
                server_id: {
                    'vm_count': len(data['vms']),
                    'age': round(time.time() - data['timestamp'], 1)
                }
                for server_id, data in self._cache.items()
            }
        }


# Global instance
_refresh_service = None


def get_refresh_service() -> BackgroundRefreshService:
    """Get or create the global background refresh service instance"""
    global _refresh_service
    if _refresh_service is None:
        import os
        interval = int(os.getenv('BACKGROUND_REFRESH_INTERVAL', 30))
        _refresh_service = BackgroundRefreshService(refresh_interval=interval)
    return _refresh_service
