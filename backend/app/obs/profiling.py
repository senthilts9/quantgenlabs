"""In-process latency profiler for the Observability terminal screen.

Prometheus + Grafana are the source of truth for ops (see metrics.py), but
a trader staring at the console shouldn't have to alt-tab to Grafana to see
whether the C++ tick engine is actually beating the Python fallback. This
keeps a small lock-free-enough ring buffer of recent durations per
(engine, impl) and computes percentiles on demand.

Thread-safe via a single lock; the critical section is a deque append.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

_MAXLEN = 2048


@dataclass(frozen=True, slots=True)
class Stat:
    engine: str
    impl: str
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    last_ms: float
    ewma_ms: float


class Profiler:
    def __init__(self, maxlen: int = _MAXLEN):
        self._buf: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=maxlen))
        self._ewma: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def record(self, engine: str, impl: str, seconds: float) -> None:
        ms = seconds * 1000.0
        key = (engine, impl)
        with self._lock:
            self._buf[key].append(ms)
            prev = self._ewma.get(key)
            self._ewma[key] = ms if prev is None else 0.2 * ms + 0.8 * prev

    def snapshot(self) -> list[Stat]:
        out: list[Stat] = []
        with self._lock:
            items = {k: list(v) for k, v in self._buf.items()}
            ewma = dict(self._ewma)
        for (engine, impl), xs in items.items():
            if not xs:
                continue
            a = sorted(xs)
            def pct(p: float) -> float:
                idx = min(len(a) - 1, int(round((p / 100.0) * (len(a) - 1))))
                return a[idx]
            out.append(Stat(engine, impl, len(a), pct(50), pct(95), pct(99),
                            a[-1] if False else max(a),
                            xs[-1], ewma.get((engine, impl), xs[-1])))
        return sorted(out, key=lambda s: (s.engine, s.impl))


profiler = Profiler()
