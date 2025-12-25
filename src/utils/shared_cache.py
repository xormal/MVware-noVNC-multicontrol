"""
Shared cache storage for thumbnails.

Provides a single global cache accessible from all modules.
"""

import threading
import time
from typing import Optional, Dict, Any

# Global cache storage
_cache_lock = threading.Lock()
_thumbnail_cache: Dict[str, bytes] = {}
_thumbnail_timestamps: Dict[str, float] = {}


def set_thumbnail(cache_key: str, data: bytes) -> None:
    """
    Store thumbnail data in cache.

    Args:
        cache_key: Cache key (e.g., "thumbnail_{server_id}_{moid}")
        data: Thumbnail image data (JPEG bytes)
    """
    with _cache_lock:
        _thumbnail_cache[cache_key] = data
        _thumbnail_timestamps[cache_key] = time.time()


def get_thumbnail(cache_key: str) -> Optional[bytes]:
    """
    Get thumbnail data from cache.

    Args:
        cache_key: Cache key (e.g., "thumbnail_{server_id}_{moid}")

    Returns:
        Thumbnail data or None if not in cache
    """
    with _cache_lock:
        return _thumbnail_cache.get(cache_key)


def get_thumbnail_timestamp(cache_key: str) -> float:
    """
    Get timestamp when thumbnail was cached.

    Args:
        cache_key: Cache key

    Returns:
        Timestamp or 0 if not in cache
    """
    with _cache_lock:
        return _thumbnail_timestamps.get(cache_key, 0)


def has_thumbnail(cache_key: str) -> bool:
    """
    Check if thumbnail exists in cache.

    Args:
        cache_key: Cache key

    Returns:
        True if exists, False otherwise
    """
    with _cache_lock:
        return cache_key in _thumbnail_cache


def get_all_thumbnails() -> Dict[str, bytes]:
    """
    Get all thumbnails in cache.

    Returns:
        Dictionary of {cache_key: data}
    """
    with _cache_lock:
        return dict(_thumbnail_cache)


def clear_cache(server_id: Optional[str] = None) -> None:
    """
    Clear thumbnail cache.

    Args:
        server_id: If provided, only clear thumbnails for this server
    """
    with _cache_lock:
        if server_id:
            # Clear only thumbnails for specific server
            prefix = f"thumbnail_{server_id}_"
            keys_to_delete = [k for k in _thumbnail_cache.keys() if k.startswith(prefix)]
            for key in keys_to_delete:
                del _thumbnail_cache[key]
                _thumbnail_timestamps.pop(key, None)
        else:
            # Clear all
            _thumbnail_cache.clear()
            _thumbnail_timestamps.clear()
