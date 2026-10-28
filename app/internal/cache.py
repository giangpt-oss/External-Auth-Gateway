import threading
import time
from typing import Any


class IdentityVerificationCache:
    """Thread-safe in-memory cache for Odoo verification and permissions with 60s TTL."""

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            timestamp, data = entry
            if time.time() - timestamp > self._ttl:
                del self._cache[key]
                return None
            return dict(data)

    def set(self, key: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._cache[key] = (time.time(), dict(data))

    def invalidate(self, key_prefix: str) -> int:
        """Invalidate any cache entry starting with key_prefix."""
        with self._lock:
            to_remove = [k for k in self._cache if k.startswith(key_prefix)]
            for k in to_remove:
                del self._cache[k]
            return len(to_remove)

    def invalidate_user(self, user_id: int) -> int:
        return self.invalidate(f"{user_id}:")

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)


identity_cache = IdentityVerificationCache(ttl_seconds=60.0)
