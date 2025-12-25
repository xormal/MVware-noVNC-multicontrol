"""
Background thumbnail refresh service.

Continuously updates VM thumbnails in background with controlled ESXi load.
"""

import threading
import time
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


class ThumbnailRefreshService:
    """Background service for refreshing VM thumbnails."""

    def __init__(self, app, esxi_client_factory, thumbnail_cache_ttl=120):
        """
        Initialize thumbnail refresh service.

        Args:
            app: Flask app instance (for config and globals)
            esxi_client_factory: Function to create ESXi client
            thumbnail_cache_ttl: TTL for thumbnail cache in seconds
        """
        self.app = app
        self.esxi_client_factory = esxi_client_factory
        self.thumbnail_cache_ttl = thumbnail_cache_ttl

        self._threads = {}  # {server_id: thread}
        self._stop_flags = {}  # {server_id: threading.Event}
        self._stats = {}  # {server_id: stats}
        self._lock = threading.Lock()

        # Configuration
        self.batch_size = 2  # VMs to refresh in parallel
        self.batch_delay_min = 0.5  # Minimum delay between batches (seconds)
        self.batch_delay_max = 10.0  # Maximum delay between batches (seconds)
        self.cycle_delay = 60  # Delay between full cycles (seconds)

        # Adaptive rate limiting with ESXi feedback
        self._current_delays = {}  # {server_id: current_delay}
        self._consecutive_errors = {}  # {server_id: error_count}
        self._request_timestamps = {}  # {server_id: [timestamps]}
        self._timeout_history = {}  # {server_id: [(timeout_sec, requests_count, time_elapsed)]}
        self._max_history_size = 10  # Keep last 10 measurements

    def start_server_refresh(self, server_id: str, server_config: dict):
        """Start background refresh for a server."""
        with self._lock:
            if server_id in self._threads and self._threads[server_id].is_alive():
                logger.info(f"Thumbnail refresh already running for {server_id}")
                return

            # Create stop flag
            stop_flag = threading.Event()
            self._stop_flags[server_id] = stop_flag

            # Initialize adaptive delay and tracking
            self._current_delays[server_id] = self.batch_delay_min
            self._consecutive_errors[server_id] = 0
            self._request_timestamps[server_id] = []
            self._timeout_history[server_id] = []

            # Initialize stats
            self._stats[server_id] = {
                'started_at': time.time(),
                'cycles': 0,
                'thumbnails_refreshed': 0,
                'errors': 0,
                'last_cycle_at': None,
                'last_cycle_duration': None,
                'current_delay': self.batch_delay_min
            }

            # Start thread
            thread = threading.Thread(
                target=self._refresh_loop,
                args=(server_id, server_config, stop_flag),
                daemon=True,
                name=f'ThumbnailRefresh-{server_id[:8]}'
            )
            thread.start()
            self._threads[server_id] = thread

            logger.info(f"Started thumbnail refresh for server {server_id}")

    def stop_server_refresh(self, server_id: str):
        """Stop background refresh for a server."""
        with self._lock:
            if server_id in self._stop_flags:
                self._stop_flags[server_id].set()
                logger.info(f"Stopping thumbnail refresh for {server_id}")

    def _refresh_loop(self, server_id: str, server_config: dict, stop_flag: threading.Event):
        """Main refresh loop for a server."""
        logger.info(f"Thumbnail refresh loop started for {server_id}")

        while not stop_flag.is_set():
            cycle_start = time.time()

            try:
                # Get list of powered-on VMs from background cache
                powered_on_vms = self._get_powered_on_vms(server_id)

                if not powered_on_vms:
                    logger.debug(f"No powered-on VMs for {server_id}, waiting...")
                    stop_flag.wait(self.cycle_delay)
                    continue

                # Refresh thumbnails in batches
                refreshed = self._refresh_thumbnails_batch(
                    server_id, server_config, powered_on_vms, stop_flag
                )

                # Update stats
                cycle_duration = time.time() - cycle_start
                current_delay = self._current_delays.get(server_id, self.batch_delay_min)
                consecutive_errors = self._consecutive_errors.get(server_id, 0)

                with self._lock:
                    self._stats[server_id]['cycles'] += 1
                    self._stats[server_id]['thumbnails_refreshed'] += refreshed
                    self._stats[server_id]['last_cycle_at'] = time.time()
                    self._stats[server_id]['last_cycle_duration'] = cycle_duration

                logger.info(
                    f"Thumbnail refresh cycle for {server_id}: "
                    f"{refreshed}/{len(powered_on_vms)} VMs in {cycle_duration:.1f}s, "
                    f"delay: {current_delay:.2f}s, errors: {consecutive_errors}"
                )

            except Exception as e:
                logger.error(f"Error in thumbnail refresh loop for {server_id}: {e}")
                with self._lock:
                    self._stats[server_id]['errors'] += 1

            # Wait before next cycle
            if not stop_flag.is_set():
                logger.debug(f"Waiting {self.cycle_delay}s before next cycle for {server_id}")
                stop_flag.wait(self.cycle_delay)

        logger.info(f"Thumbnail refresh loop stopped for {server_id}")

    def _get_powered_on_vms(self, server_id: str) -> List[dict]:
        """Get list of powered-on VMs from background refresh cache."""
        try:
            # Use background refresh service to get cached VMs
            from ..utils.background_refresh import get_refresh_service

            refresh_service = get_refresh_service()
            cached_data = refresh_service.get_cached_vms(server_id)

            if cached_data:
                vms = cached_data.get('vms', [])
                return [vm for vm in vms if vm.get('power_state') == 'poweredOn']

        except Exception as e:
            logger.warning(f"Failed to get VMs from refresh service: {e}")

        return []

    def _parse_timeout_from_error(self, error_msg: str) -> float:
        """Parse timeout value from ESXi error message."""
        import re
        # Look for "connect timeout=X" or "timeout=X"
        match = re.search(r'timeout[=\s]+(\d+(?:\.\d+)?)', error_msg, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None

    def _track_request(self, server_id: str):
        """Track timestamp of a request."""
        now = time.time()
        with self._lock:
            if server_id not in self._request_timestamps:
                self._request_timestamps[server_id] = []

            # Keep only recent requests (last 60 seconds)
            cutoff = now - 60
            self._request_timestamps[server_id] = [
                ts for ts in self._request_timestamps[server_id] if ts > cutoff
            ]
            self._request_timestamps[server_id].append(now)

    def _calculate_optimal_delay(self, server_id: str, timeout_sec: float):
        """Calculate optimal delay based on ESXi timeout feedback."""
        with self._lock:
            timestamps = self._request_timestamps.get(server_id, [])
            if len(timestamps) < 2:
                # Not enough data, use conservative approach
                return min(timeout_sec, self.batch_delay_max)

            # Calculate how many requests we made and in what time
            now = time.time()
            recent_requests = [ts for ts in timestamps if ts > now - timeout_sec]
            requests_count = len(recent_requests)

            if requests_count > 0:
                time_elapsed = now - recent_requests[0]

                # Store this measurement
                if server_id not in self._timeout_history:
                    self._timeout_history[server_id] = []

                self._timeout_history[server_id].append((timeout_sec, requests_count, time_elapsed))

                # Keep only recent history
                if len(self._timeout_history[server_id]) > self._max_history_size:
                    self._timeout_history[server_id].pop(0)

                # Filter outliers (values > 3x median)
                history = self._timeout_history[server_id]
                if len(history) >= 3:
                    delays = [h[0] for h in history]
                    median_delay = sorted(delays)[len(delays) // 2]
                    history = [h for h in history if h[0] <= median_delay * 3]
                    self._timeout_history[server_id] = history

                # Calculate average rate from history
                if history:
                    avg_requests = sum(h[1] for h in history) / len(history)
                    avg_time = sum(h[2] for h in history) / len(history)

                    # Reduce by 20% for safety margin (send 20% fewer requests)
                    safe_requests = max(1, int(avg_requests * 0.8))
                    optimal_delay = (timeout_sec / safe_requests) if safe_requests > 0 else timeout_sec

                    logger.info(
                        f"ESXi rate limit for {server_id}: "
                        f"{safe_requests} requests per {timeout_sec}s, "
                        f"optimal delay: {optimal_delay:.2f}s"
                    )

                    return min(optimal_delay, self.batch_delay_max)

            # Fallback to timeout value
            return min(timeout_sec, self.batch_delay_max)

    def _adjust_delay_on_error(self, server_id: str, error_msg: str):
        """Adjust delay based on ESXi error feedback."""
        # Try to parse timeout from error
        timeout = self._parse_timeout_from_error(error_msg)

        with self._lock:
            self._consecutive_errors[server_id] = self._consecutive_errors.get(server_id, 0) + 1

            if timeout:
                # Use smart calculation based on ESXi feedback
                new_delay = self._calculate_optimal_delay(server_id, timeout)
                logger.info(
                    f"ESXi timeout detected ({timeout}s): adjusting delay to {new_delay:.2f}s"
                )
            else:
                # For 503/rate limit errors without explicit timeout, use request history
                timestamps = self._request_timestamps.get(server_id, [])
                if len(timestamps) >= 2:
                    # Calculate rate from actual requests
                    now = time.time()
                    recent = [ts for ts in timestamps if ts > now - 10]  # Last 10 seconds
                    if len(recent) >= 2:
                        # We got rate limited, so slow down based on actual rate
                        time_window = now - recent[0]
                        request_rate = len(recent) / time_window if time_window > 0 else 1

                        # Increase delay to reduce rate by 50%
                        current = self._current_delays.get(server_id, self.batch_delay_min)
                        new_delay = min(current * 2, self.batch_delay_max)

                        logger.info(
                            f"ESXi rate limit for {server_id}: "
                            f"{len(recent)} requests in {time_window:.1f}s, "
                            f"increasing delay to {new_delay:.2f}s"
                        )
                    else:
                        # Exponential backoff fallback
                        current = self._current_delays.get(server_id, self.batch_delay_min)
                        new_delay = min(current * 2, self.batch_delay_max)
                else:
                    # Exponential backoff fallback
                    current = self._current_delays.get(server_id, self.batch_delay_min)
                    new_delay = min(current * 2, self.batch_delay_max)

                if self._consecutive_errors[server_id] % 5 == 0:
                    logger.warning(
                        f"ESXi overload for {server_id}: "
                        f"{self._consecutive_errors[server_id]} errors, delay: {new_delay:.1f}s"
                    )

            self._current_delays[server_id] = new_delay
            self._stats[server_id]['current_delay'] = new_delay

    def _adjust_delay_on_success(self, server_id: str):
        """Gradually decrease delay when requests succeed."""
        with self._lock:
            self._consecutive_errors[server_id] = 0
            current = self._current_delays.get(server_id, self.batch_delay_min)

            # If we have history, use it to optimize
            history = self._timeout_history.get(server_id, [])
            if history and len(history) >= 3:
                # We have enough data, stay near calculated optimal
                # Just decrease by 5% to slowly test if we can go faster
                new_delay = max(current * 0.95, self.batch_delay_min)
            else:
                # No history yet, decrease faster
                new_delay = max(current * 0.9, self.batch_delay_min)

            self._current_delays[server_id] = new_delay
            self._stats[server_id]['current_delay'] = new_delay

    def _refresh_thumbnails_batch(
        self,
        server_id: str,
        server_config: dict,
        vms: List[dict],
        stop_flag: threading.Event
    ) -> int:
        """Refresh thumbnails for VMs in batches with adaptive rate limiting."""
        refreshed = 0
        batch_errors = 0

        for i in range(0, len(vms), self.batch_size):
            if stop_flag.is_set():
                break

            batch = vms[i:i + self.batch_size]

            # Refresh batch
            for vm in batch:
                if stop_flag.is_set():
                    break

                try:
                    # Track this request
                    self._track_request(server_id)

                    self._refresh_single_thumbnail(server_id, server_config, vm['moid'])
                    refreshed += 1
                    self._adjust_delay_on_success(server_id)
                except Exception as e:
                    error_msg = str(e)
                    # Check if it's a timeout or rate limit error
                    if 'timeout' in error_msg.lower() or '503' in error_msg or 'Service Unavailable' in error_msg:
                        self._adjust_delay_on_error(server_id, error_msg)
                        batch_errors += 1
                        logger.debug(f"Thumbnail refresh {server_id}/{vm['moid']}: rate limit/timeout, delay increased")
                    else:
                        logger.warning(f"Failed to refresh thumbnail {server_id}/{vm['moid']}: {e}")

                    with self._lock:
                        self._stats[server_id]['errors'] += 1

            # Adaptive delay between batches
            if i + self.batch_size < len(vms) and not stop_flag.is_set():
                current_delay = self._current_delays.get(server_id, self.batch_delay_min)
                time.sleep(current_delay)

        # Log batch summary if there were errors
        if batch_errors > 0:
            current_delay = self._current_delays.get(server_id, self.batch_delay_min)
            history = self._timeout_history.get(server_id, [])
            history_info = f", {len(history)} measurements" if history else ""
            logger.info(
                f"Batch for {server_id}: {refreshed} succeeded, {batch_errors} failed, "
                f"delay: {current_delay:.2f}s{history_info}"
            )

        return refreshed

    def _refresh_single_thumbnail(self, server_id: str, server_config: dict, moid: str):
        """Refresh a single thumbnail."""
        cache_key = f"thumbnail_{server_id}_{moid}"

        # Create ESXi client
        client = self.esxi_client_factory(server_config)

        try:
            client.connect()

            # Get VM
            vm = client.get_vm_by_moid(moid)
            if not vm:
                return

            # Check if powered on
            from pyVmomi import vim as pyvim
            if vm.runtime.powerState != pyvim.VirtualMachinePowerState.poweredOn:
                return

            # Create screenshot
            screenshot_data = client.create_screenshot(vm)

            # Only update if we got valid screenshot data
            if screenshot_data and len(screenshot_data) > 0:
                try:
                    # Resize to JPEG
                    from PIL import Image
                    import io

                    img = Image.open(io.BytesIO(screenshot_data))

                    # Verify image is valid and has content
                    if img.size[0] > 0 and img.size[1] > 0:
                        img.thumbnail((200, 150), Image.Resampling.LANCZOS)

                        output = io.BytesIO()
                        img.convert('RGB').save(output, format='JPEG', quality=50, optimize=True)
                        resized_data = output.getvalue()

                        # Only update cache if we got valid resized data
                        if resized_data and len(resized_data) > 100:  # Sanity check: valid JPEG should be > 100 bytes
                            from ..utils.shared_cache import set_thumbnail
                            set_thumbnail(cache_key, resized_data)
                            logger.debug(f"Refreshed thumbnail {server_id}/{moid}: {len(resized_data)} bytes")
                        else:
                            logger.warning(f"Invalid thumbnail data size for {server_id}/{moid}, keeping old thumbnail")
                    else:
                        logger.warning(f"Invalid image dimensions for {server_id}/{moid}, keeping old thumbnail")

                except Exception as img_error:
                    # If image processing fails, keep the old thumbnail
                    logger.warning(f"Failed to process thumbnail image {server_id}/{moid}: {img_error}, keeping old thumbnail")

        finally:
            client.disconnect()

    def get_stats(self) -> Dict:
        """Get statistics for all servers."""
        with self._lock:
            return dict(self._stats)

    def get_server_stats(self, server_id: str) -> Dict:
        """Get statistics for a specific server."""
        with self._lock:
            return self._stats.get(server_id, {})
