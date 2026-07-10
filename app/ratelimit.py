"""
Tiny in-process fixed-window rate limiter.

Thread-safe (chat routes run in FastAPI's threadpool). Per-process only — with
multiple instances each has its own window; move the counter to Redis when
scaling out. Keyed by whatever the caller passes (authenticated student id for
chat, client IP for login).
"""

import threading
import time
from typing import Tuple


class RateLimiter:

    def __init__(self, max_per_window: int, window_seconds: int = 60):
        self.max = max_per_window
        self.window = window_seconds
        self._hits: dict = {}          # key -> [window_start, count]
        self._lock = threading.Lock()

    def check(self, key: str) -> Tuple[bool, int]:
        """Register a hit for `key`. Returns (allowed, retry_after_seconds).
        retry_after is 0 when allowed."""
        with self._lock:
            now = time.monotonic()
            start, count = self._hits.get(key, (now, 0))
            if now - start >= self.window:      # window elapsed → reset
                start, count = now, 0
            count += 1
            self._hits[key] = (start, count)
            if count > self.max:
                return False, int(self.window - (now - start)) + 1
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
