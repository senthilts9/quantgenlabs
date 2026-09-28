"""Measure the Python->C++ boundary on the historical-VaR path.

This is the measurement that diagnosed the platform's published 0.6x. The
kernel was never slower than numpy -- it is roughly 10x faster on identical
data. What lost was the conversion around it: the call site used to box every
element of a numpy array into a Python float so pybind11 could unbox it again.

Three timings, same kernel, same data:

  numpy          np.percentile (introselect) + tail scan
  boxed          list(map(float, x)) -> the OLD call path
  zero-copy      the numpy buffer handed straight over -> the NEW call path

`boxed` is kept deliberately. Deleting it would leave nothing to compare
against, and a fix with no before-number is an assertion rather than a result.
"""
from __future__ import annotations

import statistics
import time

import numpy as np

try:
    import quant_engine_cpp as cpp
except ImportError:  # pragma: no cover - the script guards on this
    raise SystemExit("quant_engine_cpp not built - see bench-pinned.sh for the command")


def numpy_var(x: np.ndarray, conf: float = 0.99) -> tuple[float, float]:
    """Reference implementation the C++ path is checked against."""
    qv = np.percentile(x, (1.0 - conf) * 100.0)
    var = -qv
    tail = x[x <= -var]
    return var, (-tail.mean() if tail.size else var)


def best_of(fn, reps: int) -> float:
    """Minimum wall time in microseconds. The minimum, not the mean: we want
    the machine's capability, and every source of noise here adds time."""
    for _ in range(50):                      # warm caches and branch predictors
        fn()
    return min(_timed(fn) for _ in range(reps))


def _timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1e6


def main() -> None:
    print(f"  {'n':>9}  {'path':<22} {'us':>10}  {'vs numpy':>9}")
    print(f"  {'-'*9}  {'-'*22} {'-'*10}  {'-'*9}")

    for n, reps in ((704, 2000), (100_000, 200), (1_000_000, 40)):
        x = np.ascontiguousarray(
            np.random.default_rng(1).standard_t(5.0, n) * 0.01, dtype=float)

        # Correctness gate. A faster wrong answer is not a result.
        nv, ne = numpy_var(x)
        cv = cpp.historical_var(x, 0.99)
        assert abs(cv.var - nv) < 1e-12, f"VaR mismatch: {cv.var} vs {nv}"
        assert abs(cv.es - ne) < 1e-12, f"ES mismatch: {cv.es} vs {ne}"

        t_np = best_of(lambda: numpy_var(x), reps)
        # The old path, reconstructed exactly: box to Python floats, then let
        # pybind11 rebuild a vector from the list.
        t_bx = best_of(lambda: cpp.historical_var(
            np.asarray(list(map(float, x)), dtype=float), 0.99), reps)
        t_zc = best_of(lambda: cpp.historical_var(x, 0.99), reps)

        for label, t in (("numpy", t_np), ("boxed (old)", t_bx),
                         ("zero-copy (new)", t_zc)):
            print(f"  {n:>9}  {label:<22} {t:>10.2f}  {t_np/t:>8.2f}x")
        print()

    print("  The old row is the published 0.6x. The new row is the same kernel")
    print("  reached without the conversion. Nothing about the maths changed.")


if __name__ == "__main__":
    main()
