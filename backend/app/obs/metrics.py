"""Prometheus instrumentation + the one timing seam every engine uses.

`timed(engine, impl)` is both a decorator and a context manager. It updates
a Prometheus Histogram (which Grafana turns into p50/p95/p99 via
histogram_quantile) AND the in-process profiler that powers the terminal's
Observability screen — so there is exactly one place engine latency is
recorded, and the two views can never drift apart.

Bucket edges are chosen for microsecond-to-second engine calls; adjust in
one place if your workload shifts.
"""
from __future__ import annotations

import time
from contextlib import ContextDecorator

from prometheus_client import Counter, Gauge, Histogram

from .profiling import profiler

# Bucket edges decide what Grafana can RESOLVE, and the old floor of 100us was
# above the engines being measured: Black-Scholes prices in ~36us (cpp) vs
# ~43us (numpy), so every sample of both landed in the first bucket and
# `histogram_quantile` had nothing to interpolate between. The speedup panel
# duly reported 0.95x for a pair whose measured ratio is 1.21x -- a number
# derived from a bucket edge rather than from the data, which is worse than no
# number at all on a dashboard whose whole claim is "measured, not asserted".
#
# The floor is now 1us, roughly a decade below the fastest engine, and the
# ladder stays ~2.5x per step up to 10s. 22 edges x (4 engines x 2 impls) is a
# few hundred series -- nothing, and the resolution is what makes the panel
# trustworthy.
_BUCKETS = (
    .000001, .0000025, .000005, .00001, .000025, .00005,
    .0001, .00025, .0005, .001, .0025, .005, .0075,
    # The millisecond decade is deliberately dense. Monte Carlo at 1M paths
    # runs ~6.6ms (cpp) against ~34.7ms (numpy), and on the coarse ladder those
    # straddled .005-.01 and .025-.05 -- two very wide buckets, so the
    # interpolated p50 swung with tiny shifts in the distribution and the panel
    # printed 17x for a pair whose measured ratio is 5.3x. Resolution here is
    # what makes the headline engine's number reproducible rather than lucky.
    .01, .015, .02, .03, .04, .05, .075,
    .1, .25, .5, 1.0, 2.5, 5.0, 10.0,
)

ENGINE_LATENCY = Histogram(
    "meridian_engine_latency_seconds",
    "Engine execution time",
    labelnames=("engine", "impl"),
    buckets=_BUCKETS,
)
ENGINE_CALLS = Counter(
    "meridian_engine_calls_total", "Engine invocations",
    labelnames=("engine", "impl"),
)
ENGINE_ERRORS = Counter(
    "meridian_engine_errors_total", "Engine failures",
    labelnames=("engine", "impl"),
)
ENGINE_LAST = Gauge(
    "meridian_engine_last_seconds", "Last engine execution time",
    labelnames=("engine", "impl"),
)

# --- controlled C++-vs-numpy comparison ---------------------------------
#
# These are published by services.engine_calibration, which runs ONE fixed
# workload down both paths back to back. They exist because deriving that
# ratio from the latency histogram is wrong twice over:
#
#   * histogram_quantile interpolates inside bucket edges, so the answer moves
#     with the ladder rather than with the code;
#   * every caller of an engine shares one series, so a bench run at 2M paths
#     and a pricing screen at 200k land in the same bucket set as the
#     calibration's 1M. The p50 of that mixture is not a measurement of
#     anything. It read 15.9x for a pair whose controlled ratio is 5.2x.
#
# A gauge carrying the calibration's own timing is exact, like-for-like, and
# says precisely what it measured.
CALIB_SECONDS = Gauge(
    "meridian_calibration_seconds",
    "Controlled fixed-workload engine timing (calibration only)",
    labelnames=("engine", "impl"),
)
CALIB_SPEEDUP = Gauge(
    "meridian_calibration_speedup_ratio",
    "numpy/cpp wall-clock ratio on an identical workload (calibration only)",
    labelnames=("engine",),
)


class timed(ContextDecorator):
    """with timed('monte_carlo', 'numpy'): ...   |   @timed('binomial')"""
    def __init__(self, engine: str, impl: str = "python"):
        self.engine, self.impl = engine, impl

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, *_):
        dt = time.perf_counter() - self._t0
        labels = (self.engine, self.impl)
        ENGINE_LATENCY.labels(*labels).observe(dt)
        ENGINE_CALLS.labels(*labels).inc()
        ENGINE_LAST.labels(*labels).set(dt)
        if exc_type is not None:
            ENGINE_ERRORS.labels(*labels).inc()
        profiler.record(self.engine, self.impl, dt)
        return False    # never swallow exceptions
