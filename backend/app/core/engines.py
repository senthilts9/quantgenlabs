"""Engine selection — one interface, C++ or numpy, always instrumented.

This is the seam that makes "performance where it matters" real AND
observable. Each call:
  1. picks the C++ extension if it's built, else the numpy reference;
  2. runs inside `timed(engine, impl)` so Prometheus/Grafana and the
     in-terminal profiler record cpp vs numpy latency separately —
     lighting up the C++-vs-Python speedup panel for the CORE engines,
     not just crypto.

Import is defensive: if `quant_engine_cpp` isn't compiled, `HAVE_CPP` is
False and everything transparently uses the verified numpy core. No call
site changes; the only observable difference is the `impl` label and the
latency.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import asdict

import numpy as np

from ..obs import timed                    # the single timing seam
from .pricing import bsm, crr, mc_price, Exercise, Option, OptionType
from .risk import historical, parametric

try:
    import quant_engine_cpp as _cpp        # pybind11 module (optional)
    HAVE_CPP = True
except Exception:                          # not built → numpy everywhere
    _cpp = None
    HAVE_CPP = False

# Allow forcing an impl for benchmarking/testing: QUANT_ENGINE=numpy|cpp|auto
_FORCE = os.getenv("QUANT_ENGINE", "auto").lower()


# Per-thread implementation pin, used by the calibration service to run the
# SAME workload down both paths. Thread-local rather than global: request
# handlers run concurrently in a threadpool, and a global switch would silently
# drag someone else's pricing call onto the slow path mid-flight.
_PIN = threading.local()


def _impl(prefer_cpp: bool) -> str:
    pinned = getattr(_PIN, "impl", None)
    if pinned:
        return pinned
    if _FORCE == "numpy":
        return "numpy"
    if _FORCE == "cpp":
        return "cpp"
    return "cpp" if (prefer_cpp and HAVE_CPP) else "numpy"


@contextmanager
def pin_impl(impl: str):
    """Pin `impl` for the current thread.

    Exists so the C++-vs-numpy comparison can be MEASURED rather than asserted.
    In normal operation the selector always picks C++, so the numpy path is
    never timed and any speedup panel has one of its two operands missing --
    which is precisely why that Grafana panel read "No data".

    Asking for cpp when the extension isn't built yields numpy rather than
    raising: a calibration pass is diagnostics, and it must not be able to take
    down the process it is measuring.
    """
    if impl == "cpp" and not HAVE_CPP:
        impl = "numpy"
    prev = getattr(_PIN, "impl", None)
    _PIN.impl = impl
    try:
        yield impl
    finally:
        _PIN.impl = prev


# ---- pricing -----------------------------------------------------------
def price_black_scholes(opt: Option) -> dict:
    impl = _impl(True)
    with timed("black_scholes", impl):
        if impl == "cpp":
            p = _cpp.black_scholes(opt.spot, opt.strike, opt.maturity, opt.rate,
                                   opt.dividend, opt.vol, opt.kind is OptionType.CALL)
            g = p.greeks
            return {"price": p.price, "greeks": {"delta": g.delta, "gamma": g.gamma,
                    "vega": g.vega, "theta": g.theta, "rho": g.rho}, "impl": impl}
        r = bsm(opt)
        return {"price": r.price, "greeks": asdict(r.greeks), "impl": impl}


def price_binomial(opt: Option, steps: int = 512) -> dict:
    impl = _impl(True)
    with timed("binomial", impl):
        if impl == "cpp":
            # The `american` flag was hardcoded False, so the compiled path
            # silently priced every American option as European and dropped
            # the early-exercise premium -- ~17% on the standard Longstaff-
            # Schwartz put (3.84 returned against a true 4.49). The numpy CRR
            # honoured opt.exercise correctly, so the two engines disagreed
            # and only the FAST one was wrong: the bug appeared exactly when
            # the C++ extension loaded, i.e. only in production.
            price = _cpp.binomial(opt.spot, opt.strike, opt.maturity, opt.rate,
                                  opt.dividend, opt.vol, steps,
                                  opt.kind is OptionType.CALL,
                                  opt.exercise is Exercise.AMERICAN)
            return {"price": price, "impl": impl}
        return {"price": crr(opt, steps).price, "impl": impl}


def price_lsm(opt: Option, paths: int = 200_000, steps: int = 50, *,
              n_threads: int = 0, degree: int = 3) -> dict:
    """Longstaff-Schwartz American Monte Carlo.

    The compiled `longstaff_schwartz` binding has existed since the C++ module
    was written and was never called from Python -- so the platform advertised
    "American LSM" while the only American number it could produce came from
    the binomial tree. This exposes it.

    The early-exercise premium is returned alongside the price because it is
    the number that proves the algorithm did something: if LSM and its own
    European control converge to the same value, the regression never found an
    exercise boundary worth taking, and the result is European in all but name.

    Falls back to the binomial tree when the extension is absent, since the
    numpy CRR handles American exercise correctly.
    """
    impl = _impl(True)
    with timed("lsm", impl):
        if impl == "cpp":
            fn = getattr(_cpp, "longstaff_schwartz", None)
            if fn is not None:
                r = fn(opt.spot, opt.strike, opt.maturity, opt.rate,
                       opt.dividend, opt.vol, paths, steps,
                       opt.kind is OptionType.CALL, 7, n_threads, degree)
                return {"price": r.price, "stderr": r.stderr,
                        "european": r.european,
                        "early_exercise_premium": r.early_exercise_premium,
                        "paths": r.paths, "steps": r.steps, "impl": impl}
        # numpy fallback: CRR honours American exercise, so this is a correct
        # American price -- just not a Monte Carlo one, which is stated.
        res = crr(opt, 512)
        return {"price": res.price, "stderr": None, "european": None,
                "early_exercise_premium": None, "paths": 0, "steps": 512,
                "impl": impl, "note": "numpy fallback: binomial tree, not LSM"}


def price_monte_carlo(opt: Option, paths: int = 1_000_000, *,
                      n_threads: int = 0) -> dict:
    """The engine where C++ wins ~5×/core with constant memory.

    n_threads=0 → all cores. Result is deterministic regardless of thread
    count (fixed per-block seeding), so this is safe to parallelise in a
    risk system that must reproduce numbers exactly.
    """
    impl = _impl(True)
    with timed("monte_carlo", impl):
        if impl == "cpp":
            fn = getattr(_cpp, "monte_carlo_parallel", None)
            if fn is not None:
                m = fn(opt.spot, opt.strike, opt.maturity, opt.rate,
                       opt.dividend, opt.vol, paths,
                       opt.kind is OptionType.CALL, 7, n_threads)
            else:
                m = _cpp.monte_carlo(opt.spot, opt.strike, opt.maturity, opt.rate,
                                     opt.dividend, opt.vol, paths,
                                     opt.kind is OptionType.CALL)
            return {"price": m.price, "stderr": m.stderr,
                    "ci95": [m.ci_lo, m.ci_hi], "paths": m.paths, "impl": impl}
        r = mc_price(opt, paths)
        return {"price": r.price, "stderr": r.stderr, "ci95": list(r.ci95),
                "impl": impl}


# ---- risk --------------------------------------------------------------
def var_historical(returns: np.ndarray, confidence: float = 0.99) -> dict:
    # This path used to be the platform's one published case of C++ LOSING to
    # numpy, at 0.6x. The cause was not the kernel and not the language: the
    # call below read `list(map(float, returns))`, which boxed every element
    # into a Python float so pybind11 could unbox it again. At a 704-point
    # window that conversion cost 40.9us around a 7.5us kernel -- 5.4x the
    # arithmetic it was feeding, and enough to turn a 10x win into 0.58x.
    #
    # The binding now takes the numpy buffer directly (see quant_bindings.cpp),
    # so the array must be passed through as an array. Re-introducing any
    # per-element conversion here silently restores the old behaviour.
    impl = _impl(prefer_cpp=False)
    with timed("var_historical", impl):
        if impl == "cpp":
            v = _cpp.historical_var(np.ascontiguousarray(returns, dtype=float),
                                    confidence)
            return {"var": v.var, "es": v.es, "impl": impl}
        r = historical(np.asarray(returns, float), confidence)
        return {"var": r.var, "es": r.es, "impl": impl}


def engine_info() -> dict:
    return {"cpp_available": HAVE_CPP, "forced": _FORCE,
            "active_default": _impl(True)}
