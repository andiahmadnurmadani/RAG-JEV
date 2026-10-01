"""In-process metrics registry (PRD 36, 37). Exposed as JSON at /metrics."""

from __future__ import annotations

import statistics
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List

_LOCK = threading.Lock()
_COUNTERS: Dict[str, int] = {}
_GAUGES: Dict[str, float] = {}
_SAMPLES: Dict[str, List[float]] = {}
_MAX_SAMPLES = 2048


def incr(name: str, value: int = 1) -> None:
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + value


def gauge(name: str, value: float) -> None:
    with _LOCK:
        _GAUGES[name] = float(value)


def observe(name: str, value: float) -> None:
    with _LOCK:
        bucket = _SAMPLES.setdefault(name, [])
        bucket.append(float(value))
        if len(bucket) > _MAX_SAMPLES:
            del bucket[: len(bucket) - _MAX_SAMPLES]


@contextmanager
def timer(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        observe(name, time.perf_counter() - started)


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * len(ordered))) - 1))
    return ordered[index]


def snapshot() -> Dict[str, object]:
    with _LOCK:
        counters = dict(_COUNTERS)
        gauges = dict(_GAUGES)
        latencies = {name: list(values) for name, values in _SAMPLES.items()}
    latency_stats = {
        name: {
            "count": len(values),
            "avg_ms": round(statistics.fmean(values) * 1000, 2) if values else 0.0,
            "p95_ms": round(_percentile(values, 95) * 1000, 2),
            "p99_ms": round(_percentile(values, 99) * 1000, 2),
            "max_ms": round(max(values) * 1000, 2) if values else 0.0,
        }
        for name, values in latencies.items()
        if values
    }
    return {"counters": counters, "gauges": gauges, "latency": latency_stats}


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _SAMPLES.clear()
