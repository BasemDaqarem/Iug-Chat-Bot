"""
In-process TTL + LRU cache with hit/miss stats.

Thread-safe because FastAPI runs the (sync) chat routes in a threadpool, so
several requests can touch the same cache concurrently. Deliberately a single
small class: swapping to Redis later means re-implementing just this surface
(get / set / clear / stats), exactly like SessionStore.

This module holds NO policy about *what* is safe to cache — callers decide
that (see app.chatbot). It is a dumb, correct key→value store with eviction.
"""

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:

    def __init__(self, name: str, maxsize: int, ttl: int):
        self.name = name
        self.maxsize = maxsize
        self.ttl = ttl  # seconds; 0 or None → entries never expire on time
        self._data: "OrderedDict[str, tuple]" = OrderedDict()  # key -> (expires_at, value)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            expires_at, value = item
            if self.ttl and time.monotonic() > expires_at:
                del self._data[key]
                self._expirations += 1
                self._misses += 1
                return None
            self._data.move_to_end(key)  # LRU: mark most-recently-used
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            expires_at = time.monotonic() + self.ttl if self.ttl else float("inf")
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # evict least-recently-used
                self._evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "name": self.name,
                "size": len(self._data),
                "maxsize": self.maxsize,
                "ttl_seconds": self.ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "evictions": self._evictions,
                "expirations": self._expirations,
            }
