"""Per-identity rate limiting (PRD 29 RATE_LIMITED, 44 production hardening).

Sliding window in process memory: enough for a single replica and honest about
its limits — behind multiple replicas, point this at Redis instead.

Batas dibaca dari setelan yang sedang berlaku pada SETIAP permintaan, jadi mengubahnya di
layar Setelan (bagian "limits") langsung berlaku tanpa restart. Nilai 0 berarti tanpa batas.

* ``rate_limit_per_minute``        - per aplikasi/kunci: /query, /search, /extract, tulis knowledge
* ``rate_limit_read_per_minute``   - per aplikasi/kunci: daftar & status knowledge (yang sering di-poll)
* ``rate_limit_global_per_minute`` - semua pemanggil digabung, untuk semua endpoint di atas
* ``max_concurrent_queries``       - /query, /search, /extract yang boleh berjalan bersamaan
  (pekerjaan inilah yang memakan CPU: embedding, rerank, panggilan model)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, Iterator, Union

from starlette.requests import Request

from app.core.errors import AppError

LimitSource = Union[int, Callable[[], int]]


def _read_limit(source: LimitSource) -> int:
    try:
        value = int(source() if callable(source) else source)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


class SlidingWindowLimiter:
    def __init__(self, limit_per_minute: LimitSource, *, label: str = "") -> None:
        self._limit = limit_per_minute
        self._label = label
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = {}

    def check(self, identity: str) -> None:
        limit = _read_limit(self._limit)
        if limit <= 0:
            return
        now = time.time()
        window_start = now - 60.0
        with self._lock:
            bucket = self._hits.setdefault(identity, deque())
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(60 - (now - bucket[0])))
                what = f" {self._label}" if self._label else ""
                raise AppError(
                    "RATE_LIMITED",
                    f"Rate limit{what} of {limit} requests/minute exceeded",
                    details={"retry_after_seconds": retry_after, "limit_per_minute": limit},
                )
            bucket.append(now)
            # buang ember identitas yang sudah lama diam supaya memori tidak tumbuh terus
            if len(self._hits) > 1000:
                for key in [k for k, b in self._hits.items() if not b or b[-1] < window_start]:
                    del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class ConcurrencyGate:
    """Batas jumlah pekerjaan berat yang berjalan bersamaan. Yang melebihi langsung ditolak
    (429) alih-alih mengantre: antrean panjang hanya memindahkan beban, bukan mengurangi."""

    def __init__(self, limit: LimitSource) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._running = 0

    @property
    def running(self) -> int:
        return self._running

    @contextmanager
    def slot(self) -> Iterator[None]:
        limit = _read_limit(self._limit)
        with self._lock:
            if limit > 0 and self._running >= limit:
                raise AppError(
                    "RATE_LIMITED",
                    f"Server is busy: {limit} queries already running, try again shortly",
                    details={"retry_after_seconds": 2, "max_concurrent_queries": limit},
                )
            self._running += 1
        try:
            yield
        finally:
            with self._lock:
                self._running -= 1


class ServiceLimits:
    """Satu set limiter untuk seluruh layanan, membaca batas dari objek Settings yang hidup."""

    def __init__(self, settings: Any) -> None:
        get = lambda name: (lambda: getattr(settings, name, 0))  # noqa: E731
        self.heavy = SlidingWindowLimiter(get("rate_limit_per_minute"))
        self.read = SlidingWindowLimiter(get("rate_limit_read_per_minute"), label="(read)")
        self.total = SlidingWindowLimiter(get("rate_limit_global_per_minute"), label="(all clients)")
        self.queries = ConcurrencyGate(get("max_concurrent_queries"))

    def check(self, identity: str, kind: str = "heavy") -> None:
        (self.read if kind == "read" else self.heavy).check(identity or "anon")
        self.total.check("*")


_shared: Dict[int, tuple] = {}
_shared_lock = threading.Lock()


def limits_for(settings: Any) -> ServiceLimits:
    """Limiter bersama per objek Settings (satu per proses; tes membuat Settings sendiri)."""
    key = id(settings)
    with _shared_lock:
        found = _shared.get(key)
        if found is None or found[0] is not settings:
            found = _shared[key] = (settings, ServiceLimits(settings))
        return found[1]


def query_slot(request: Request) -> Iterator[None]:
    """FastAPI dependency: memegang satu slot ``max_concurrent_queries`` selama permintaan berjalan."""
    from app.api.deps import services_from_request

    with limits_for(services_from_request(request).settings).queries.slot():
        yield
