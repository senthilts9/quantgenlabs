"""VaR model backtesting — coverage & independence tests.

Given realised P&L (or returns) and the VaR forecast that stood on each
day, count exceptions and run the standard regulatory tests. This is
*model* backtesting (is the VaR honest?), distinct from *strategy*
backtesting (is the alpha real?).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import chi2


@dataclass(frozen=True, slots=True)
class BacktestResult:
    observations: int
    exceptions: int
    expected: float
    confidence: float
    kupiec_stat: float
    kupiec_p: float
    christoffersen_ind_stat: float
    christoffersen_ind_p: float
    christoffersen_cc_stat: float
    christoffersen_cc_p: float
    basel_zone: str            # GREEN / YELLOW / RED (Basel traffic light, 250d)


def _basel_zone(exceptions: int, n: int = 250) -> str:
    scale = n / 250.0
    if exceptions <= 4 * scale:
        return "GREEN"
    if exceptions <= 9 * scale:
        return "YELLOW"
    return "RED"


def backtest_var(pnl: np.ndarray, var_forecast: np.ndarray,
                 confidence: float = 0.99) -> BacktestResult:
    pnl = np.asarray(pnl, float)
    vf = np.asarray(var_forecast, float)
    if pnl.shape != vf.shape:
        raise ValueError("pnl and var_forecast must align")

    breach = (pnl < -vf).astype(int)            # loss beyond VaR
    n, x = breach.size, int(breach.sum())
    p = 1.0 - confidence

    # Kupiec proportion-of-failures (unconditional coverage).
    #
    # The endpoints x=0 and x=n are *removable* singularities, not undefined
    # cases. Each log term is multiplied by a count that goes to zero with it,
    # and 0·log(0) → 0 in the limit, so the statistic is finite at both ends.
    # Guarding them away as `kup = 0.0` is not a safe fallback — it is the most
    # dangerous possible answer, because LR=0 is p=1.000, the value that means
    # "perfectly calibrated". A model that never breached its VaR across 209
    # forecasts is not perfect; at 99% it should have breached ~2.1 times, and
    # the honest statistic is -2n·ln(1-p) = 4.201, p = 0.040 — a rejection of
    # correct coverage for being *too conservative*. Reporting that as a pass
    # inverts the verdict on exactly the model a reader would trust most.
    pi = x / n
    term_breach = x * np.log(p / pi) if x > 0 else 0.0
    term_quiet = (n - x) * np.log((1 - p) / (1 - pi)) if x < n else 0.0
    kup = -2.0 * (term_breach + term_quiet)
    kup_p = 1.0 - chi2.cdf(kup, 1)

    # Christoffersen independence (no clustering of breaches)
    n00 = n01 = n10 = n11 = 0
    for a, b in zip(breach[:-1], breach[1:]):
        if a == 0 and b == 0: n00 += 1
        elif a == 0 and b == 1: n01 += 1
        elif a == 1 and b == 0: n10 += 1
        else: n11 += 1
    ind = 0.0
    denom0, denom1 = (n00 + n01), (n10 + n11)
    if denom0 and denom1 and (n01 + n11):
        p01 = n01 / denom0 if denom0 else 0.0
        p11 = n11 / denom1 if denom1 else 0.0
        pi = (n01 + n11) / (n00 + n01 + n10 + n11)
        def _ll(a, b): return a * np.log(b) if (a > 0 and b > 0) else 0.0
        l_null = _ll(n01 + n11, pi) + _ll(n00 + n10, 1 - pi)
        l_alt = (_ll(n01, p01) + _ll(n00, 1 - p01)
                 + _ll(n11, p11) + _ll(n10, 1 - p11))
        ind = -2.0 * (l_null - l_alt)
    ind_p = 1.0 - chi2.cdf(ind, 1)

    cc = kup + ind                              # conditional coverage
    cc_p = 1.0 - chi2.cdf(cc, 2)

    return BacktestResult(
        observations=n, exceptions=x, expected=p * n, confidence=confidence,
        kupiec_stat=float(kup), kupiec_p=float(kup_p),
        christoffersen_ind_stat=float(ind), christoffersen_ind_p=float(ind_p),
        christoffersen_cc_stat=float(cc), christoffersen_cc_p=float(cc_p),
        basel_zone=_basel_zone(x, n),
    )
