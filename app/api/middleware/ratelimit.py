"""Per-identity rate limiting (PRD 29 RATE_LIMITED, 44 production hardening).

Sliding window in process memory: enough for a single replica and honest about
its limits — behind multiple replicas, point this at Redis instead.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict

from app.core.errors import AppError


class SlidingWindowLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self._limit = max(1, int(limit_per_minute))
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = {}

    def check(self, identity: str) -> None:
        now = time.time()
        window_start = now - 60.0
        with self._lock:
            bucket = self._hits.setdefault(identity, deque())
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= self._limit:
                retry_after = max(1, int(60 - (now - bucket[0])))
                raise AppError(
                    "RATE_LIMITED",
                    f"Rate limit of {self._limit} requests/minute exceeded",
                    details={"retry_after_seconds": retry_after},
                )
            bucket.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
