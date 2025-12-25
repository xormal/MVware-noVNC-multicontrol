"""
ESXi Connection Pool

Maintains a pool of persistent ESXi connections for multi-user support.
Prevents authentication issues and improves performance.
"""

import threading
import time
import queue
from typing import Optional
import os
from dotenv import load_dotenv

from src.utils.esxi_client import ESXiClient

load_dotenv()


class ESXiConnectionPool:
    """
    Thread-safe connection pool for ESXi clients.

    Maintains persistent connections with automatic reconnection on failure.
    """

    def __init__(self, pool_size=None, connection_ttl=None):
        """
        Initialize connection pool.

        Args:
            pool_size: Maximum number of connections (default: 5)
            connection_ttl: Connection time-to-live in seconds (default: 300)
        """
        self.pool_size = pool_size or int(os.getenv('ESXI_POOL_SIZE', 5))
        self.connection_ttl = connection_ttl or int(os.getenv('ESXI_CONNECTION_TTL', 300))

        self.pool = queue.Queue(maxsize=self.pool_size)
        self.lock = threading.Lock()
        self.stats = {
            'total_connections': 0,
            'active_connections': 0,
            'reconnects': 0,
            'errors': 0
        }
        self.initialized = False

    def _is_connection_valid(self, conn_info):
        """Check if connection is still valid"""
        # Check TTL
        age = time.time() - conn_info['created_at']
        if age > self.connection_ttl:
            return False

        # Check if connection is alive
        try:
            client = conn_info['client']
            if not client.si:
                return False
            # Try to get current time - if this fails, connection is dead
            client.si.CurrentTime()
            return True
        except:
            return False

    def acquire(self, timeout=10):
        """
        Acquire connection from pool.

        Args:
            timeout: Maximum time to wait for connection

        Returns:
            ESXiClient instance

        Raises:
            queue.Empty: If no connection available within timeout
        """
        try:
            # Try to get connection from pool
            try:
                conn_info = self.pool.get(timeout=0.1)  # Short timeout
            except queue.Empty:
                # No connection available - create new one
                client = ESXiClient()
                client.connect()
                conn_info = {
                    'client': client,
                    'created_at': time.time(),
                    'last_used': time.time()
                }
                with self.lock:
                    self.stats['total_connections'] += 1
                    self.initialized = True

            # Validate connection
            if not self._is_connection_valid(conn_info):
                # Connection invalid - reconnect
                try:
                    conn_info['client'].disconnect()
                except:
                    pass

                try:
                    client = ESXiClient()
                    client.connect()
                    conn_info = {
                        'client': client,
                        'created_at': time.time(),
                        'last_used': time.time()
                    }
                    with self.lock:
                        self.stats['reconnects'] += 1
                except Exception as e:
                    with self.lock:
                        self.stats['errors'] += 1
                    # Return connection to pool even if failed
                    self.pool.put(conn_info)
                    raise

            # Update last used time
            conn_info['last_used'] = time.time()

            with self.lock:
                self.stats['active_connections'] += 1

            return conn_info['client']

        except queue.Empty:
            with self.lock:
                self.stats['errors'] += 1
            raise Exception("No ESXi connections available in pool")

    def release(self, client):
        """
        Return connection to pool.

        Args:
            client: ESXiClient to return
        """
        try:
            # Return to pool
            self.pool.put({
                'client': client,
                'created_at': time.time(),
                'last_used': time.time()
            }, timeout=1)

            with self.lock:
                self.stats['active_connections'] -= 1

        except queue.Full:
            # Pool full - disconnect this connection
            try:
                client.disconnect()
            except:
                pass

    def get_stats(self):
        """Get pool statistics"""
        with self.lock:
            return {
                'pool_size': self.pool_size,
                'available_connections': self.pool.qsize(),
                'active_connections': self.stats['active_connections'],
                'total_connections': self.stats['total_connections'],
                'reconnects': self.stats['reconnects'],
                'errors': self.stats['errors']
            }

    def shutdown(self):
        """Shutdown pool and close all connections"""
        while not self.pool.empty():
            try:
                conn_info = self.pool.get_nowait()
                try:
                    conn_info['client'].disconnect()
                except:
                    pass
            except queue.Empty:
                break


class PooledConnection:
    """Context manager for pooled connections"""

    def __init__(self, pool):
        self.pool = pool
        self.client = None

    def __enter__(self):
        self.client = self.pool.acquire()
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            self.pool.release(self.client)


# Global pool instance
_global_pool = None


def get_pool():
    """Get global ESXi connection pool instance"""
    global _global_pool
    if _global_pool is None:
        _global_pool = ESXiConnectionPool()
    return _global_pool


def get_connection():
    """Get pooled connection context manager"""
    return PooledConnection(get_pool())
