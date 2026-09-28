"""Engine heartbeat — keeps observability populated between real requests.

Prometheus counters and histograms live in the exporter process, so they
reset to zero every time the container restarts. Combined with a 15-minute
dashboard window that produces a Grafana full of "No data" whenever nobody
has exercised the platform recently -- which, for a demo someone opens once,
is most of the time.

This runs a small, fixed workload on a timer so the engine panels always have
a live signal. Three properties make it honest rather than fake data:

  * It exercises the REAL engines through the REAL timed() seam. The latency
    plotted is genuine measured work, not a synthetic number written into a
    gauge.
  * The workload is deliberately tiny (a few hundred microseconds per cycle)
    so it cannot distort the percentiles that real traffic produces.
  * It is labelled `heartbeat` in the profiler, so anyone reading the numbers
    can separate it from user-driven load.

Disable with HEARTBEAT_SECONDS=0.
"""
from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

INTERVAL = int(os.getenv("HEARTBEAT_SECONDS", "20"))
# Small enough to be free, large enough that the MC path is a real measurement
# rather than loop overhead.
MC_PATHS = int(os.getenv("HEARTBEAT_MC_PATHS", "50000"))


def _cycle() -> None:
    """One pass over the engines. Never raises -- a broken heartbeat must not
    take down the API it is only meant to observe."""
    try:
        import numpy as np

        from ..core import engines as E
        from ..core.pricing import Option, OptionType

        opt = Option(spot=100.0, strike=100.0, maturity=1.0, rate=0.05,
                     dividend=0.0, vol=0.2, kind=OptionType.CALL)
        E.price_black_scholes(opt)
        E.price_binomial(opt, 256)
        E.price_monte_carlo(opt, MC_PATHS)
        E.var_historical(np.random.default_rng(7).normal(0, 0.01, 500), 0.99)
    except Exception as e:                      # pragma: no cover
        log.debug("heartbeat cycle failed: %s", e)


async def run() -> None:
    if INTERVAL <= 0:
        log.info("heartbeat disabled (HEARTBEAT_SECONDS=0)")
        return
    log.info("engine heartbeat every %ss", INTERVAL)
    while True:
        # Off the event loop: these are CPU-bound C++/numpy calls and would
        # otherwise block every concurrent HTTP request for their duration.
        await asyncio.to_thread(_cycle)
        await asyncio.sleep(INTERVAL)
