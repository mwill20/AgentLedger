"""Small in-process TTL cache for hot read paths."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


_CACHE: dict[str, _CacheEntry] = {}
_LOCK = Lock()


def get(key: str) -> Any | None:
    """Return one cached value when it is still fresh."""
    now = monotonic()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _CACHE.pop(key, None)
            return None
        return deepcopy(entry.value)


def set(key: str, value: Any, ttl_seconds: float) -> None:
    """Store one cached value for a bounded TTL."""
    with _LOCK:
        _CACHE[key] = _CacheEntry(
            expires_at=monotonic() + max(ttl_seconds, 0.0),
            value=deepcopy(value),
        )


def invalidate_prefix(prefix: str) -> None:
    """Drop all cache entries with the provided prefix."""
    with _LOCK:
        stale_keys = [key for key in _CACHE if key.startswith(prefix)]
        for key in stale_keys:
            _CACHE.pop(key, None)
