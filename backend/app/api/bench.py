"""Engine benchmark — MEASURED on this host, not quoted from a spec sheet.

Runs the parallel Monte Carlo at several thread counts and reports actual
throughput plus the determinism check. Numbers depend entirely on the machine
serving the request, which is why they are measured live and stamped with the
core count rather than hardcoded.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/bench", tags=["benchmark"])


@router.get("/monte-carlo")
def monte_carlo(paths: int = Query(2_000_000, ge=100_000, le=50_000_000),
                threads: str = Query("1,2,4,8")) -> dict:
    try:
        import quant_engine_cpp as cpp
    except Exception as e:
        return {"cpp_available": False,
                "reason": f"{type(e).__name__}: {e}",
                "note": "extension not built in this image; engines run on numpy"}

    cores = os.cpu_count() or 1
    counts = [int(t) for t in threads.split(",") if t.strip().isdigit()]
    rows, prices = [], []
    for n in counts:
        t0 = time.perf_counter()
        res = cpp.monte_carlo_parallel(100.0, 100.0, 1.0, 0.05, 0.0, 0.2,
                                       paths, True, 42, n)
        dt = time.perf_counter() - t0
        price = res.price if hasattr(res, "price") else float(res)
        prices.append(price)
        rows.append({
            "threads": n,
            "ms": round(dt * 1000, 2),
            "paths_per_sec": int(paths / dt) if dt > 0 else None,
            "millions_per_sec": round(paths / dt / 1e6, 2) if dt > 0 else None,
            "price": price,
            # Threads beyond the physical core count cannot add throughput;
            # flagging it stops the table reading like free scaling.
            "oversubscribed": n > cores,
        })

    base = rows[0]["paths_per_sec"] or 1
    for r in rows:
        r["speedup_vs_1_thread"] = round((r["paths_per_sec"] or 0) / base, 2)

    return {
        "cpp_available": True,
        "host_cores": cores,
        "paths_per_run": paths,
        "results": rows,
        # Bitwise determinism across thread counts is the property a risk desk
        # signs off on: the same book must not reprice differently because a
        # machine had more cores free.
        "deterministic_across_threads": len(set(f"{p:.12f}" for p in prices)) == 1,
        "reference_price": prices[0] if prices else None,
        "note": (f"measured on this host ({cores} vCPU). Throughput above the "
                 f"core count reflects hyperthreading/scheduling, not real "
                 f"parallelism."),
    }


@router.get("/engines")
def engines(paths: int = Query(1_000_000, ge=10_000, le=20_000_000),
            steps: int = Query(512, ge=32, le=4096),
            repeat: int = Query(3, ge=1, le=10)) -> dict:
    """Head-to-head C++ vs numpy on identical inputs.

    The Observability speedup panel is computed from the profiler, which only
    has a ratio when BOTH implementations have been exercised. In normal
    operation the selector always picks C++, so that panel stays empty -- the
    honest fix is to run both here explicitly rather than leave a claim on the
    screen with nothing behind it.

    Both paths price the SAME option, so the returned prices double as a
    correctness cross-check: a large speedup with a mismatched price would
    mean the fast path is fast because it is wrong.
    """
    import time

    from ..core.pricing import Option, OptionType, bsm, crr, mc_price

    try:
        import quant_engine_cpp as cpp
    except Exception as e:
        return {"cpp_available": False, "reason": f"{type(e).__name__}: {e}"}

    opt = Option(spot=100.0, strike=100.0, maturity=1.0, rate=0.05,
                 dividend=0.0, vol=0.2, kind=OptionType.CALL)
    rows = []

    def bench(label, cpp_fn, py_fn):
        best_c = best_p = None
        pc = pp = None
        for _ in range(repeat):
            t = time.perf_counter(); pc = cpp_fn(); dc = time.perf_counter() - t
            best_c = dc if best_c is None else min(best_c, dc)
            t = time.perf_counter(); pp = py_fn(); dp = time.perf_counter() - t
            best_p = dp if best_p is None else min(best_p, dp)
        rows.append({
            "engine": label,
            "cpp_ms": round(best_c * 1000, 4),
            "numpy_ms": round(best_p * 1000, 4),
            "speedup": round(best_p / best_c, 2) if best_c > 0 else None,
            "cpp_price": round(float(pc), 8),
            "numpy_price": round(float(pp), 8),
            "price_diff": round(abs(float(pc) - float(pp)), 8),
        })

    bench("black_scholes",
          lambda: cpp.black_scholes(opt.spot, opt.strike, opt.maturity, opt.rate,
                                    opt.dividend, opt.vol, True).price,
          lambda: bsm(opt).price)
    bench("binomial",
          lambda: cpp.binomial(opt.spot, opt.strike, opt.maturity, opt.rate,
                               opt.dividend, opt.vol, steps, True, False),
          lambda: crr(opt, steps).price)
    bench("monte_carlo",
          lambda: cpp.monte_carlo_parallel(opt.spot, opt.strike, opt.maturity,
                                           opt.rate, opt.dividend, opt.vol,
                                           paths, True, 7, 0).price,
          lambda: mc_price(opt, paths).price)

    return {
        "cpp_available": True,
        "host_cores": os.cpu_count(),
        "repeat": repeat,
        "results": rows,
        # Best-of-N rather than mean: on a shared vCPU the mean is dominated by
        # scheduler noise, and best-of is the standard way to compare compute
        # kernels. Stated so the number is not mistaken for an average.
        "method": "best-of-N wall clock, identical inputs, price cross-checked",
        "note": ("Monte Carlo prices differ between implementations by design -- "
                 "different RNG streams. Compare them against the analytic BSM "
                 "price, not against each other."),
    }


# 434 million Monte Carlo paths per call. That is the benchmark doing genuine
# work -- it MEASURES throughput, so it cannot be made faster without measuring
# something else. But the answer only changes when the hardware or the engine
# changes, and re-running it on every page view cost 6.5s each time. Cached,
# with an explicit ?refresh=true for when a re-measurement is actually wanted.
_MC_CACHE: dict[str, tuple[float, dict]] = {}
_MC_TTL = 3600.0


@router.get("/mc-scaling")
def mc_scaling(paths: str = Query("34000000,50000000,100000000,250000000"),
               threads: int = Query(0, ge=0, le=64),
               refresh: bool = Query(False)) -> dict:
    """Monte Carlo accuracy/throughput as path count scales.

    The point is NOT that more paths is faster -- it is that Monte Carlo error
    falls as 1/sqrt(N). Going from 34M to 250M paths is 7.4x the work for
    2.7x the precision, and this table makes that trade explicit instead of
    implying that a big path count is free.

    `analytic_error` is the honest accuracy measure: distance from the
    closed-form Black-Scholes price, which is the ground truth for this
    contract. Standard error alone tells you the estimator's precision, not
    whether it is converging to the right number.
    """
    import time

    from ..core.pricing import Option, OptionType, bsm

    key = f"{paths}|{threads}"
    hit = _MC_CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < _MC_TTL:
        return {**hit[1], "cached": True,
                "measured_seconds_ago": round(time.time() - hit[0], 1),
                "note": "cached benchmark — pass ?refresh=true to re-measure"}

    try:
        import quant_engine_cpp as cpp
    except Exception as e:
        return {"cpp_available": False, "reason": f"{type(e).__name__}: {e}"}

    S, K, T, r, q, vol = 100.0, 100.0, 1.0, 0.05, 0.0, 0.2
    analytic = bsm(Option(spot=S, strike=K, maturity=T, rate=r, dividend=q,
                          vol=vol, kind=OptionType.CALL)).price

    counts = sorted({int(p) for p in paths.split(",") if p.strip().isdigit()})
    rows, prices = [], []
    for n in counts:
        t0 = time.perf_counter()
        m = cpp.monte_carlo_parallel(S, K, T, r, q, vol, n, True, 7, threads)
        dt = time.perf_counter() - t0
        prices.append(m.price)
        rows.append({
            "paths": n,
            "seconds": round(dt, 4),
            "millions_per_sec": round(n / dt / 1e6, 2) if dt > 0 else None,
            "price": round(m.price, 9),
            "std_error": round(m.stderr, 9),
            "ci95_width": round(abs(m.ci_hi - m.ci_lo), 9),
            "analytic_error": round(abs(m.price - analytic), 9),
            "analytic_error_bps": round(abs(m.price - analytic) / analytic * 1e4, 4),
        })

    base = rows[0]
    for row in rows:
        row["paths_vs_base"] = round(row["paths"] / base["paths"], 2)
        row["error_reduction_vs_base"] = (
            round(base["std_error"] / row["std_error"], 2)
            if row["std_error"] > 0 else None)
        # sqrt(N) is the theoretical rate; showing observed-vs-theoretical is
        # the check that the estimator is behaving as advertised.
        row["theoretical_reduction"] = round(row["paths_vs_base"] ** 0.5, 2)

    out = {
        "cpp_available": True,
        "host_cores": os.cpu_count(),
        "threads": threads or os.cpu_count(),
        "analytic_price": round(analytic, 9),
        "results": rows,
        "deterministic_note": ("price is reproducible for a given path count and "
                               "seed regardless of thread count -- verified by "
                               "/api/bench/monte-carlo"),
        "convergence_note": ("standard error falls as 1/sqrt(N): 7.4x the paths "
                             "buys 2.7x the precision, not 7.4x"),
    }
    _MC_CACHE[key] = (time.time(), out)
    return {**out, "cached": False}
