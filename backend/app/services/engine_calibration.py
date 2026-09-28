"""Keeps the C++-vs-numpy comparison measured, not asserted.

The Observability screen and the Grafana dashboard both show a C++-vs-Python
speedup. That ratio needs BOTH implementations timed for the same engine --
and in normal operation the selector always picks C++, so the numpy operand
never exists. The panel therefore read "No data" while the platform's headline
claim was precisely that C++ is faster.

Worse, `meridian_engine_*` has no series at all until something prices an
option. After a redeploy the whole engine section of the dashboard is empty
until a visitor happens to open a pricing screen, which is a bad look on a
dashboard whose job is to prove the engines work.

This runs a small fixed workload down BOTH paths on a timer, through the same
`timed()` seam every real call uses. Three consequences, all intended:

  * The speedup panel always has two live operands, and the number under it is
    a real head-to-head on identical inputs -- not a figure from a README.
  * The engine series exist from the moment the process starts, so a dashboard
    opened straight after a deploy shows engines rather than blank panels.
  * Any regression in the C++ path shows up on the dashboard within a minute,
    whether or not a user happened to price something.

The workload is deliberately small. This is instrumentation, not a benchmark:
it exists to keep the ratio current and honest, and the absolute latencies are
comparable between impls because both price the SAME contract with the same
parameters on the same box, back to back.

Disable with ENGINE_CALIBRATION_SECONDS=0.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import numpy as np

log = logging.getLogger(__name__)

# 30s against Grafana's 5-minute rate() window leaves ~10 samples per bucket:
# enough for a stable p50, cheap enough to be invisible on a small instance.
# 60s: REPEAT passes over a 1M-path Monte Carlo is ~1.7s of CPU, so a tighter
# interval would start competing with request handling for no extra accuracy.
INTERVAL = float(os.getenv("ENGINE_CALIBRATION_SECONDS", "60"))

# Sizes chosen so a full pass (both impls, four engines) is tens of
# milliseconds. Big enough that the C++/numpy gap is real and not dominated by
# call overhead; small enough that this never competes with a user request.
# 1M paths, matching the regime the platform's headline C++ figure is quoted
# in. At 100k the fixed overhead of the compiled path still dominates and the
# panel showed ~1.8x -- true for 100k, but a visitor reading it next to a "~5x"
# claim would reasonably conclude one of the two was invented. The dashboard
# should measure the case being claimed.
MC_PATHS = int(os.getenv("ENGINE_CALIBRATION_PATHS", "1000000"))
# 512 steps: the same tree the head-to-head bench and the LSM numpy fallback
# price. At 256 the dashboard read ~30x while /api/bench/engines read ~42x for
# what a reader reasonably assumes is the same thing — two true numbers that
# look like a contradiction. One workload, one number.
BINOMIAL_STEPS = 512
VAR_SAMPLE = 1000

# Best-of-N, not a single shot. Black-Scholes prices in ~40us, which on a
# shared instance is the same order as scheduler jitter -- single samples had
# the panel reporting 0.67x for it, i.e. C++ "slower" than numpy, which is
# noise wearing a number's clothes. The minimum is the right statistic for a
# microbenchmark: contention can only ever make a run slower, never faster, so
# the fastest observed run is the closest estimate of the true cost. This is
# the same method /api/bench/engines already uses.
REPEAT = int(os.getenv("ENGINE_CALIBRATION_REPEAT", "3"))

# A FIXED repeat count is the wrong knob when the engines differ by three
# orders of magnitude. Three reps of a 1M-path Monte Carlo is 1.6s of work;
# three reps of a 40us Black-Scholes is 120us, far too little to average out
# scheduler jitter on a shared vCPU -- that engine swung 1.33x-2.64x across
# consecutive passes, and var_historical looked like noise rather than the
# genuine result it turned out to be.
#
# Budget per (engine, impl) instead: keep repeating until this much time has
# been spent, so cheap engines get hundreds of samples and expensive ones get
# the minimum. Same total cost, far better resolution where it was missing.
BUDGET_S = float(os.getenv("ENGINE_CALIBRATION_BUDGET_S", "0.05"))
MAX_REPEAT = 5000


def _cycle(publish: bool = True) -> None:
    """One calibration pass. Never raises: instrumentation must not be able to
    take down the thing it instruments.

    `publish=False` runs the workload but reports nothing -- used for the first
    pass after start. A process's FIRST numpy call pays allocation, page-fault
    and cache warmup that no later call does, and on a cold container that pass
    measured var_historical at 130x and Monte Carlo at 23x against steady-state
    ratios of ~1.3x and ~5.2x. Publishing it would put a number on the
    dashboard that is real, reproducible, and about the wrong thing.
    """
    from ..core.engines import (HAVE_CPP, pin_impl, price_binomial,
                                price_black_scholes, price_monte_carlo,
                                var_historical)
    from ..core.pricing import Option, OptionType
    from ..obs.metrics import CALIB_SECONDS, CALIB_SPEEDUP

    opt = Option(spot=100.0, strike=100.0, maturity=1.0, rate=0.05,
                 dividend=0.0, vol=0.2, kind=OptionType.CALL)
    # A fixed pseudo-random sample, not live returns: the point is a stable
    # like-for-like timing, and a workload that changes size between passes
    # would show up as latency drift that isn't real.
    rets = np.random.default_rng(7).normal(0.0005, 0.012, VAR_SAMPLE)

    work = (
        ("black_scholes",  lambda: price_black_scholes(opt)),
        ("binomial",       lambda: price_binomial(opt, steps=BINOMIAL_STEPS)),
        ("monte_carlo",    lambda: price_monte_carlo(opt, paths=MC_PATHS)),
        ("var_historical", lambda: var_historical(rets, 0.99)),
    )
    impls = ("cpp", "numpy") if HAVE_CPP else ("numpy",)
    timings: dict[tuple[str, str], float] = {}

    for impl in impls:
        for name, fn in work:
            try:
                best = None
                n = 0
                with pin_impl(impl):
                    spent0 = time.perf_counter()
                    while True:
                        t0 = time.perf_counter()
                        fn()
                        dt = time.perf_counter() - t0
                        best = dt if best is None else min(best, dt)
                        n += 1
                        if n >= REPEAT and (time.perf_counter() - spent0) >= BUDGET_S:
                            break
                        if n >= MAX_REPEAT:
                            break
                dt = best
                timings[(name, impl)] = dt
                if publish:
                    CALIB_SECONDS.labels(name, impl).set(dt)
            except Exception as e:               # noqa: BLE001
                log.warning("engine calibration failed (%s/%s): %s: %s",
                            name, impl, type(e).__name__, e)

    # Publish the ratio only when BOTH sides of this pass measured successfully.
    # A speedup carried over from an older pass would be a number on a
    # dashboard that nothing just measured.
    if publish:
        for name, _ in work:
            c, n = timings.get((name, "cpp")), timings.get((name, "numpy"))
            if c and n and c > 0:
                CALIB_SPEEDUP.labels(name).set(n / c)


async def run() -> None:
    if INTERVAL <= 0:
        log.info("engine calibration disabled")
        return
    # Discard the first pass: it measures process warmup, not the engines.
    await asyncio.to_thread(_cycle, False)
    while True:
        # to_thread: these are CPU-bound numeric calls, and a numpy Monte Carlo
        # on the event loop would stall every concurrent request for its whole
        # duration.
        await asyncio.to_thread(_cycle)
        await asyncio.sleep(INTERVAL)
