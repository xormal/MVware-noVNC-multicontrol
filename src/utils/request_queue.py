"""
ESXi Request Queue Manager

Manages concurrent requests to ESXi API with rate limiting, queuing, and priority support.
"""

import threading
import time
import queue
from contextlib import contextmanager
from typing import Callable, Any, Optional
import os
from enum import IntEnum
from dotenv import load_dotenv

load_dotenv()


class RequestPriority(IntEnum):
    """Request priority levels (lower number = higher priority)"""
    CRITICAL = 0   # Console tickets, VM power operations
    HIGH = 1       # VM list, VM info
    NORMAL = 2     # Thumbnails (first load)
    LOW = 3        # Thumbnail refresh


class PriorityQueueItem:
    """Item for priority queue with request tracking"""

    def __init__(self, priority: RequestPriority, request_id: int):
        self.priority = priority
        self.request_id = request_id
        self.event = threading.Event()
        self.timestamp = time.time()

    def __lt__(self, other):
        """Compare by priority first, then by timestamp (FIFO within priority)"""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp


class ESXiRequestQueue:
    """
    Thread-safe priority request queue for ESXi API calls.

    Supports multiple concurrent users with priority-based request handling.
    """

    def __init__(self, max_concurrent=None, min_interval=None):
        """
        Initialize request queue.

        Args:
            max_concurrent: Maximum concurrent requests (default: 8 for multi-user)
            min_interval: Minimum interval between requests in seconds (default: 0.05)
        """
        self.max_concurrent = max_concurrent or int(os.getenv('ESXI_MAX_CONCURRENT', 8))
        self.min_interval = min_interval or float(os.getenv('ESXI_MIN_INTERVAL', 0.05))

        # Priority queue for waiting requests
        self.wait_queue = queue.PriorityQueue()

        # Semaphore to limit concurrent requests
        self.semaphore = threading.Semaphore(self.max_concurrent)

        # Lock for last_request_time and request_counter
        self.lock = threading.Lock()
        self.last_request_time = 0
        self.request_counter = 0

        # Statistics per priority
        self.stats_lock = threading.Lock()
        self.total_requests = 0
        self.active_requests = 0
        self.waiting_requests = 0
        self.total_wait_time = 0
        self.priority_stats = {
            RequestPriority.CRITICAL: {'total': 0, 'wait_time': 0},
            RequestPriority.HIGH: {'total': 0, 'wait_time': 0},
            RequestPriority.NORMAL: {'total': 0, 'wait_time': 0},
            RequestPriority.LOW: {'total': 0, 'wait_time': 0},
        }

    @contextmanager
    def acquire(self, priority: RequestPriority = RequestPriority.NORMAL):
        """
        Context manager to acquire slot in request queue with priority.

        Args:
            priority: Request priority level

        Usage:
            with queue.acquire(RequestPriority.CRITICAL):
                # Make critical ESXi API call here
                ticket = esxi_client.acquire_webmks_ticket(vm)
        """
        wait_start = time.time()

        with self.stats_lock:
            self.waiting_requests += 1

        try:
            # Wait for available slot
            self.semaphore.acquire()

            # Enforce minimum interval between requests
            with self.lock:
                now = time.time()
                time_since_last = now - self.last_request_time

                if time_since_last < self.min_interval:
                    sleep_time = self.min_interval - time_since_last
                    time.sleep(sleep_time)

                self.last_request_time = time.time()

            wait_time = time.time() - wait_start

            with self.stats_lock:
                self.waiting_requests -= 1
                self.active_requests += 1
                self.total_requests += 1
                self.total_wait_time += wait_time
                self.priority_stats[priority]['total'] += 1
                self.priority_stats[priority]['wait_time'] += wait_time

            yield

        finally:
            with self.stats_lock:
                self.active_requests -= 1
            self.semaphore.release()

    def execute(self, func: Callable, priority: RequestPriority = RequestPriority.NORMAL, *args, **kwargs) -> Any:
        """
        Execute function with queue management.

        Args:
            func: Function to execute
            priority: Request priority level
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func(*args, **kwargs)
        """
        with self.acquire(priority):
            return func(*args, **kwargs)

    def get_stats(self):
        """Get queue statistics with priority breakdown"""
        with self.stats_lock:
            avg_wait = self.total_wait_time / self.total_requests if self.total_requests > 0 else 0

            priority_breakdown = {}
            for prio, stats in self.priority_stats.items():
                avg_prio_wait = stats['wait_time'] / stats['total'] if stats['total'] > 0 else 0
                priority_breakdown[prio.name] = {
                    'total_requests': stats['total'],
                    'avg_wait_time': round(avg_prio_wait, 3)
                }

            return {
                'max_concurrent': self.max_concurrent,
                'min_interval': self.min_interval,
                'active_requests': self.active_requests,
                'waiting_requests': self.waiting_requests,
                'total_requests': self.total_requests,
                'avg_wait_time': round(avg_wait, 3),
                'by_priority': priority_breakdown
            }


# Global queue instance
_global_queue = None


def get_queue():
    """Get global ESXi request queue instance"""
    global _global_queue
    if _global_queue is None:
        _global_queue = ESXiRequestQueue()
    return _global_queue


def reset_queue():
    """Reset global queue (useful for testing)"""
    global _global_queue
    _global_queue = None
