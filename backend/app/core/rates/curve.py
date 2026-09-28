"""Yield curve construction: par -> zero bootstrap, and Nelson-Siegel fitting.

Two representations, used for different jobs:

  * BOOTSTRAPPED ZERO CURVE -- exact by construction: it reprices every par
    bond on the input curve to 100 by definition. Use it for discounting and
    for anything that must tie out to quoted levels.

  * NELSON-SIEGEL FIT       -- a smooth 4-parameter model. It will NOT reprice
    the inputs exactly, and that is the point: the residual (actual - fitted)
    is the rich/cheap signal a relative-value desk trades on. Reporting the
    fit RMSE alongside is mandatory -- a model whose error you don't quote is
    a model you can't size a trade against.

Treasury par yields are semiannual-coupon conventions, so the bootstrap runs
on a semiannual grid throughout.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FREQ = 2                      # semiannual, matching Treasury conventions


@dataclass
class ZeroCurve:
    """Continuously-compounded zero rates on a maturity grid (years, decimal)."""
    years: np.ndarray
    zeros: np.ndarray

    def zero_rate(self, t: float | np.ndarray) -> np.ndarray:
        """Linear interpolation in zero space, flat extrapolation past the ends.

        Flat rather than linear extrapolation is deliberate: extending the
        last slope beyond 30Y can drive discount factors negative at long
        horizons, which silently corrupts every downstream price.
        """
        t = np.atleast_1d(np.asarray(t, dtype=float))
        return np.interp(t, self.years, self.zeros,
                         left=self.zeros[0], right=self.zeros[-1])

    def discount(self, t: float | np.ndarray) -> np.ndarray:
        t = np.atleast_1d(np.asarray(t, dtype=float))
        return np.exp(-self.zero_rate(t) * t)

    def par_yield(self, maturity: float) -> float:
        """Coupon that prices a bond of this maturity to par off this curve."""
        times = _coupon_times(maturity)
        dfs = self.discount(times)
        annuity = dfs.sum() / FREQ
        return float((1.0 - dfs[-1]) / annuity) if annuity > 0 else 0.0


def _coupon_times(maturity: float) -> np.ndarray:
    """Semiannual coupon dates; always includes maturity as the final date."""
    n = max(1, int(round(maturity * FREQ)))
    return np.arange(1, n + 1, dtype=float) / FREQ


def bootstrap(years, par_yields_pct) -> ZeroCurve:
    """Par yields (percent) -> continuously-compounded zero curve.

    Standard sequential bootstrap on a semiannual grid: par yields are first
    interpolated onto every coupon date, then discount factors are solved
    forward, each using the ones already known.
    """
    years = np.asarray(years, dtype=float)
    par = np.asarray(par_yields_pct, dtype=float) / 100.0
    grid = np.arange(1, int(round(years[-1] * FREQ)) + 1, dtype=float) / FREQ
    par_grid = np.interp(grid, years, par, left=par[0], right=par[-1])

    dfs = np.zeros(len(grid))
    for i, (t, c) in enumerate(zip(grid, par_grid)):
        coupon = c / FREQ
        prev = dfs[:i].sum()
        # 1 = coupon * sum(DF_j) + (1 + coupon) * DF_i  ->  solve for DF_i
        dfs[i] = (1.0 - coupon * prev) / (1.0 + coupon)
        # A non-positive discount factor means the input curve is not
        # arbitrage-free at this node; clamping hides that, so fail loudly.
        if dfs[i] <= 0:
            raise ValueError(
                f"bootstrap produced non-positive discount factor at t={t:.2f}y")

    zeros = -np.log(dfs) / grid
    return ZeroCurve(years=grid, zeros=zeros)


# --- Nelson-Siegel -------------------------------------------------------
def _ns_basis(tau: np.ndarray, lam: float) -> np.ndarray:
    """Level / slope / curvature loadings at decay parameter lambda."""
    x = np.clip(tau / lam, 1e-8, None)
    decay = (1.0 - np.exp(-x)) / x
    return np.column_stack([np.ones_like(tau), decay, decay - np.exp(-x)])


def fit_nelson_siegel(years, yields_pct) -> dict:
    """Fit y(t) = b0 + b1*L(t) + b2*C(t) with lambda chosen by grid search.

    Lambda enters non-linearly, so for a fixed lambda the betas are an exact
    linear least-squares solve. Scanning lambda over a grid and taking the
    best linear solve is more robust than a joint non-linear optimiser, which
    on 14 noisy points routinely converges to a local minimum with an absurd
    lambda and a beautiful-looking in-sample fit.
    """
    tau = np.asarray(years, dtype=float)
    y = np.asarray(yields_pct, dtype=float)
    best = None
    for lam in np.linspace(0.2, 12.0, 240):
        X = _ns_basis(tau, lam)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        sse = float(resid @ resid)
        if best is None or sse < best[0]:
            best = (sse, lam, beta, resid)

    sse, lam, beta, resid = best
    rmse = float(np.sqrt(sse / len(y)))
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {
        "lambda": round(float(lam), 4),
        "beta0_level": round(float(beta[0]), 4),
        "beta1_slope": round(float(beta[1]), 4),
        "beta2_curvature": round(float(beta[2]), 4),
        "rmse_pct": round(rmse, 5),
        "rmse_bps": round(rmse * 100, 3),
        "r_squared": round(1 - sse / ss_tot, 6) if ss_tot > 0 else None,
        "fitted": [round(float(v), 4) for v in (_ns_basis(tau, lam) @ beta)],
        # Positive residual = quoted above the smooth curve = CHEAP (higher
        # yield than the model implies, so the bond is priced low).
        "residuals_bps": [round(float(r) * 100, 2) for r in resid],
    }
