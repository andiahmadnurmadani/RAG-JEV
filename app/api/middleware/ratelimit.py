"""Per-identity rate limiting (PRD 29 RATE_LIMITED, 44 production hardening).

Sliding window in process memory: enough for a single replica and honest about
its limits — behind multiple replicas, point this at Redis instead.

Batas dibaca dari setelan yang sedang berlaku pada SETIAP permintaan, jadi mengubahnya di
layar Setelan (bagian "limits") langsung berlaku tanpa restart. Nilai 0 berarti tanpa batas.

* ``rate_limit_per_minute``        - per kunci/sesi (lihat ``TrustedContext.limit_identity``): /query, /search, /extract, tulis knowledge
* ``rate_limit_read_per_minute``   - per kunci/sesi: daftar & status knowledge (yang sering di-poll)
* ``rate_limit_global_per_minute`` - semua pemanggil digabung, untuk semua endpoint di atas
* ``max_concurrent_queries``       - /query, /search, /extract yang boleh berjalan bersamaan
  (pekerjaan inilah yang memakan CPU: embedding, rerank, panggilan model)
* ``max_concurrent_queries_per_key`` - dari jumlah itu, paling banyak sekian per lingkup
  (``max_concurrent_scope``: per knowledge base, atau per kunci/sesi)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, Iterator, Optional, Union

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
    """Batas jumlah pekerjaan berat yang berjalan bersamaan, total dan per identitas. Yang
    melebihi langsung ditolak (429) alih-alih mengantre: antrean panjang hanya memindahkan
    beban, bukan mengurangi."""

    def __init__(self, limit: LimitSource, per_identity: LimitSource = 0) -> None:
        self._limit = limit
        self._per_identity = per_identity
        self._lock = threading.Lock()
        self._running = 0
        self._by_identity: Dict[str, int] = {}

    @property
    def running(self) -> int:
        return self._running

    def running_for(self, identity: str) -> int:
        return self._by_identity.get(identity, 0)

    def acquire(self, identity: str = "", *, subject: str = "This key", unit: str = "key", scope: str = "key") -> None:
        limit = _read_limit(self._limit)
        per_identity = _read_limit(self._per_identity)
        with self._lock:
            mine = self._by_identity.get(identity, 0)
            if per_identity > 0 and mine >= per_identity:
                raise AppError(
                    "RATE_LIMITED",
                    f"{subject} already has {mine} queries running (limit {per_identity} per {unit}), "
                    "wait for one to finish",
                    details={"retry_after_seconds": 5, "max_concurrent_per_key": per_identity, "scope": scope},
                )
            if limit > 0 and self._running >= limit:
                raise AppError(
                    "RATE_LIMITED",
                    f"Server is busy: {limit} queries already running, try again shortly",
                    details={"retry_after_seconds": 2, "max_concurrent_queries": limit},
                )
            self._running += 1
            self._by_identity[identity] = mine + 1

    def release(self, identity: str = "") -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            left = self._by_identity.get(identity, 0) - 1
            if left > 0:
                self._by_identity[identity] = left
            else:
                self._by_identity.pop(identity, None)

    @contextmanager
    def slot(self, identity: str = "") -> Iterator[None]:
        self.acquire(identity)
        try:
            yield
        finally:
            self.release(identity)


class QuerySlot:
    """Slot yang diambil handler SETELAH mencatat KB & pertanyaan ke log permintaan (supaya
    penolakan 429 tetap tercatat lengkap), dan dilepas oleh dependency ``query_slot`` - jadi
    tetap lepas walau handler gagal di tengah jalan."""

    def __init__(self, gate: ConcurrencyGate) -> None:
        self._gate = gate
        self._identity: Optional[str] = None

    def acquire(self, identity: str, **label: str) -> None:
        if self._identity is not None:
            return
        self._gate.acquire(identity, **label)
        self._identity = identity

    def acquire_for(self, settings: Any, context: Any, knowledge_base_id: Optional[str]) -> None:
        """Ambil slot sesuai ``max_concurrent_scope``: per knowledge base (bawaan) atau per kunci.
        Tanpa KB (pertanyaan ke seluruh organisasi) dihitung sebagai satu lingkup "*"."""
        if str(getattr(settings, "max_concurrent_scope", "knowledge_base")) == "key":
            self.acquire(context.limit_identity)
            return
        kb = str(knowledge_base_id or "*")
        self.acquire(
            f"kb:{context.organization_id}:{kb}",
            subject="Knowledge base '%s'" % kb if kb != "*" else "Queries without a knowledge base",
            unit="knowledge base",
            scope="knowledge_base",
        )

    def release(self) -> None:
        if self._identity is not None:
            self._gate.release(self._identity)
            self._identity = None


class ServiceLimits:
    """Satu set limiter untuk seluruh layanan, membaca batas dari objek Settings yang hidup."""

    def __init__(self, settings: Any) -> None:
        get = lambda name: (lambda: getattr(settings, name, 0))  # noqa: E731
        self.heavy = SlidingWindowLimiter(get("rate_limit_per_minute"))
        self.read = SlidingWindowLimiter(get("rate_limit_read_per_minute"), label="(read)")
        self.total = SlidingWindowLimiter(get("rate_limit_global_per_minute"), label="(all clients)")
        self.queries = ConcurrencyGate(get("max_concurrent_queries"), get("max_concurrent_queries_per_key"))
        # /vision: antrean sendiri, supaya pembacaan gambar yang lama tidak menghabiskan slot /query
        self.vision = ConcurrencyGate(get("max_concurrent_vision"), get("max_concurrent_vision_per_key"))

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


def vision_slot(request: Request) -> Iterator[QuerySlot]:
    """Seperti :func:`query_slot`, untuk gerbang /vision."""
    from app.api.deps import services_from_request

    slot = QuerySlot(limits_for(services_from_request(request).settings).vision)
    try:
        yield slot
    finally:
        slot.release()


def query_slot(request: Request) -> Iterator[QuerySlot]:
    """FastAPI dependency: handler memanggil ``slot.acquire(identitas)``; slotnya dilepas di sini
    setelah permintaan selesai."""
    from app.api.deps import services_from_request

    slot = QuerySlot(limits_for(services_from_request(request).settings).queries)
    try:
        yield slot
    finally:
        slot.release()
