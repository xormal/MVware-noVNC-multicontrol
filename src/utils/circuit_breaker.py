"""
Circuit Breaker for ESXi API Protection

Prevents cascading failures when ESXi is overloaded or unavailable.
"""

import time
import threading
from enum import Enum
import os
from dotenv import load_dotenv

load_dotenv()


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"          # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """
    Circuit breaker for ESXi API calls.

    States:
    - CLOSED: Normal operation, requests go through
    - OPEN: Too many failures, blocking requests
    - HALF_OPEN: Testing if service recovered
    """

    def __init__(self, failure_threshold=None, recovery_timeout=None, success_threshold=None):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening (default: 5)
            recovery_timeout: Seconds to wait before testing recovery (default: 30)
            success_threshold: Successful requests needed to close circuit (default: 3)
        """
        self.failure_threshold = failure_threshold or int(os.getenv('CB_FAILURE_THRESHOLD', 5))
        self.recovery_timeout = recovery_timeout or int(os.getenv('CB_RECOVERY_TIMEOUT', 30))
        self.success_threshold = success_threshold or int(os.getenv('CB_SUCCESS_THRESHOLD', 3))

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.opened_at = None

        self.lock = threading.Lock()

    def call(self, func, *args, **kwargs):
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result of func(*args, **kwargs)

        Raises:
            CircuitBreakerOpen: If circuit is open
            Original exception if func fails
        """
        with self.lock:
            # Check if we should transition to HALF_OPEN
            if self.state == CircuitState.OPEN:
                if time.time() - self.opened_at >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                else:
                    raise CircuitBreakerOpen(
                        f"Circuit breaker is OPEN. "
                        f"Retry in {int(self.recovery_timeout - (time.time() - self.opened_at))}s"
                    )

            # Block requests if OPEN
            if self.state == CircuitState.OPEN:
                raise CircuitBreakerOpen("Circuit breaker is OPEN")

        # Execute request
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        """Handle successful request"""
        with self.lock:
            self.failure_count = 0
            self.last_failure_time = None

            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    # Recovery successful - close circuit
                    self.state = CircuitState.CLOSED
                    self.success_count = 0

    def _on_failure(self):
        """Handle failed request"""
        with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            # If in HALF_OPEN, immediately reopen
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.opened_at = time.time()
                return

            # If failure threshold reached, open circuit
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.time()

    def get_state(self):
        """Get current circuit state"""
        with self.lock:
            return {
                'state': self.state.value,
                'failure_count': self.failure_count,
                'success_count': self.success_count,
                'failure_threshold': self.failure_threshold,
                'recovery_timeout': self.recovery_timeout,
                'opened_at': self.opened_at,
                'time_until_retry': max(0, int(self.recovery_timeout - (time.time() - self.opened_at)))
                    if self.opened_at else None
            }

    def reset(self):
        """Manually reset circuit breaker"""
        with self.lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = None
            self.opened_at = None


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open"""
    pass


# Global circuit breaker instance
_global_breaker = None


def get_breaker():
    """Get global circuit breaker instance"""
    global _global_breaker
    if _global_breaker is None:
        _global_breaker = CircuitBreaker()
    return _global_breaker
