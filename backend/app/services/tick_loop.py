"""Live tick loop — per-tick risk, instrumented for Grafana.

Consumes a tick stream (live XRPL/perp feed in prod; a synthetic generator
here so it runs offline) and, on every tick, recomputes the position's
margin state and a rolling VaR. Both calls go through the SAME `timed()`
seam the order-book engine uses, so the crypto risk calc's p50/p95/p99 show
up in Grafana and the in-terminal profiler right next to the matching-engine
latency — one dashboard, every hot path.

Chooses the C++ crypto engine when the extension is built, else the pure
Python `xquant.crypto` reference — recorded as impl="cpp"/"python" so the
speedup panel populates. Run:

    python -m app.services.tick_loop            # synthetic, 5s
    QUANT_ENGINE=cpp python -m app.services.tick_loop
"""
from __future__ import annotations

import collections
import random
import time
from dataclasses import asdict

from ..obs import timed, profiler

# --- engine selection: C++ crypto hot path if available, else Python ----
try:
    import qgl_xrpl_crypto as _cc          # pybind11 crypto engine
    _IMPL = "cpp"
except Exception:
    _cc = None
    _IMPL = "python"

if _cc is None:
    from app.core.crypto import liquidation as _liq   # pure-Python fallback
    from app.core.crypto import risk as _risk


def _margin(entry, mark, qty, lev, is_long):
    with timed("crypto_margin", _IMPL):
        if _cc is not None:
            ms = _cc.margin_state(entry, mark, qty, lev, is_long, 0.005)
            return ms.distance_to_liq, ms.margin_ratio
        ms = _liq.margin_state(entry, mark, qty, lev, is_long)
        return ms.distance_to_liq, ms.margin_ratio


def _var(window):
    with timed("crypto_var", _IMPL):
        if _cc is not None:
            v = _cc.historical_var(list(window), 0.99)
            return v.var
        return _risk.historical_var(list(window), 0.99).var


def run(duration_s: float = 5.0, ticks_per_s: int = 200, *,
        entry: float = 2.10, qty: float = 40_000, leverage: float = 10.0) -> dict:
    """Drive synthetic ticks and recompute risk on each. Returns a summary."""
    mark = entry
    window: collections.deque[float] = collections.deque(maxlen=500)
    n = 0
    t_end = time.perf_counter() + duration_s
    interval = 1.0 / ticks_per_s
    last_report = time.perf_counter()

    print(f"tick loop: impl={_IMPL}  {ticks_per_s} ticks/s for {duration_s}s")
    while time.perf_counter() < t_end:
        # synthetic GBM-ish tick (prod: replace with feed.next())
        ret = random.gauss(0, 0.004)
        mark *= (1.0 + ret)
        window.append(ret)

        dist, mr = _margin(entry, mark, qty, leverage, True)   # timed
        if len(window) >= 30:
            _var(window)                                        # timed
        n += 1

        now = time.perf_counter()
        if now - last_report >= 1.0:                            # 1 Hz console line
            _print_percentiles(mark, dist)
            last_report = now
        time.sleep(max(0.0, interval - 0.00005))

    print(f"\nprocessed {n} ticks")
    return {"ticks": n, "impl": _IMPL,
            "profiler": [asdict(s) for s in profiler.snapshot()]}


def _print_percentiles(mark: float, dist: float) -> None:
    snap = {(s.engine, s.impl): s for s in profiler.snapshot()}
    parts = [f"mark={mark:.4f} distLiq={dist*100:+.1f}%"]
    for key in ("crypto_margin", "crypto_var"):
        s = snap.get((key, _IMPL))
        if s:
            parts.append(f"{key} p50={s.p50_ms:.3f}ms p99={s.p99_ms:.3f}ms")
    print("  " + "  ".join(parts))


if __name__ == "__main__":
    import json
    summary = run()
    print(json.dumps(summary["profiler"], indent=2))
